"""Config editor route — view and edit config.json."""

import copy
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from loguru import logger

router = APIRouter()


def _safe_restart_next_url(next_url: str) -> str:
    """Keep restart redirects inside the dashboard origin."""
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url

# ── Tested models cache ──────────────────────────────────


def _get_tested_models_path():
    """Get path to tested_models.json in user data dir."""
    from nanobot.config.paths import get_data_dir
    return get_data_dir() / "tested_models.json"


def _load_tested_models() -> dict:
    """Load tested models cache from disk."""
    path = _get_tested_models_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_tested_model(provider: str, model: str) -> None:
    """Save a tested model name for a provider."""
    models = _load_tested_models()
    if provider not in models:
        models[provider] = []
    if model not in models[provider]:
        models[provider].append(model)
    path = _get_tested_models_path()
    path.write_text(json.dumps(models, indent=2), encoding="utf-8")


@router.post("/config/test-provider")
async def test_provider(request: Request):
    """Test API key + model by sending a short prompt via the appropriate SDK."""
    from nanobot.providers.registry import find_by_name

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "key_valid": False, "error": "Invalid request body"})

    model = (body.get("model") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    api_base = (body.get("api_base") or "").strip() or None
    provider_name = (body.get("provider") or "").strip()

    if not model:
        return JSONResponse({
            "success": False, "key_valid": False,
            "error": "Bạn chưa nhập model name. Hãy điền tên model trước khi test.",
        })

    _OAUTH_PROVIDERS = {"openai_codex", "github_copilot"}

    # OAuth providers: no API key needed — use their own token mechanism
    if provider_name in _OAUTH_PROVIDERS:
        return await _test_oauth_provider(model, provider_name)

    if not api_key:
        return JSONResponse({"success": False, "key_valid": False, "error": "API key is required"})

    logger.info("[Config Test] Testing provider={} model={}", provider_name, model)

    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # Determine effective base URL: user override → spec default → None
    effective_base = api_base or (spec.default_api_base if spec else None) or None

    # === Anthropic (native Anthropic SDK) ==================================
    if backend == "anthropic":
        return await _test_anthropic_provider(model, api_key, api_base, provider_name)

    # === Azure OpenAI (AzureOpenAI SDK) =====================================
    if backend == "azure_openai":
        return await _test_azure_provider(model, api_key, api_base, provider_name)

    # === All OpenAI-compatible providers (AsyncOpenAI) =====================
    return await _test_openai_compat_provider(model, api_key, effective_base, provider_name)


async def _test_oauth_provider(model: str, provider_name: str) -> JSONResponse:
    """Test OAuth-based providers (openai_codex, github_copilot) using their stored token."""
    try:
        if provider_name == "openai_codex":
            from nanobot.providers.openai_codex_provider import OpenAICodexProvider
            provider = OpenAICodexProvider(default_model=model)
        elif provider_name == "github_copilot":
            from nanobot.providers.github_copilot_provider import GitHubCopilotProvider
            provider = GitHubCopilotProvider(default_model=model)
        else:
            return JSONResponse({"success": False, "key_valid": False, "error": f"Unknown OAuth provider: {provider_name}"})

        response = await provider.chat(
            messages=[{"role": "user", "content": "Say hi in one word."}],
            model=model,
        )

        result_text = (response.content or "").strip()
        logger.info("[Config Test] OAuth provider={} model={} OK: {}", provider_name, model, result_text[:30])
        _save_tested_model(provider_name, model)

        return JSONResponse({
            "success": True,
            "key_valid": True,
            "message": f'OAuth OK \u2014 "{result_text[:80]}"',
        })
    except Exception as e:
        logger.warning("[Config Test] OAuth provider={} model={} failed: {}", provider_name, model, e)
        return JSONResponse({"success": False, "key_valid": False, "error": str(e)})



async def _test_openai_compat_provider(model: str, api_key: str, api_base: str | None, provider_name: str) -> JSONResponse:
    """Test any OpenAI-compatible provider (openai, deepseek, groq, gemini, custom, etc.)."""
    from openai import AsyncOpenAI

    base_url = api_base or None
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    if provider_name == "openrouter" or (base_url and "openrouter" in base_url.lower()):
        headers["HTTP-Referer"] = "https://github.com/HKUDS/nanobot"
        headers["X-OpenRouter-Title"] = "nanobot"
        
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=headers)

    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
            timeout=15,
        )
        if provider_name:
            _save_tested_model(provider_name, model)
        logger.info("[Config Test] OK provider={} model={}", provider_name, model)
        return JSONResponse({"success": True, "key_valid": True, "message": "✓ API key valid, model responds"})
    except Exception as e:
        return _parse_openai_error(e, model, provider_name, api_base or "provider endpoint")


