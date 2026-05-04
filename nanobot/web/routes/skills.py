"""Skills manager routes — list, view, and toggle skills."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from nanobot.agent.skills import SkillsLoader

router = APIRouter()


def _get_workspace(request: Request) -> Path:
    return request.app.state.config.workspace_path


def _get_disabled_skills(request: Request) -> list[str]:
    return list(request.app.state.config.tools.disabled_skills)


@router.get("/skills", response_class=HTMLResponse)
async def skills_list(request: Request):
    """List all available skills."""
    workspace = _get_workspace(request)
    disabled_skills = _get_disabled_skills(request)
    loader = SkillsLoader(workspace)
    all_skills = loader.list_skills(filter_unavailable=False)

    # Determine which skills meet system requirements (ignoring manual disabling)
    available_set = {
        s["name"] for s in all_skills
        if loader._check_requirements(loader._get_skill_meta(s["name"]))
    }

    skills_data = []
    for s in all_skills:
        meta = loader.get_skill_metadata(s["name"])
        skill_meta = loader._get_skill_meta(s["name"])
        meets_requirements = s["name"] in available_set
        is_manually_disabled = s["name"] in disabled_skills

        missing_reason = ""
        install_hint = ""
        if not meets_requirements:
            missing_reason = loader._get_missing_requirements(skill_meta)
            installs = skill_meta.get("install", [])
            if installs:
                hints = []
                for i in installs:
                    kind = i.get("kind", "")
                    pkg = i.get("formula") or i.get("package", "")
                    label = i.get("label", "")
                    if kind and pkg:
                        hints.append(f"{kind} install {pkg}")
                    elif label:
                        hints.append(label)
                install_hint = "  |  ".join(hints)

        # Check if workspace skill also has a builtin counterpart
        has_builtin = False
        if s["source"] == "workspace" and loader.builtin_skills:
            has_builtin = (loader.builtin_skills / s["name"] / "SKILL.md").exists()

        skills_data.append({
            "name": s["name"],
            "source": s["source"],
            "path": s["path"],
            "description": meta.get("description", "") if meta else "",
            "meets_requirements": meets_requirements,
            "manually_disabled": is_manually_disabled,
            "available": meets_requirements and not is_manually_disabled,
            "missing_reason": missing_reason,
            "install_hint": install_hint,
            "has_builtin": has_builtin,
        })

    # Load MCP servers from config
    mcp_servers = {}
    try:
        from nanobot.config.loader import load_config
        cfg = load_config()
        for name, srv in cfg.tools.mcp_servers.items():
            mcp_servers[name] = {
                "name": name,
                "type": srv.type or ("stdio" if srv.command else "sse"),
                "command": srv.command,
                "args": srv.args,
                "env": srv.env,
                "url": srv.url,
                "headers": srv.headers,
                "tool_timeout": srv.tool_timeout,
                "enabled_tools": srv.enabled_tools,
            }
    except Exception as e:
        logger.warning("[Skills] Failed to load MCP servers: {}", e)

    return request.app.state.templates.TemplateResponse(request, "skills.html", {"skills": skills_data,
        "total": len(skills_data),
        "workspace": str(workspace),
        "mcp_servers": mcp_servers})


@router.post("/skills/{name}/toggle")
async def toggle_skill(request: Request, name: str):
    """Toggle a skill's enabled/disabled state."""
    from nanobot.config.loader import load_config, save_config

    config = load_config()
    disabled = list(config.tools.disabled_skills)

    if name in disabled:
        disabled.remove(name)
        enabled = True
    else:
        disabled.append(name)
        enabled = False

    config.tools.disabled_skills = disabled
    save_config(config)
    request.app.state.config = config

    # Hot-reload: update live agent's SkillsLoader so change takes effect immediately
    agent = getattr(request.app.state, "agent", None)
    if agent and hasattr(agent, "context") and hasattr(agent.context, "skills"):
        agent.context.skills.disabled_skills = set(disabled)

    action = "enabled" if enabled else "disabled"
    logger.info("[Skills] Skill '{}' {} via web dashboard", name, action)
    return JSONResponse({"success": True, "enabled": enabled, "skill": name})


