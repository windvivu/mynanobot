"""Agent loop: the core processing engine."""

from __future__ import annotations

from nanobot import __version__

import asyncio
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.memory import Consolidator, Dream, MemoryStore
from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.notebook import NotebookEditTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.self import MyTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command.builtin import register_builtin_commands
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.helpers import image_placeholder_text, truncate_text
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, WebSearchConfig, WebToolsConfig
    from nanobot.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"

class _LoopHook(AgentHook):
    """Core hook for the main agent loop — preserves all project-specific behaviours."""

    def __init__(
        self,
        agent_loop: "AgentLoop",
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._loop = agent_loop
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._stream_buf = ""

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._loop._current_iteration = context.iteration

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        # Think-tag filter: strip <think>...</think> blocks before forwarding
        from nanobot.utils.helpers import strip_think
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean):]
        if incremental and self._on_stream:
            await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._on_progress:
            if not self._on_stream:
                # Non-streaming: show reasoning thought before tool hint
                thought = self._loop._strip_think(
                    context.response.content if context.response else None
                )
                if thought:
                    await self._on_progress(thought)
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        self._loop._set_tool_context(self._channel, self._chat_id, self._message_id)

    async def after_iteration(self, context: AgentHookContext) -> None:
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )



class _LoopHookChain(AgentHook):
    """Run the core hook before extra hooks (supports external hooks injection)."""

    __slots__ = ("_primary", "_extras")

    def __init__(self, primary: AgentHook, extra_hooks: list[AgentHook]) -> None:
        self._primary = primary
        self._extras = CompositeHook(extra_hooks)

    def wants_streaming(self) -> bool:
        return self._primary.wants_streaming() or self._extras.wants_streaming()

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._primary.before_iteration(context)
        await self._extras.before_iteration(context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._primary.on_stream(context, delta)
        await self._extras.on_stream(context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._primary.on_stream_end(context, resuming=resuming)
        await self._extras.on_stream_end(context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._primary.before_execute_tools(context)
        await self._extras.before_execute_tools(context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._primary.after_iteration(context)
        await self._extras.after_iteration(context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        content = self._primary.finalize_content(context, content)
        return self._extras.finalize_content(context, content)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        web_search_config: "WebSearchConfig | None" = None,
        web_proxy: str | None = None,
        web_config: "WebToolsConfig | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        sandbox_mode: str = "unrestricted",
        restrict_to_workspace: bool = False,  # upstream compat alias -> sandbox_mode=workspace
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        tool_preset: str = "developer",
        enable_file_tools: bool = True,
        enable_web_tools: bool = True,
        enable_spawn: bool = True,
        enable_cron: bool = True,
        enable_mcp: bool = True,
        disabled_skills: list[str] | None = None,
        timezone: str = "UTC",
        chatbot_config: Any | None = None,
        hooks: list[AgentHook] | None = None,
        dream_config: Any | None = None,
        unified_session: bool = False,
        session_ttl_minutes: int = 0,
    ):
        from nanobot.config.schema import AgentDefaults, ExecToolConfig, WebToolsConfig
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations if max_iterations is not None else defaults.max_tool_iterations
        self.context_window_tokens = context_window_tokens if context_window_tokens is not None else defaults.context_window_tokens
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = max_tool_result_chars if max_tool_result_chars is not None else defaults.max_tool_result_chars
        self.provider_retry_mode = provider_retry_mode
        # Merge web_config / legacy web_search_config + web_proxy params
        if web_config is not None:
            self.web_config = web_config
        else:
            self.web_config = WebToolsConfig()
            if web_search_config is not None:
                self.web_config.search = web_search_config
            if web_proxy is not None:
                self.web_config.proxy = web_proxy
        # Backward-compat aliases kept for commands.py (still passes legacy params)
        self.web_search_config = self.web_config.search
        self.web_proxy = self.web_config.proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        # restrict_to_workspace is upstream API; map to local sandbox_mode
        if restrict_to_workspace and sandbox_mode == "unrestricted":
            sandbox_mode = "workspace"
        self.sandbox_mode = sandbox_mode
        self.tool_preset = tool_preset
        self.enable_file_tools = enable_file_tools
        self.enable_web_tools = enable_web_tools
        self.enable_spawn = enable_spawn
        self.enable_cron = enable_cron
        self.enable_mcp = enable_mcp
        self._chatbot_config = chatbot_config
        self._extra_hooks: list[AgentHook] = hooks or []
        self._unified_session = unified_session

        self.context = ContextBuilder(workspace, disabled_skills=disabled_skills, timezone=timezone,
                                      chatbot_config=chatbot_config)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self._runtime_vars: dict[str, Any] = {}
        self._inject_default_hooks()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_config=self.web_config,
            max_tool_result_chars=self.max_tool_result_chars,
            exec_config=self.exec_config,
            restrict_to_workspace=(sandbox_mode == "workspace"),
        )
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        # Per-session locks: allow concurrent cross-session processing
        self._session_locks: dict[str, asyncio.Lock] = {}
        _max_concurrent = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max_concurrent) if _max_concurrent > 0 else None
        )
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._status_response: str | None = None
        self._memory_store = MemoryStore(workspace)
        self.memory_consolidator = Consolidator(
            store=self._memory_store,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
        )
        # Public alias aligning with upstream API; internal code uses memory_consolidator
        self.consolidator = self.memory_consolidator
        # Dream: instantiated and configured from dream_config
        from nanobot.config.schema import DreamConfig
        _dream_cfg: DreamConfig = dream_config if isinstance(dream_config, DreamConfig) else DreamConfig()
        _dream_model = _dream_cfg.model_override or self.model
        self.dream = Dream(
            store=self._memory_store,
            provider=provider,
            model=_dream_model,
            max_batch_size=_dream_cfg.max_batch_size,
            max_iterations=_dream_cfg.max_iterations,
        )
        self._dream_interval_s: float = _dream_cfg.interval_h * 3600
        self._dream_task: asyncio.Task | None = None
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.memory_consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self._register_default_tools()
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    def _inject_default_hooks(self) -> None:
        """Inject built-in hooks that power custom behaviors."""
        from nanobot.agent.hook import StripThinkHook, PromptSnapshotHook, VietnameseToolHintHook
        self._extra_hooks.extend([
            StripThinkHook(),
            PromptSnapshotHook(self.workspace, self.tools),
            VietnameseToolHintHook(),
        ])

    def _resolve_tool_flags(self) -> tuple[bool, bool, bool, bool, bool, bool]:
        """Resolve effective tool group flags from preset.

        Returns (eff_file, eff_exec, eff_web, eff_spawn, eff_cron, eff_mcp).
        """
        preset = self.tool_preset
        if preset == "chatbot":
            return False, False, True, False, False, True
        elif preset == "custom":
            return (
                self.enable_file_tools,
                self.sandbox_mode != "disabled",
                self.enable_web_tools,
                self.enable_spawn,
                self.enable_cron,
                self.enable_mcp,
            )
        else:  # developer — all enabled
            return True, self.sandbox_mode != "disabled", True, True, True, True

    def _register_default_tools(self) -> None:
        """Register the default set of tools based on preset and flags."""
        eff_file, eff_exec, eff_web, eff_spawn, eff_cron, eff_mcp = self._resolve_tool_flags()

        # File tools: respect sandbox boundary
        if eff_file:
            if self.sandbox_mode == "workspace":
                sandbox_base = self.workspace / "sandbox"
                subdir = self.exec_config.workspace_subdir
                effective_dir = sandbox_base / subdir if subdir else sandbox_base
                effective_dir.mkdir(parents=True, exist_ok=True)
                allowed_dir = effective_dir
            elif self.sandbox_mode == "disabled":
                allowed_dir = self.workspace  # restrict to workspace when exec disabled
            else:
                allowed_dir = None  # unrestricted
            extra_dirs = [Path(d) for d in self.exec_config.allowed_dirs] if self.sandbox_mode == "workspace" else None
            extra_read = ([BUILTIN_SKILLS_DIR] + (extra_dirs or [])) if allowed_dir else extra_dirs
            self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
            for cls in (WriteFileTool, EditFileTool, ListDirTool):
                self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_dirs))
            self.tools.register(NotebookEditTool(workspace=self.workspace, allowed_dir=allowed_dir))

        # Exec tool: resolve effective working dir
        if eff_exec and self.exec_config.enable:
            working_dir = str(self.workspace)
            if self.sandbox_mode not in ("unrestricted", "disabled"):
                sandbox_base = self.workspace / "sandbox"
                subdir = self.exec_config.workspace_subdir
                exec_dir = sandbox_base / subdir if subdir else sandbox_base
                exec_dir.mkdir(parents=True, exist_ok=True)
                working_dir = str(exec_dir)
            self.tools.register(ExecTool(
                working_dir=working_dir,
                timeout=self.exec_config.timeout,
                sandbox_mode=self.sandbox_mode,
                allowed_dirs=self.exec_config.allowed_dirs,
                path_append=self.exec_config.path_append,
            ))

        # Web tools
        if eff_web:
            self.tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
            self.tools.register(WebFetchTool(proxy=self.web_proxy))

        # Message tool (always enabled)
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound, working_dir=str(self.workspace)))

        # Spawn tool
        if eff_spawn:
            self.tools.register(SpawnTool(manager=self.subagents))

        # Cron tool
        if eff_cron and self.cron_service:
            self.tools.register(CronTool(self.cron_service, default_timezone=self.context.timezone or "UTC"))

        # MyTool: runtime inspection — always on except chatbot preset
        if self.tool_preset != "chatbot":
            self.tools.register(MyTool(loop=self, modify_allowed=False))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        # Check if MCP is enabled by current preset
        _, _, _, _, _, eff_mcp = self._resolve_tool_flags()
        if not eff_mcp:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>...</think> blocks that some models embed in content."""
        if not text:
            return None
        from nanobot.utils.helpers import strip_think
        return strip_think(text) or None

    def _with_effective_session_key(self, msg: InboundMessage) -> InboundMessage:
        """Apply unified-session routing while preserving explicit thread overrides."""
        if not self._unified_session or msg.session_key_override or msg.channel == "system":
            return msg
        return InboundMessage(
            channel=msg.channel,
            sender_id=msg.sender_id,
            chat_id=msg.chat_id,
            content=msg.content,
            timestamp=msg.timestamp,
            media=msg.media,
            metadata=msg.metadata,
            session_key_override=UNIFIED_SESSION_KEY,
        )

    # ── Custom: dynamic config reload (web dashboard support) ──
    def _reload_config(self) -> None:
        """Reload model/provider/credentials from config file.

        Called at the start of each message processing — similar to how SOUL.md
        is re-read on every call via ContextBuilder._get_identity().
        """
        try:
            from nanobot.config.loader import load_config
            config = load_config()

            # Reload model
            new_model = config.agents.defaults.model
            if new_model and new_model != self.model:
                logger.info("[Agent] Model changed: {} -> {}", self.model, new_model)
                self.model = new_model

            # Reload provider credentials (detect key change OR provider change)
            active_model = new_model or self.model
            provider_name = config.get_provider_name(active_model)
            p = config.get_provider(active_model)
            
            if p:
                from nanobot.providers.registry import find_by_name
                spec = find_by_name(provider_name) if provider_name else None
                
                is_oauth = spec.is_oauth if spec else False
                is_local = spec.is_local if spec else False
                
                # Providers like OpenAI Codex (OAuth) and Ollama (Local) don't need API keys
                if p.api_key or is_oauth or is_local:
                    current_provider = getattr(self, "_current_provider_name", None)
                    current_key = getattr(self.provider, "api_key", None)
                    key_changed = p.api_key != current_key
                    provider_changed = provider_name != current_provider
                    
                    if key_changed or provider_changed:
                        # Delegate to _make_provider() which handles all provider
                        # types: custom → CustomProvider, azure → AzureOpenAIProvider,
                        # everything else → LiteLLMProvider.
                        from nanobot.cli.commands import _make_provider
                        self.provider = _make_provider(config)
                        self._current_provider_name = provider_name
                        # Sync provider to all dependent objects that hold
                        # their own reference (state sync fix 2026-04-10)
                        self.runner.provider = self.provider
                        self.subagents.provider = self.provider
                        self.subagents.model = active_model
                        self.memory_consolidator.provider = self.provider
                        self.memory_consolidator.model = active_model
                        self.dream.provider = self.provider
                        self.dream._runner.provider = self.provider
                        logger.info("[Agent] Provider recreated: '{}' model='{}' (key_changed={}, provider_changed={})",
                                    provider_name, active_model, key_changed, provider_changed)

            # Hot-reload chatbot config
            self._chatbot_config = config.chatbot
            self.context._chatbot_config = config.chatbot

            # Hot-reload tool preset — re-register tools when preset or flags change
            tc = config.tools
            preset_changed = (
                tc.tool_preset != self.tool_preset
                or tc.enable_file_tools != self.enable_file_tools
                or tc.enable_web_tools != self.enable_web_tools
                or tc.enable_spawn != self.enable_spawn
                or tc.enable_cron != self.enable_cron
                or tc.enable_mcp != self.enable_mcp
                or tc.sandbox_mode != self.sandbox_mode
            )
            if preset_changed:
                old_preset = self.tool_preset
                self.tool_preset = tc.tool_preset
                self.enable_file_tools = tc.enable_file_tools
                self.enable_web_tools = tc.enable_web_tools
                self.enable_spawn = tc.enable_spawn
                self.enable_cron = tc.enable_cron
                self.enable_mcp = tc.enable_mcp
                self.sandbox_mode = tc.sandbox_mode
                self.tools = ToolRegistry()
                self._register_default_tools()
                logger.info("[Agent] Tool preset reloaded: '{}' -> '{}'", old_preset, tc.tool_preset)
        except Exception as e:
            logger.info("[Agent] Config reload ERROR: {}", e)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop via AgentRunner + _LoopHook.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.
        """
        # Custom: hot-reload model/provider/preset from config file at start of each turn
        self._reload_config()

        loop_hook = _LoopHook(
            self,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
        )
        hook: AgentHook = (
            _LoopHookChain(loop_hook, self._extra_hooks)
            if self._extra_hooks
            else loop_hook
        )

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        result = await self.runner.run(AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.tools,
            model=self.model,
            max_iterations=self.max_iterations,
            max_tool_result_chars=self.max_tool_result_chars,
            hook=hook,
            error_message="Sorry, I encountered an error calling the AI model.",
            concurrent_tools=True,
            workspace=self.workspace,
            session_key=session.key if session else None,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
            provider_retry_mode=self.provider_retry_mode,
            progress_callback=on_progress,
            checkpoint_callback=_checkpoint,
        ))

        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])

        return result.final_content, result.tools_used, result.messages, result.stop_reason

    # ── Message debouncing for channel messages ──
    _DEBOUNCE_SKIP_CHANNELS = frozenset({"webchat", "cli"})

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        self._dream_task = asyncio.create_task(self._run_dream_loop())
        logger.info("Agent loop started (nanobot v{}, model={})", __version__, self.model)

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._get_active_session_keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            msg = self._with_effective_session_key(msg)
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result:
                    await self.bus.publish_outbound(result)
                continue

            # Channels that handle their own input blocking: dispatch immediately
            if msg.channel in self._DEBOUNCE_SKIP_CHANNELS:
                self._dispatch_task(msg)
                continue

            # Debounce: wait briefly to batch rapid messages from same session
            from nanobot.config.loader import load_config
            debounce_s = load_config().channels.debounce_seconds
            if debounce_s <= 0:
                self._dispatch_task(msg)
                continue

            batched = [msg]
            deadline = time.monotonic() + debounce_s

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_msg = await asyncio.wait_for(
                        self.bus.consume_inbound(), timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    break

                next_raw = next_msg.content.strip()
                next_msg = self._with_effective_session_key(next_msg)
                # Priority commands (e.g. /stop, /restart): execute immediately, don't batch
                if self.commands.is_priority(next_raw):
                    next_ctx = CommandContext(
                        msg=next_msg, session=None,
                        key=next_msg.session_key, raw=next_raw, loop=self,
                    )
                    next_result = await self.commands.dispatch_priority(next_ctx)
                    if next_result:
                        await self.bus.publish_outbound(next_result)
                    continue
                # Other slash commands: dispatch as normal task, don't batch
                if next_raw.startswith("/"):
                    self._dispatch_task(next_msg)
                    continue

                # Same session: batch together, reset timer
                if next_msg.session_key == msg.session_key:
                    batched.append(next_msg)
                    deadline = time.monotonic() + debounce_s
                else:
                    # Different session: dispatch separately
                    self._dispatch_task(next_msg)

            # Merge batched messages and dispatch
            if len(batched) > 1:
                combined = "\n".join(m.content for m in batched)
                merged = InboundMessage(
                    channel=msg.channel,
                    sender_id=msg.sender_id,
                    chat_id=msg.chat_id,
                    content=combined,
                    timestamp=msg.timestamp,
                    media=msg.media,
                    metadata=msg.metadata,
                    session_key_override=msg.session_key_override,
                )
                logger.info("[Debounce] Batched {} messages for {}", len(batched), msg.session_key)
                self._dispatch_task(merged)
            else:
                self._dispatch_task(msg)

    def _dispatch_task(self, msg: InboundMessage) -> None:
        """Create an async task to dispatch a message."""
        msg = self._with_effective_session_key(msg)
        task = asyncio.create_task(self._dispatch(msg))
        self._active_tasks.setdefault(msg.session_key, []).append(task)
        task.add_done_callback(
            lambda t, k=msg.session_key: (
                self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )
        )

    async def _run_dream_loop(self) -> None:
        """Background loop: run Dream every dream_interval_s seconds."""
        logger.info("Dream scheduler started (interval={}h)", self._dream_interval_s / 3600)
        while self._running:
            await asyncio.sleep(self._dream_interval_s)
            if not self._running:
                break
            try:
                did_work = await self.dream.run()
                if did_work:
                    logger.info("Dream run completed (scheduled)")
                else:
                    logger.debug("Dream run: no new history entries to process")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dream scheduled run failed, will retry next cycle")

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    # _handle_restart removed: OQ1 — restart handled by cmd_restart() in command/builtin.py
    # which uses os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])
    # This is correct on Windows. The method was migrated to upstream builtin.py in Phase 1D.

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        async with lock, gate:
            try:
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):
                    stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                    stream_segment = 0

                    def _current_stream_id() -> str:
                        return f"{stream_base_id}:{stream_segment}"

                    async def on_stream(delta: str) -> None:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=delta, metadata={"_stream_delta": True, "_stream_id": _current_stream_id()},
                        ))

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        nonlocal stream_segment
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata={"_stream_end": True, "_resuming": resuming, "_stream_id": _current_stream_id()},
                        ))
                        stream_segment += 1

                response = await self._process_message(
                    msg, on_stream=on_stream, on_stream_end=on_stream_end,
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._background_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Background tasks drain timed out — cancelling remaining")
                for t in self._background_tasks:
                    if not t.done():
                        t.cancel()
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await asyncio.wait_for(self._mcp_stack.aclose(), timeout=2.0)
            except (RuntimeError, BaseExceptionGroup, asyncio.TimeoutError):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)


    def _get_active_session_keys(self) -> frozenset[str]:
        """Collect session keys that currently have in-flight agent or subagent tasks.

        Passed to AutoCompact.check_expired() so that sessions being actively
        processed are never compacted mid-turn, preventing race conditions.
        """
        active: set[str] = set()
        # Main loop tasks: session_key -> list[asyncio.Task]
        for key, tasks in self._active_tasks.items():
            if any(not t.done() for t in tasks):
                active.add(key)
        # Subagent tasks: session_key -> set[task_id]
        for key in self.subagents._session_tasks:
            if self.subagents.get_running_count_by_session(key) > 0:
                active.add(key)
        return frozenset(active)
    def _build_status(self, session_msg_count: int = 0) -> str:
        """Build /status response content."""
        from nanobot.utils.helpers import build_status_content, estimate_prompt_tokens
        return build_status_content(
            version=__version__,
            model=self.model or "unknown",
            start_time=self._start_time,
            last_usage=self._last_usage,
            context_window_tokens=self.context_window_tokens,
            session_msg_count=session_msg_count,
            context_tokens_estimate=estimate_prompt_tokens([], None),
        )

    @staticmethod
    def _image_placeholder(block: dict[str, Any]) -> dict[str, str]:
        """Convert an inline image block into a compact text placeholder."""
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

    def _sanitize_persisted_blocks(
        self,
        content: Any,
        *,
        truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> Any:
        """Strip volatile multimodal payloads before writing session history."""
        if not isinstance(content, list):
            return content
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if (
                block.get("type") == "image_url"
                and block.get("image_url", {}).get("url", "").startswith("data:image/")
            ):
                filtered.append(self._image_placeholder(block))
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if truncate_text and len(text) > self.max_tool_result_chars:
                    text = text[:self.max_tool_result_chars] + "\\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered if filtered else content

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        if self._dream_task and not self._dream_task.done():
            self._dream_task.cancel()
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        direct_return: bool = False,  # Custom: bypass _sent_in_turn for fleet/direct callers
        on_multi_send: Callable[[str], Awaitable[None]] | None = None,  # Custom: send intermediate multi-message parts
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            if self._restore_runtime_checkpoint(session):
                self.sessions.save(session)
            session, _compact_summary = self.auto_compact.prepare_session(session, key)
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=0)
            # Subagent results should be assistant role, other system messages use user role
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                sender_id=msg.sender_id,
                sender_name=msg.metadata.get("sender_name"),
                thread_type=msg.metadata.get("thread_type"),
                session_summary=_compact_summary,
                current_role=current_role,
            )
            final_content, _, all_msgs = await self._run_agent_loop(
                messages,
                session=session, channel=channel, chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self._clear_runtime_checkpoint(session)
            self.sessions.save(session)
            self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        # Restore any unfinished turn from a previous crash/restart
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        session, _compact_summary = self.auto_compact.prepare_session(session, key)

        # Slash commands — delegate to command router
        raw = msg.content.strip()
        ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        if result := await self.commands.dispatch(ctx):
            return result

        await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            sender_name=msg.metadata.get("sender_name"),
            thread_type=msg.metadata.get("thread_type"),
            session_summary=_compact_summary,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        final_content, tools_used, all_msgs, stop_reason = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            session=session,
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
        )

        if not final_content or not final_content.strip():
            if tools_used:
                tools_str = ", ".join(sorted(set(tools_used)))
                final_content = f"*[Executed tools: {tools_str}]*"
            else:
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        self._save_turn(session, all_msgs, 1 + len(history))
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))

        # Custom: bypass _sent_in_turn suppression for direct_return callers (fleet, cron)
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn and not direct_return:
            return None

        # Delegate multi-message splitting and typing delay to DeliveryPolicy
        from nanobot.agent.delivery import ResponseDeliveryPolicy
        policy = ResponseDeliveryPolicy(chatbot_config=self._chatbot_config, bus=self.bus)
        
        return await policy.deliver(
            final_content=final_content,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            metadata=msg.metadata or {},
            on_stream=on_stream,
            on_multi_send=on_multi_send,
            direct_return=direct_return,
            stop_reason=stop_reason,
        )

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": "Error: Task interrupted before this tool finished.",
                "timestamp": datetime.now().isoformat(),
            })

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])
        self._clear_runtime_checkpoint(session)
        return True

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text(content, self.max_tool_result_chars)
                entry["content"] = self._sanitize_persisted_blocks(entry["content"])
            elif role == "assistant":
                entry["content"] = self._sanitize_persisted_blocks(entry.get("content"))
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                # Custom: Strip room context — extract plain content for natural session log
                c = entry.get("content", "")
                if isinstance(c, str) and c.startswith("[ROOM:"):
                    last_other = None
                    user_msg = None
                    for line in c.split("\n"):
                        if line.startswith("👤 User:"):
                            user_msg = line[len("👤 User:"):].strip()
                        elif line.startswith("🤖 ") and "(bạn)" not in line:
                            colon_pos = line.find(": ", 2)
                            if colon_pos > 0:
                                last_other = line[colon_pos + 2:].strip()
                    entry["content"] = last_other or user_msg or "(room discussion)"
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": image_placeholder_text((c.get("_meta") or {}).get("path", ""))})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_multi_send: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly (for CLI, cron, or fleet usage)."""
        await self._connect_mcp()
        # Custom: sender_id=chat_id (not "user") so session log shows actual caller name
        msg = InboundMessage(channel=channel, sender_id=chat_id, chat_id=chat_id, content=content, media=media)
        return await self._process_message(
            msg, session_key=session_key, on_progress=on_progress,
            on_stream=on_stream, on_stream_end=on_stream_end, direct_return=True,
            on_multi_send=on_multi_send,
        )