async def _test_anthropic_provider(model: str, api_key: str, api_base: str | None, provider_name: str) -> JSONResponse:
    """Test Anthropic provider using native Anthropic SDK."""
    from anthropic import AsyncAnthropic

    client_kw = {"api_key": api_key}
    if api_base:
        client_kw["base_url"] = api_base
    client = AsyncAnthropic(**client_kw)

    # Strip anthropic/ prefix if present
    bare_model = model[len("anthropic/"):] if model.startswith("anthropic/") else model

    try:
        await client.messages.create(
            model=bare_model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
        )
        if provider_name:
            _save_tested_model(provider_name, model)
        logger.info("[Config Test] OK provider={} model={}", provider_name, model)
        return JSONResponse({"success": True, "key_valid": True, "message": "✓ API key valid, model responds"})
    except Exception as e:
        return _parse_openai_error(e, model, provider_name, "api.anthropic.com")


async def _test_azure_provider(model: str, api_key: str, api_base: str | None, provider_name: str) -> JSONResponse:
    """Test Azure OpenAI provider using AzureOpenAI SDK."""
    from openai import AsyncAzureOpenAI

    if not api_base:
        return JSONResponse({
            "success": False, "key_valid": False,
            "error": "Azure OpenAI requires api_base (e.g. https://your-resource.openai.azure.com/)",
        })

    client = AsyncAzureOpenAI(api_key=api_key, azure_endpoint=api_base, api_version="2024-10-21")

    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
            timeout=15,
        )
        if provider_name:
            _save_tested_model(provider_name, model)
        logger.info("[Config Test] OK provider={} model={}", provider_name, model)
        return JSONResponse({"success": True, "key_valid": True, "message": "✓ API key valid, model responds"})
    except Exception as e:
        return _parse_openai_error(e, model, provider_name, api_base)


def _parse_openai_error(e: Exception, model: str, provider_name: str, endpoint: str) -> JSONResponse:
    """Shared error parser for OpenAI/Anthropic SDK exceptions."""
    err_msg = str(e)
    try:
        err_msg = err_msg.encode("ascii", errors="replace").decode("ascii")
    except Exception:
        err_msg = repr(e)

    if "401" in err_msg or "Unauthorized" in err_msg or "authentication_error" in err_msg:
        logger.warning("[Config Test] FAIL provider={} -- Auth error", provider_name)
        return JSONResponse({"success": False, "key_valid": False, "error": "Invalid API key (authentication failed)"})
    if "404" in err_msg or "not_found" in err_msg or "not found" in err_msg.lower():
        logger.warning("[Config Test] WARN provider={} model={} -- Not found", provider_name, model)
        return JSONResponse({"success": False, "key_valid": True, "error": f"API key valid, but model '{model}' not found"})
    if "connect" in err_msg.lower() or "connection" in err_msg.lower() or "refused" in err_msg.lower():
        logger.warning("[Config Test] FAIL provider={} -- Connection error", provider_name)
        return JSONResponse({"success": False, "key_valid": False, "error": f"Cannot connect to {endpoint} — check API base URL"})

    logger.error("[Config Test] FAIL provider={} -- Error: {}", provider_name, err_msg[:200])
    return JSONResponse({"success": False, "key_valid": False, "error": err_msg[:200]})


@router.get("/config/tested-models")
async def get_tested_models():
    """Return the cached tested models per provider."""
    models = _load_tested_models()
    return JSONResponse(models)