@router.get("/skills/{name}", response_class=HTMLResponse)
async def skill_detail(request: Request, name: str):
    """View a specific skill's SKILL.md content."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    content = loader.load_skill(name)
    if content is None:
        return HTMLResponse("Skill not found", status_code=404)

    # Get metadata
    meta = loader.get_skill_metadata(name)

    # Determine source and override state
    all_skills = loader.list_skills(filter_unavailable=False)
    source = "unknown"
    for s in all_skills:
        if s["name"] == name:
            source = s["source"]
            break

    # Check if both builtin and workspace versions exist
    builtin_path = loader.builtin_skills / name / "SKILL.md"
    workspace_path = loader.workspace_skills / name / "SKILL.md"
    has_builtin = builtin_path.exists()
    has_workspace = workspace_path.exists()
    is_overridden = has_builtin and has_workspace  # workspace overrides builtin

    return request.app.state.templates.TemplateResponse(request, "skill_detail.html", {"name": name,
        "source": source,
        "description": meta.get("description", "") if meta else "",
        "content": content,
        "has_builtin": has_builtin,
        "has_workspace": has_workspace,
        "is_overridden": is_overridden})


@router.post("/skills/{name}/override")
async def override_skill(request: Request, name: str):
    """Copy builtin skill to workspace for customization."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    builtin_path = loader.builtin_skills / name / "SKILL.md"
    if not builtin_path.exists():
        return JSONResponse({"success": False, "error": "Builtin skill not found"}, status_code=404)

    workspace_skill_dir = loader.workspace_skills / name
    workspace_path = workspace_skill_dir / "SKILL.md"

    if workspace_path.exists():
        return JSONResponse({"success": False, "error": "Workspace override already exists"})

    # Copy entire skill directory (SKILL.md + any scripts/resources)
    import shutil
    builtin_dir = loader.builtin_skills / name
    shutil.copytree(str(builtin_dir), str(workspace_skill_dir))

    logger.info("[Skills] Created workspace override for '{}' at {}", name, workspace_skill_dir)
    return JSONResponse({"success": True, "message": f"Skill '{name}' copied to workspace for editing"})


@router.post("/skills/{name}/restore")
async def restore_skill(request: Request, name: str):
    """Remove workspace override, restoring the builtin version."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    builtin_path = loader.builtin_skills / name / "SKILL.md"
    workspace_skill_dir = loader.workspace_skills / name

    if not builtin_path.exists():
        return JSONResponse({"success": False, "error": "No builtin skill to restore to"}, status_code=400)

    if not workspace_skill_dir.exists():
        return JSONResponse({"success": False, "error": "No workspace override to remove"})

    # Remove workspace override directory
    import shutil
    shutil.rmtree(str(workspace_skill_dir))

    logger.info("[Skills] Restored builtin '{}' (removed workspace override)", name)
    return JSONResponse({"success": True, "message": f"Skill '{name}' restored to builtin version"})


@router.post("/skills/{name}/delete")
async def delete_skill(request: Request, name: str):
    """Delete a workspace-only skill (not a builtin override)."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    workspace_skill_dir = loader.workspace_skills / name
    if not workspace_skill_dir.exists():
        return JSONResponse({"success": False, "error": "Skill not found in workspace"}, status_code=404)

    # Prevent deleting builtin overrides (use restore instead)
    builtin_path = loader.builtin_skills / name / "SKILL.md"
    if builtin_path.exists():
        return JSONResponse({"success": False, "error": "This is a builtin override. Use 'Restore Builtin' instead."}, status_code=400)

    import shutil
    shutil.rmtree(str(workspace_skill_dir))

    logger.info("[Skills] Deleted workspace skill '{}'", name)
    return JSONResponse({"success": True, "message": f"Skill '{name}' deleted"})


