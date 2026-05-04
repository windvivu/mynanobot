"""OAuth login routes for OpenAI Codex (manual URL flow) and GitHub Copilot (device flow)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

router = APIRouter()

# ---------------------------------------------------------------------------
# In-memory session store for active OAuth/device flows
# { session_id: {"type": "codex"|"copilot", "status": ..., "data": ..., "expires": int} }
# ---------------------------------------------------------------------------
_sessions: dict[str, dict[str, Any]] = {}
_SESSION_TTL = 600  # 10 minutes


def _prune_sessions() -> None:
    now = time.time()
    expired = [k for k, v in _sessions.items() if v.get("expires", 0) < now]
    for k in expired:
        del _sessions[k]


def _new_session(typ: str, data: dict[str, Any]) -> str:
    import uuid
    _prune_sessions()
    sid = uuid.uuid4().hex
    _sessions[sid] = {"type": typ, "status": "pending", "data": data, "expires": time.time() + _SESSION_TTL}
    return sid


# ===========================================================================
# OpenAI Codex — Manual URL Flow
# ===========================================================================

def _build_codex_auth_url() -> tuple[str, str, str]:
    """Build PKCE authorization URL for Codex. Returns (url, verifier, state)."""
    import base64
    import hashlib
    import os
    import urllib.parse

    from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX

    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    params = {
        "response_type": "code",
        "client_id": _CODEX.client_id,
        "redirect_uri": _CODEX.redirect_uri,
        "scope": _CODEX.scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": _CODEX.default_originator,
    }
    url = f"{_CODEX.authorize_url}?{urllib.parse.urlencode(params)}"
    return url, verifier, state


async def _exchange_codex_code(code: str, verifier: str) -> dict[str, Any]:
    """Exchange authorization code for token. Returns token dict."""
    import httpx
    from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX
    from oauth_cli_kit.storage import FileTokenStorage

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _CODEX.redirect_uri,
        "client_id": _CODEX.client_id,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(_CODEX.token_url, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"Token exchange failed: HTTP {r.status_code} — {r.text[:300]}")
        token_data = r.json()

    # Determine account_id from JWT claim
    import base64, json as _json
    account_id = ""
    id_token = token_data.get("id_token", "")
    if id_token:
        try:
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
            account_id = payload.get("chatgpt_account_id", "") or payload.get("sub", "")
        except Exception:
            pass

    # Save token via oauth-cli-kit storage
    from oauth_cli_kit.models import OAuthToken
    from oauth_cli_kit.storage import FileTokenStorage
    from nanobot.config.paths import get_data_dir
    expires_in = token_data.get("expires_in", 3600)
    token = OAuthToken(
        access=token_data.get("access_token", ""),
        refresh=token_data.get("refresh_token", ""),
        expires=int(time.time()) + expires_in,
        account_id=account_id,
    )
    storage = FileTokenStorage("codex.json", data_dir=get_data_dir())
    storage.save(token)
    logger.info("[OAuth/Codex] Token saved. account_id={}", account_id)
    return {"account_id": account_id, "expires_in": expires_in}


@router.get("/oauth/codex/status")
async def codex_status() -> JSONResponse:
    """Check if Codex token exists and is still valid."""
    try:
        from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX
        from oauth_cli_kit.storage import FileTokenStorage
        from nanobot.config.paths import get_data_dir
        storage = FileTokenStorage(_CODEX.token_filename, data_dir=get_data_dir())
        token = storage.load()
        if token and token.expires > time.time() + 60:
            return JSONResponse({"logged_in": True, "account_id": token.account_id or ""})
        return JSONResponse({"logged_in": False})
    except Exception as e:
        logger.debug("[OAuth/Codex] Status check error: {}", e)
        return JSONResponse({"logged_in": False})


@router.post("/oauth/codex/start")
async def codex_start() -> JSONResponse:
    """Generate Codex OAuth URL for manual flow. Returns url + session_id."""
    try:
        url, verifier, state = _build_codex_auth_url()
        from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX
        sid = _new_session("codex", {"verifier": verifier, "state": state, "redirect_uri": _CODEX.redirect_uri})
        logger.info("[OAuth/Codex] Started manual flow, session={}", sid)
        return JSONResponse({"success": True, "url": url, "session_id": sid})
    except Exception as e:
        logger.error("[OAuth/Codex] Start error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/oauth/codex/callback")
async def codex_callback(request: Request) -> JSONResponse:
    """Accept the callback URL pasted by user, extract code and exchange for token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"})

    session_id = (body.get("session_id") or "").strip()
    callback_url = (body.get("callback_url") or "").strip()

    if not session_id or session_id not in _sessions:
        return JSONResponse({"success": False, "error": "Session expired or invalid. Please restart login."})

    sess = _sessions[session_id]
    if sess["type"] != "codex":
        return JSONResponse({"success": False, "error": "Wrong session type"})

    # Parse callback URL
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(callback_url)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state_recv = params.get("state", [None])[0]
    except Exception:
        return JSONResponse({"success": False, "error": "Cannot parse callback URL"})

    if not code:
        return JSONResponse({"success": False, "error": "No authorization code found in URL"})

    if state_recv and state_recv != sess["data"]["state"]:
        return JSONResponse({"success": False, "error": "State mismatch — possible CSRF. Please restart."})

    try:
        result = await _exchange_codex_code(code, sess["data"]["verifier"])
        del _sessions[session_id]
        return JSONResponse({"success": True, "account_id": result.get("account_id", "")})
    except Exception as e:
        logger.error("[OAuth/Codex] Token exchange error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/oauth/codex/logout")