@router.delete("/config/tested-models")
async def delete_tested_model(request: Request):
    """Remove a specific model from a provider's tested list."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not provider or not model:
        return JSONResponse({"success": False, "error": "provider and model required"})

    models = _load_tested_models()
    if provider in models and model in models[provider]:
        models[provider].remove(model)
        if not models[provider]:
            del models[provider]
        path = _get_tested_models_path()
        path.write_text(json.dumps(models, indent=2), encoding="utf-8")
        logger.info("[Config] Removed tested model provider={} model={}", provider, model)
    return JSONResponse({"success": True})


@router.post("/config/save-provider")
async def save_provider(request: Request):
    """Save a single provider's API key and base URL to config."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    provider_name = (body.get("provider") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    api_base = (body.get("api_base") or "").strip()

    if not provider_name:
        return JSONResponse({"success": False, "error": "Provider name is required"})

    config = load_config()
    p = getattr(config.providers, provider_name, None)
    if not p:
        return JSONResponse({"success": False, "error": f"Unknown provider: {provider_name}"})

    p.api_key = api_key if api_key else ""
    p.api_base = api_base if api_base else None
    save_config(config)

    logger.info("[Config] Saved provider={} key={}...{}", provider_name,
                api_key[:4] if len(api_key) > 4 else "****",
                api_key[-4:] if len(api_key) > 4 else "")
    return JSONResponse({"success": True, "message": f"Provider {provider_name} saved"})


@router.post("/config/save-model")
async def save_model(request: Request):
    """Save model name (and optionally provider) to config."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    model = (body.get("model") or "").strip()
    provider = (body.get("provider") or "").strip()
    if not model:
        return JSONResponse({"success": False, "error": "Model name is required"})

    config = load_config()
    config.agents.defaults.model = model
    if provider:
        config.agents.defaults.provider = provider
    save_config(config)

    # Update in-memory config so agent loop uses new settings immediately
    request.app.state.config = config

    logger.info("[Config] Saved model={} provider={}", model, provider or "(unchanged)")
    return JSONResponse({"success": True, "message": f"Model '{model}' saved"})


@router.post("/config/toggle-channel")
async def toggle_channel(request: Request):
    """Enable or disable a channel."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    channel_name = (body.get("channel") or "").strip()
    enabled = bool(body.get("enabled", False))

    if not channel_name:
        return JSONResponse({"success": False, "error": "Channel name is required"})

    config = load_config()
    ch = getattr(config.channels, channel_name, None)
    
    if ch is None:
        # Channel not in config yet, initialize a dict for it
        if not enabled:
            return JSONResponse({"success": True, "message": f"Channel '{channel_name}' remains disabled"})
        setattr(config.channels, channel_name, {"enabled": True})
    elif isinstance(ch, dict):
        ch["enabled"] = enabled
    else:
        ch.enabled = enabled
        
    save_config(config)
    request.app.state.config = config

    action = "enabled" if enabled else "disabled"
    logger.info("[Config] Channel {} {}", channel_name, action)
    return JSONResponse({"success": True, "message": f"Channel '{channel_name}' {action}"})