@router.post("/skills/{name}/save")
async def save_skill(request: Request, name: str):
    """Save edited SKILL.md content for a workspace skill."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    workspace_path = loader.workspace_skills / name / "SKILL.md"
    if not workspace_path.exists():
        return JSONResponse({"success": False, "error": "Only workspace skills can be edited"}, status_code=400)

    body = await request.json()
    content = body.get("content", "")
    if not content.strip():
        return JSONResponse({"success": False, "error": "Content cannot be empty"})

    workspace_path.write_text(content, encoding="utf-8")
    logger.info("[Skills] Saved workspace skill '{}'", name)
    return JSONResponse({"success": True})


# ============================================================================
# ClawHub — Skill Marketplace
# ============================================================================

import asyncio
import re
import shutil as _shutil


def _find_npx() -> str | None:
    """Find npx binary."""
    return _shutil.which('npx')


async def _run_clawhub(*args: str, workdir: str | None = None) -> tuple[int, str]:
    """Run clawhub CLI command and return (exit_code, output)."""
    parts = ['npx', '--yes', 'clawhub@latest', *args]
    if workdir:
        parts.extend(['--workdir', workdir])
    cmd = ' '.join(parts)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode('utf-8', errors='replace').strip()
        # Filter Node.js noise from stderr
        err_text = stderr.decode('utf-8', errors='replace').strip()
        err_text = '\n'.join(l for l in err_text.splitlines() if 'ExperimentalWarning' not in l and 'trace-warnings' not in l).strip()
        if proc.returncode != 0 and not output:
            output = err_text
        return proc.returncode, output
    except asyncio.TimeoutError:
        return 1, 'Command timed out (120s) — check your network connection'
    except Exception as e:
        return 1, str(e)


def _parse_search_results(output: str) -> list[dict]:
    """Parse clawhub search output: 'slug  Name  (score)' per line."""
    results = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Match: slug  Name  (score)
        m = re.match(r'^(\S+)\s+(.+?)\s+\(([0-9.]+)\)$', line)
        if m:
            results.append({
                'slug': m.group(1),
                'name': m.group(2).strip(),
                'score': float(m.group(3)),
            })
    return results


@router.get('/skills/hub/search')
async def hub_search(request: Request, q: str = ''):
    """Search ClawHub registry via REST API."""
    if not q.strip():
        return JSONResponse({'success': True, 'results': []})

    import urllib.request
    import json as _json

    try:
        url = f'https://clawhub.ai/api/search?q={urllib.parse.quote(q)}&limit=12'
        req = urllib.request.Request(url, headers={'User-Agent': 'nanobot'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
    except Exception as e:
        return JSONResponse({'success': False, 'error': f'ClawHub API error: {e}'})

    results = []
    for r in data.get('results', []):
        results.append({
            'slug': r.get('slug', ''),
            'name': r.get('displayName', r.get('slug', '')),
            'summary': r.get('summary', ''),
            'score': round(r.get('score', 0), 1),
        })

    # Mark which ones are already installed
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)
    installed = {s['name'] for s in loader.list_skills(filter_unavailable=False)}
    for r in results:
        r['installed'] = r['slug'] in installed

    return JSONResponse({'success': True, 'results': results})


@router.post('/skills/hub/install')
async def hub_install(request: Request):
    """Install a skill from ClawHub."""
    body = await request.json()
    slug = body.get('slug', '').strip()
    if not slug:
        return JSONResponse({'success': False, 'error': 'No slug provided'})

    if not _find_npx():
        return JSONResponse({'success': False, 'error': 'npx not found'}, status_code=500)

    workspace = _get_workspace(request)
    code, output = await _run_clawhub('install', slug, workdir=str(workspace))
    if code != 0:
        return JSONResponse({'success': False, 'error': output or 'Install failed'})

    logger.info('[Skills Hub] Installed skill "{}" from ClawHub', slug)
    return JSONResponse({'success': True, 'message': f'Skill "{slug}" installed'})


@router.post('/skills/hub/uninstall')
async def hub_uninstall(request: Request):
    """Uninstall a ClawHub skill (remove from workspace)."""
    body = await request.json()
    slug = body.get('slug', '').strip()
    if not slug:
        return JSONResponse({'success': False, 'error': 'No slug provided'})

    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)
    skill_dir = loader.workspace_skills / slug

    if not skill_dir.exists():
        return JSONResponse({'success': False, 'error': f'Skill "{slug}" not found in workspace'})

    # Only allow uninstalling workspace skills (not builtin)
    builtin_path = loader.builtin_skills / slug
    if builtin_path.exists():
        return JSONResponse({'success': False, 'error': f'Cannot uninstall builtin skill "{slug}"'})

    import shutil
    shutil.rmtree(str(skill_dir))
    logger.info('[Skills Hub] Uninstalled skill "{}"', slug)
    return JSONResponse({'success': True, 'message': f'Skill "{slug}" uninstalled'})


@router.post('/skills/hub/upload')
async def hub_upload(request: Request):
    """Install a skill from uploaded zip file."""
    import zipfile
    import json as _json
    import io
    import re

    form = await request.form()
    upload = form.get('file')
    if not upload:
        return JSONResponse({'success': False, 'error': 'No file uploaded'})

    # Read file into memory
    data = await upload.read()

    # Size limit: 10MB
    if len(data) > 10 * 1024 * 1024:
        return JSONResponse({'success': False, 'error': 'File too large (max 10MB)'})

    # Verify it is a valid zip
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return JSONResponse({'success': False, 'error': 'Invalid zip file'})

    names = zf.namelist()

    # SECURITY: Check for path traversal
    for name in names:
        if name.startswith('/') or '..' in name:
            return JSONResponse({'success': False, 'error': f'Unsafe path detected: {name}'})

    # Must contain SKILL.md at root level
    if 'SKILL.md' not in names:
        return JSONResponse({'success': False, 'error': 'Not a valid skill package: SKILL.md not found at root'})

    # SECURITY: Block executable/script files that are NOT part of known skill patterns
    BLOCKED_EXTENSIONS = {'.exe', '.bat', '.cmd', '.msi', '.dll', '.so', '.dylib', '.com', '.scr', '.pif'}
    for name in names:
        ext = Path(name).suffix.lower()
        if ext in BLOCKED_EXTENSIONS:
            return JSONResponse({'success': False, 'error': f'Blocked file type: {name}'})

    # Determine skill slug from _meta.json or SKILL.md frontmatter
    slug = None
    if '_meta.json' in names:
        try:
            meta = _json.loads(zf.read('_meta.json').decode('utf-8'))
            slug = meta.get('slug', '').strip()
        except Exception:
            pass

    if not slug:
        # Try to extract name from SKILL.md frontmatter
        skill_md = zf.read('SKILL.md').decode('utf-8', errors='replace')
        m = re.search(r'^name:\s*(.+)$', skill_md, re.MULTILINE)
        if m:
            slug = m.group(1).strip().lower().replace(' ', '-')
            slug = re.sub(r'[^a-z0-9_-]', '', slug)

    if not slug:
        return JSONResponse({'success': False, 'error': 'Cannot determine skill name from zip'})

    # Extract to workspace/skills/<slug>/
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)
    target_dir = loader.workspace_skills / slug

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Extract all files
    for member in names:
        target_path = target_dir / member
        if member.endswith('/'):
            target_path.mkdir(parents=True, exist_ok=True)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(zf.read(member))

    logger.info('[Skills Hub] Installed skill "{}" from uploaded zip ({} files)', slug, len(names))
    return JSONResponse({'success': True, 'message': f'Skill "{slug}" installed from zip', 'slug': slug})


# ── MCP Server Management ─────────────────────────────────────────

@router.post("/skills/mcp/add")
async def mcp_add(request: Request):
    """Add a new MCP server to config."""
    from nanobot.config.loader import load_config, save_config
    from nanobot.config.schema import MCPServerConfig

    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"success": False, "error": "Server name is required"})
    # Sanitize name
    import re
    name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    if not name:
        return JSONResponse({"success": False, "error": "Invalid name (use letters, numbers, - or _)"})

    cfg = load_config()
    if name in cfg.tools.mcp_servers:
        return JSONResponse({"success": False, "error": f"Server '{name}' already exists"})

    srv = MCPServerConfig(
        type=body.get("type") or None,
        command=body.get("command", ""),
        args=body.get("args", []),
        env=body.get("env", {}),
        url=body.get("url", ""),
        headers=body.get("headers", {}),
        tool_timeout=int(body.get("tool_timeout", 30)),
        enabled_tools=body.get("enabled_tools", ["*"]),
    )

    # Validate: must have command (stdio) or url (sse/http)
    if not srv.command and not srv.url:
        return JSONResponse({"success": False, "error": "Either command (stdio) or url (sse/http) is required"})

    cfg.tools.mcp_servers[name] = srv
    save_config(cfg)
    logger.info("[MCP] Added server '{}' ({})", name, srv.type or "auto")
    return JSONResponse({"success": True, "message": f"MCP server '{name}' added. Restart gateway to connect."})


@router.post("/skills/mcp/{name}/edit")
async def mcp_edit(request: Request, name: str):
    """Edit an existing MCP server config."""
    from nanobot.config.loader import load_config, save_config

    cfg = load_config()
    if name not in cfg.tools.mcp_servers:
        return JSONResponse({"success": False, "error": f"Server '{name}' not found"}, status_code=404)

    body = await request.json()
    srv = cfg.tools.mcp_servers[name]

    if "command" in body:
        srv.command = body["command"]
    if "args" in body:
        srv.args = body["args"]
    if "env" in body:
        srv.env = body["env"]
    if "url" in body:
        srv.url = body["url"]
    if "headers" in body:
        srv.headers = body["headers"]
    if "tool_timeout" in body:
        srv.tool_timeout = int(body["tool_timeout"])
    if "enabled_tools" in body:
        srv.enabled_tools = body["enabled_tools"]
    if "type" in body:
        srv.type = body["type"] or None

    save_config(cfg)
    logger.info("[MCP] Updated server '{}'", name)
    return JSONResponse({"success": True, "message": f"MCP server '{name}' updated. Restart gateway to apply."})


@router.post("/skills/mcp/{name}/delete")
async def mcp_delete(request: Request, name: str):
    """Delete an MCP server from config."""
    from nanobot.config.loader import load_config, save_config

    cfg = load_config()
    if name not in cfg.tools.mcp_servers:
        return JSONResponse({"success": False, "error": f"Server '{name}' not found"}, status_code=404)

    del cfg.tools.mcp_servers[name]
    save_config(cfg)
    logger.info("[MCP] Deleted server '{}'", name)
    return JSONResponse({"success": True, "message": f"MCP server '{name}' removed. Restart gateway to apply."})


@router.get("/skills/mcp/status")
async def mcp_status(request: Request):
    """Return connection status for each configured MCP server.

    Checks which servers have tools registered in the agent's tool registry.
    A server is 'connected' if at least 1 tool with prefix mcp_{name}_ exists.
    Returns: { "browser": { "connected": true, "tool_count": 22 }, ... }
    """
    from nanobot.config.loader import load_config

    cfg = load_config()
    result = {}

    # Get registered tool names from live agent (if available)
    registered_tool_names: list[str] = []
    agent = getattr(request.app.state, "agent", None)
    if agent and hasattr(agent, "tools"):
        try:
            registered_tool_names = list(agent.tools.tool_names)
        except Exception:
            pass

    for name in cfg.tools.mcp_servers:
        prefix = f"mcp_{name}_"
        matching = [t for t in registered_tool_names if t.startswith(prefix)]

        if len(matching) > 0:
            state = "connected"
        elif agent is None:
            # No agent info available at all
            state = "unknown"
        else:
            # Check if agent even knows about this server
            # If not in agent._mcp_servers, it was added after gateway start → pending restart
            agent_mcp = getattr(agent, "_mcp_servers", {})
            if name not in agent_mcp:
                state = "pending"   # added after startup, not yet tried
            else:
                state = "failed"    # agent tried but failed to connect

        result[name] = {
            "state": state,
            "connected": state == "connected",
            "tool_count": len(matching),
        }

    return JSONResponse(result)