async def codex_logout() -> JSONResponse:
    """Delete cached Codex token."""
    try:
        from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX
        from oauth_cli_kit.storage import FileTokenStorage
        from nanobot.config.paths import get_data_dir
        storage = FileTokenStorage(_CODEX.token_filename, data_dir=get_data_dir())
        token_path = storage.get_token_path()
        if token_path.exists():
            token_path.unlink()
        logger.info("[OAuth/Codex] Token deleted")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


# ===========================================================================
# GitHub Copilot — Device Authorization Flow
# ===========================================================================

_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # GitHub Copilot CLI client ID (public)
_COPILOT_DEVICE_URL = "https://github.com/login/device/code"
_COPILOT_TOKEN_URL = "https://github.com/login/oauth/access_token"
_COPILOT_API_URL = "https://api.githubcopilot.com"
_COPILOT_TOKEN_FILE = "github_copilot.json"


def _save_copilot_token(access_token: str) -> None:
    """Persist GitHub Copilot access token to disk."""
    import json
    from pathlib import Path
    from nanobot.config.paths import get_data_dir
    token_path = get_data_dir() / _COPILOT_TOKEN_FILE
    token_path.write_text(json.dumps({"access_token": access_token, "saved_at": int(time.time())}), encoding="utf-8")
    logger.info("[OAuth/Copilot] Token saved to {}", token_path)


def _load_copilot_token() -> str | None:
    """Load GitHub Copilot access token from disk."""
    import json
    from pathlib import Path
    from nanobot.config.paths import get_data_dir
    token_path = get_data_dir() / _COPILOT_TOKEN_FILE
    if not token_path.exists():
        return None
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
        return data.get("access_token")
    except Exception:
        return None


def _delete_copilot_token() -> None:
    from nanobot.config.paths import get_data_dir
    token_path = get_data_dir() / _COPILOT_TOKEN_FILE
    if token_path.exists():
        token_path.unlink()


@router.get("/oauth/copilot/status")
async def copilot_status() -> JSONResponse:
    """Check if Copilot token exists."""
    token = _load_copilot_token()
    if token:
        return JSONResponse({"logged_in": True})
    return JSONResponse({"logged_in": False})


@router.post("/oauth/copilot/start")
async def copilot_start() -> JSONResponse:
    """Start GitHub Copilot device flow. Returns user_code and verification_uri."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _COPILOT_DEVICE_URL,
                headers={"Accept": "application/json"},
                data={"client_id": _COPILOT_CLIENT_ID, "scope": "read:user"},
            )
            if r.status_code != 200:
                return JSONResponse({"success": False, "error": f"GitHub returned HTTP {r.status_code}"})
            data = r.json()

        device_code = data.get("device_code")
        user_code = data.get("user_code")
        verification_uri = data.get("verification_uri", "https://github.com/login/device")
        interval = data.get("interval", 5)
        expires_in = data.get("expires_in", 900)

        if not device_code or not user_code:
            return JSONResponse({"success": False, "error": "No device_code/user_code from GitHub"})

        sid = _new_session("copilot", {
            "device_code": device_code,
            "interval": interval,
            "expires_in": expires_in,
        })
        _sessions[sid]["expires"] = time.time() + expires_in

        logger.info("[OAuth/Copilot] Device flow started, user_code={}", user_code)
        return JSONResponse({
            "success": True,
            "session_id": sid,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_in": expires_in,
            "interval": interval,
        })
    except Exception as e:
        logger.error("[OAuth/Copilot] Start error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/oauth/copilot/poll")
async def copilot_poll(request: Request) -> JSONResponse:
    """Poll GitHub token endpoint. Frontend calls this repeatedly until done."""
    import httpx
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"})

    session_id = (body.get("session_id") or "").strip()
    if not session_id or session_id not in _sessions:
        return JSONResponse({"success": False, "done": True, "error": "Session expired. Please restart."})

    sess = _sessions[session_id]
    device_code = sess["data"]["device_code"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _COPILOT_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": _COPILOT_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            data = r.json()

        error = data.get("error")
        if error == "authorization_pending":
            return JSONResponse({"success": False, "done": False, "pending": True})
        if error == "slow_down":
            return JSONResponse({"success": False, "done": False, "pending": True, "slow_down": True})
        if error in ("expired_token", "access_denied"):
            del _sessions[session_id]
            return JSONResponse({"success": False, "done": True, "error": f"Login failed: {error}"})
        if error:
            return JSONResponse({"success": False, "done": False, "error": error})

        access_token = data.get("access_token")
        if access_token:
            _save_copilot_token(access_token)
            del _sessions[session_id]
            logger.info("[OAuth/Copilot] Token received and saved")
            return JSONResponse({"success": True, "done": True})

        return JSONResponse({"success": False, "done": False, "pending": True})
    except Exception as e:
        logger.error("[OAuth/Copilot] Poll error: {}", e)
        return JSONResponse({"success": False, "done": False, "error": str(e)})


@router.post("/oauth/copilot/logout")
async def copilot_logout() -> JSONResponse:
    """Delete cached Copilot token."""
    try:
        _delete_copilot_token()
        logger.info("[OAuth/Copilot] Token deleted")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})