@router.post("/config/check-path")
async def check_path(request: Request):
    """Check if a directory path is valid (syntax) or exists on disk."""
    import re
    from pathlib import Path
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"valid": False, "error": "Invalid JSON"}, status_code=400)
    path_str = (body.get("path") or "").strip()
    mode = body.get("mode", "exists")  # "syntax" or "exists"
    if not path_str:
        return JSONResponse({"valid": False, "error": "Đường dẫn trống"})
    # Syntax check: valid path format
    try:
        p = Path(path_str).expanduser().resolve()
        if not p.is_absolute():
            return JSONResponse({"valid": False, "error": "Đường dẫn không hợp lệ (không phải absolute path)"})
    except Exception:
        return JSONResponse({"valid": False, "error": "Cú pháp đường dẫn không hợp lệ"})
    if mode == "syntax":
        # Validate just the folder name portion (not the full resolved path)
        name = (body.get("name") or path_str).strip()
        if not re.match(r'^[a-zA-Z0-9_\-./\\]+$', name):
            return JSONResponse({"valid": False, "error": "Tên thư mục chứa ký tự không hợp lệ (chỉ cho phép chữ, số, -, _, .)" })
        if '//' in name or '\\\\' in name or name.startswith('.') or name.endswith('.'):
            return JSONResponse({"valid": False, "error": "Tên thư mục không hợp lệ"})
        return JSONResponse({"valid": True, "message": "Cú pháp hợp lệ", "exists": p.exists()})
    # Exists check
    if p.exists():
        if p.is_dir():
            return JSONResponse({"valid": True, "message": "Thư mục tồn tại"})
        else:
            return JSONResponse({"valid": False, "error": "Đường dẫn tồn tại nhưng không phải thư mục"})
    return JSONResponse({"valid": False, "error": "Thư mục không tồn tại"})


@router.post("/config/test-brave-key")
async def test_brave_key(request: Request):
    """Test a Brave Search API key by making a minimal search query."""
    import httpx
    from nanobot.config.loader import load_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return JSONResponse({"success": False, "error": "API key is empty"})

    # Use proxy from current config if available
    config = load_config()
    proxy = getattr(config.tools.web, "proxy", None) or None

    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=10.0) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": "test", "count": 1},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key,
                },
            )
        if r.status_code == 200:
            results = r.json().get("web", {}).get("results", [])
            return JSONResponse({
                "success": True,
                "message": f"Key hợp lệ! ({len(results)} result)",
            })
        elif r.status_code in (401, 403):
            return JSONResponse({"success": False, "error": f"Key không hợp lệ (HTTP {r.status_code})"})
        else:
            return JSONResponse({"success": False, "error": f"Brave API trả về HTTP {r.status_code}"})
    except httpx.ProxyError as e:
        return JSONResponse({"success": False, "error": f"Proxy error: {e}"})
    except Exception as e:
        logger.error("[Config] Test Brave key failed: {}", e)
        return JSONResponse({"success": False, "error": f"Connection error: {e}"})


@router.post("/config/test-telegram-token")
async def test_telegram_token(request: Request):
    """Test a Telegram bot token by calling getMe API."""
    import httpx
    from nanobot.config.loader import load_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    token = (body.get("token") or "").strip()
    if not token:
        return JSONResponse({"success": False, "error": "Token is empty"})

    # Use proxy from current config if available
    config = load_config()
    tg = getattr(config.channels, "telegram", None)
    proxy_url = tg.get("proxy") if isinstance(tg, dict) else getattr(tg, "proxy", None)

    try:
        async with httpx.AsyncClient(proxy=proxy_url or None, timeout=10.0) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")

        if r.status_code == 200:
            data = r.json()
            bot = data.get("result", {})
            bot_name = bot.get("first_name", "")
            username = bot.get("username", "")
            return JSONResponse({
                "success": True,
                "message": f"Token hợp lệ! Bot: {bot_name} (@{username})",
                "bot_name": bot_name,
                "username": username,
            })
        elif r.status_code == 401:
            return JSONResponse({"success": False, "error": "Token không hợp lệ (401 Unauthorized)"})
        else:
            return JSONResponse({"success": False, "error": f"Telegram API trả về HTTP {r.status_code}"})
    except httpx.ProxyError as e:
        return JSONResponse({"success": False, "error": f"Proxy error: {e}"})
    except Exception as e:
        logger.error("[Config] Test Telegram token failed: {}", e)
        return JSONResponse({"success": False, "error": f"Connection error: {e}"})

# Provider names in display order
_PROVIDER_NAMES = [
    "openai", "anthropic", "openrouter", "deepseek", "groq", "gemini",
    "custom", "azure_openai", "zhipu", "dashscope", "vllm", "ollama", "moonshot",
    "minimax", "mistral", "aihubmix", "siliconflow",
    "volcengine", "volcengine_coding_plan", "byteplus", "byteplus_coding_plan",
    "ovms", "openai_codex", "github_copilot",
]

# Channel names in display order
_CHANNEL_NAMES = [
    "telegram", "discord", "slack", "whatsapp", "feishu", "dingtalk",
    "mochat", "email", "qq", "matrix", "wecom", "weixin", "zalo",
]


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, saved: str | None = None):
    """Render the configuration editor page."""
    app_state = request.app.state
    config = app_state.config

    defaults = config.agents.defaults
    gateway = config.gateway
    heartbeat = gateway.heartbeat
    web_cfg = gateway.web

    # Providers data
    providers_data = []
    for name in _PROVIDER_NAMES:
        p = getattr(config.providers, name, None)
        if p:
            providers_data.append({
                "name": name,
                "api_key": p.api_key or "",
                "api_base": p.api_base or "",
                "has_key": bool(p.api_key),
            })

    # Channels data (only enabled)
    channels_data = []
    for name in _CHANNEL_NAMES:
        ch = getattr(config.channels, name, None)
        if ch and (ch.get("enabled", False) if isinstance(ch, dict) else getattr(ch, "enabled", False)):
            fields = {}
            if isinstance(ch, dict):
                for k, v in ch.items():
                    if k == "enabled":
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        fields[k] = v
                    elif isinstance(v, list):
                        fields[k] = ", ".join(str(x) for x in v)
            else:
                for field_name in ch.model_fields:
                    if field_name in ("enabled",):
                        continue
                    val = getattr(ch, field_name)
                    if isinstance(val, (str, int, float, bool)):
                        fields[field_name] = val
                    elif isinstance(val, list):
                        fields[field_name] = ", ".join(str(v) for v in val)
            channels_data.append({"name": name, "fields": fields})

    # Disabled channels (for enable dropdown)
    # Includes: channels with enabled=False AND channels not yet in config (ch=None)
    disabled_channels = []
    for name in _CHANNEL_NAMES:
        ch = getattr(config.channels, name, None)
        if ch is None:
            # Not yet configured → show in enable dropdown so user can set it up
            disabled_channels.append(name)
        elif not (ch.get("enabled", False) if isinstance(ch, dict) else getattr(ch, "enabled", False)):
            disabled_channels.append(name)

    # Tools data
    tools_config = config.tools
    channels_config = config.channels

    context = {
        "request": request,
        # Agent defaults
        "model": defaults.model,
        "provider": defaults.provider,
        "temperature": defaults.temperature,
        "max_tokens": defaults.max_tokens,
        "max_tool_iterations": defaults.max_tool_iterations,
        "context_window_tokens": defaults.context_window_tokens,
        "reasoning_effort": defaults.reasoning_effort or "",
        # Heartbeat
        "heartbeat_enabled": heartbeat.enabled,
        "heartbeat_interval": heartbeat.interval_s,
        # Web
        "web_port": web_cfg.port,
        "web_password": web_cfg.password,
        # Providers
        "providers": providers_data,
        # Channels
        "channels": channels_data,
        "disabled_channels": disabled_channels,
        "send_progress": channels_config.send_progress,
        "send_tool_hints": channels_config.send_tool_hints,
        "debounce_seconds": channels_config.debounce_seconds,
        # Tools
        "exec_timeout": tools_config.exec.timeout,
        "exec_path_append": tools_config.exec.path_append,
        "web_proxy": tools_config.web.proxy or "",
        "search_api_key": tools_config.web.search.api_key or "",
        "sandbox_mode": tools_config.sandbox_mode,
        "workspace": str(config.workspace_path),
        "workspace_subdir": tools_config.exec.workspace_subdir,
        "allowed_dirs": tools_config.exec.allowed_dirs,
        "tool_preset": tools_config.tool_preset,
        "enable_file_tools": tools_config.enable_file_tools,
        "enable_web_tools": tools_config.enable_web_tools,
        "enable_spawn": tools_config.enable_spawn,
        "enable_cron": tools_config.enable_cron,
        "enable_mcp": tools_config.enable_mcp,
        # Flash
        "saved": saved == "1",
    }

    return app_state.templates.TemplateResponse(request, "config.html", context)


@router.post("/config")
async def config_save(request: Request):
    """Save configuration changes."""
    from nanobot.config.loader import get_config_path, load_config, save_config

    form = await request.form()

    try:
        config_path = get_config_path()

        # Create backup before saving
        backup_path = config_path.with_suffix(".json.bak")
        if config_path.exists():
            import shutil
            shutil.copy2(config_path, backup_path)
            logger.info("[Web Config] Backup: {}", backup_path)

        # Reload config from disk
        config = load_config(config_path)

        # --- Agent defaults ---
        field_map = {
            "model": (config.agents.defaults, "model", str),
            "provider": (config.agents.defaults, "provider", str),
            "temperature": (config.agents.defaults, "temperature", float),
            "max_tokens": (config.agents.defaults, "max_tokens", int),
            "max_tool_iterations": (config.agents.defaults, "max_tool_iterations", int),
            "context_window_tokens": (config.agents.defaults, "context_window_tokens", int),
            "reasoning_effort": (config.agents.defaults, "reasoning_effort", str),
        }

        for form_key, (obj, attr, typ) in field_map.items():
            value = form.get(form_key)
            if value is not None:
                value = value.strip()
                if not value:
                    continue  # skip empty values
                if typ == float:
                    parsed = float(value)
                elif typ == int:
                    parsed = int(value)
                else:
                    parsed = value if value else None
                setattr(obj, attr, parsed)

        # --- Heartbeat ---
        config.gateway.heartbeat.enabled = form.get("heartbeat_enabled") == "on"
        hb_interval = form.get("heartbeat_interval")
        if hb_interval:
            config.gateway.heartbeat.interval_s = int(hb_interval)

        # --- Web config ---
        web_port = form.get("web_port")
        if web_port:
            config.gateway.web.port = int(web_port)
        web_password = form.get("web_password")
        if web_password is not None:
            config.gateway.web.password = web_password.strip()

        # --- Providers ---
        for name in _PROVIDER_NAMES:
            p = getattr(config.providers, name, None)
            if not p:
                continue
            key_val = form.get(f"provider_{name}_api_key")
            if key_val is not None:
                key_val = key_val.strip()
                p.api_key = key_val if key_val else ""
            base_val = form.get(f"provider_{name}_api_base")
            if base_val is not None:
                p.api_base = base_val.strip() or None

        # --- Tools ---
        exec_timeout = form.get("exec_timeout")
        if exec_timeout:
            config.tools.exec.timeout = int(exec_timeout)
        exec_path = form.get("exec_path_append")
        if exec_path is not None:
            config.tools.exec.path_append = exec_path.strip()
        web_proxy = form.get("web_proxy")
        if web_proxy is not None:
            config.tools.web.proxy = web_proxy.strip() or None
        search_key = form.get("search_api_key")
        if search_key is not None:
            search_key = search_key.strip()
            config.tools.web.search.api_key = search_key if search_key else ""

        # Sandbox mode
        sandbox_mode = form.get("sandbox_mode", "unrestricted").strip()
        if sandbox_mode in ("unrestricted", "workspace", "disabled"):
            config.tools.sandbox_mode = sandbox_mode
        workspace_subdir = form.get("workspace_subdir")
        if workspace_subdir is not None:
            config.tools.exec.workspace_subdir = workspace_subdir.strip()
        # Auto-create sandbox/subdir if sandbox mode is on
        if sandbox_mode == "workspace":
            sandbox_dir = config.workspace_path / "sandbox"
            if workspace_subdir and workspace_subdir.strip():
                sandbox_dir = sandbox_dir / workspace_subdir.strip()
            sandbox_dir.mkdir(parents=True, exist_ok=True)
        # allowed_dirs: collect from form (allowed_dir_0, allowed_dir_1, ...)
        allowed_dirs = []
        for key in sorted(form.keys()):
            if key.startswith("allowed_dir_"):
                val = form.get(key, "").strip()
                if val:
                    allowed_dirs.append(val)
        config.tools.exec.allowed_dirs = allowed_dirs

        # Tool preset
        tool_preset = form.get("tool_preset", "developer").strip()
        if tool_preset in ("developer", "chatbot", "custom"):
            config.tools.tool_preset = tool_preset
        config.tools.enable_file_tools = form.get("enable_file_tools") == "on"
        config.tools.enable_web_tools = form.get("enable_web_tools") == "on"
        config.tools.enable_spawn = form.get("enable_spawn") == "on"
        config.tools.enable_cron = form.get("enable_cron") == "on"
        config.tools.enable_mcp = form.get("enable_mcp") == "on"

        # --- Channels global ---
        config.channels.send_progress = form.get("send_progress") == "on"
        config.channels.send_tool_hints = form.get("send_tool_hints") == "on"
        debounce_val = form.get("debounce_seconds")
        if debounce_val is not None:
            config.channels.debounce_seconds = max(0.0, float(debounce_val.strip() or "2.0"))

        # --- Telegram channel ---
        tg = getattr(config.channels, "telegram", None)
        tg_enabled = tg.get("enabled", False) if isinstance(tg, dict) else getattr(tg, "enabled", False)
        if tg and tg_enabled:
            def _tgset(obj, key, val):
                if isinstance(obj, dict):
                    aliases = {
                        "allow_from": "allowFrom",
                        "reply_to_message": "replyToMessage",
                        "group_policy": "groupPolicy",
                    }
                    json_key = aliases.get(key, key)
                    obj[json_key] = val
                    if json_key != key:
                        obj.pop(key, None)
                else:
                    setattr(obj, key, val)
            tg_token = form.get("channel_telegram_token")
            if tg_token is not None:
                tg_token = tg_token.strip()
                if tg_token:
                    _tgset(tg, "token", tg_token)
            tg_allow = form.get("channel_telegram_allow_from")
            if tg_allow is not None:
                raw = [x.strip() for x in tg_allow.split(",") if x.strip()]
                _tgset(tg, "allow_from", raw)
            tg_proxy = form.get("channel_telegram_proxy")
            if tg_proxy is not None:
                _tgset(tg, "proxy", tg_proxy.strip() or None)
            reply_val = form.get("channel_telegram_reply_to_message") == "on"
            _tgset(tg, "reply_to_message", reply_val)
            _tgset(tg, "streaming", form.get("channel_telegram_streaming") == "on")
            tg_group_policy = form.get("channel_telegram_group_policy")
            if tg_group_policy in {"mention", "ambient", "open"}:
                _tgset(tg, "group_policy", tg_group_policy)
            logger.info("[Web Config] Telegram channel config updated")

        # Save to disk
        save_config(config, config_path)
        logger.info("[Web Config] Configuration saved successfully")

        # Update app state
        request.app.state.config = config

        return RedirectResponse(url="/config?saved=1", status_code=302)

    except Exception as e:
        logger.error("[Web Config] Failed to save: {}", e)
        # Re-render with error — delegate to GET with error flash
        return RedirectResponse(url="/config", status_code=302)


@router.post("/config/restart")
async def config_restart(request: Request):
    """Restart the gateway process in-place.

    Uses os.execv() to replace the current process with a new one,
    preserving the same command-line arguments. Works on Windows,
    Linux, and Docker. Uses `-m nanobot` for Windows compatibility
    (sys.argv[0] may not be a full path on Windows).
    """
    import os
    import sys

    logger.info("[Web Config] Gateway restart requested via web dashboard")

    async def _do_restart():
        import asyncio
        await asyncio.sleep(0.5)  # Let the response reach the client
        logger.info("[Web Config] Restarting via os.execv...")
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    import asyncio
    asyncio.create_task(_do_restart())
    return JSONResponse({"success": True, "message": "Gateway is restarting..."})


@router.get("/restarting", response_class=HTMLResponse)
async def restarting_page(request: Request, next: str = "/"):
    """Show a browser-facing restart progress page."""
    return request.app.state.templates.TemplateResponse(
        request,
        "restarting.html",
        {"next_url": _safe_restart_next_url(next)},
    )

