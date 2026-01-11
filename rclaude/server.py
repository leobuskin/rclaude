"""Combined HTTP + Telegram server for rclaude."""

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, cast

from aiohttp import web
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ChatAction

import shlex

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    UserMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    query as claude_query,
)
from claude_agent_sdk.types import HookInput, HookContext, SyncHookJSONOutput

from rclaude.settings import Config
from rclaude.image_handler import (
    download_telegram_photo,
    prepare_image_for_claude,
    cleanup_image_file,
)
from rclaude.session import (
    get_session,
    PendingQuestion,
    PendingPermission,
    UserSession,
    SessionUpdate,
    PermissionMode,
    ContextUsage,
    save_session_state,
    load_session_state,
    clear_session_state,
)
from rclaude.formatting import (
    send_text,
    send_tool_call,
    send_tool_result,
    create_question_keyboard,
    format_permission_prompt,
    create_permission_keyboard,
    create_mode_keyboard,
    create_model_keyboard,
)


# ─────────────────────────────────────────────────────────────────────────────
# Connection Tracking for Auto-Shutdown
# ─────────────────────────────────────────────────────────────────────────────

_sse_connection_count = 0
_shutdown_event: asyncio.Event | None = None


def _get_shutdown_event() -> asyncio.Event:
    """Get or create the shutdown event."""
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def _get_watcher_pid_file(wrapper_pid: int) -> Path:
    """Get the watcher PID file path for a given wrapper."""
    return Path(f'/tmp/rclaude-watcher-{wrapper_pid}.pid')


def _trigger_shutdown() -> None:
    """Trigger server shutdown if started by wrapper (not standalone)."""
    import signal as sig

    # Only auto-shutdown if started by a wrapper (has RCLAUDE_WRAPPER_PID)
    # Standalone servers (rclaude serve) should keep running
    wrapper_pid = os.environ.get('RCLAUDE_WRAPPER_PID')
    if not wrapper_pid:
        logger.info('[SHUTDOWN] Standalone server, not shutting down')
        return

    event = _get_shutdown_event()
    event.set()
    logger.info('[SHUTDOWN] Server shutdown triggered')

    # Kill watcher process if we have its PID
    if wrapper_pid:
        pid_file = _get_watcher_pid_file(int(wrapper_pid))
        if pid_file.exists():
            try:
                watcher_pid = int(pid_file.read_text().strip())
                os.kill(watcher_pid, sig.SIGTERM)
                logger.info(f'[SHUTDOWN] Sent SIGTERM to watcher pid {watcher_pid}')
                pid_file.unlink(missing_ok=True)
            except (ValueError, OSError, ProcessLookupError) as e:
                logger.warning(f'[SHUTDOWN] Could not kill watcher: {e}')


def get_local_claude_cli() -> str | None:
    """Find local Claude CLI, prefer it over SDK bundled version."""
    # Check common locations
    local_claude = Path.home() / '.claude' / 'local' / 'claude'
    if local_claude.exists():
        return str(local_claude)

    # Fallback to PATH
    claude_path = shutil.which('claude')
    if claude_path:
        return claude_path

    return None  # Will use SDK bundled


# ─────────────────────────────────────────────────────────────────────────────
# Dummy Hook for can_use_tool
# ─────────────────────────────────────────────────────────────────────────────

# Required workaround: In Python, can_use_tool requires a PreToolUse hook that
# returns {"continue_": True} to keep the stream open. Without this hook, the
# stream closes before the permission callback can be invoked.
# See: https://platform.claude.com/docs/en/agent-sdk/user-input


async def dummy_pretool_hook(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> SyncHookJSONOutput:
    """Keep stream open for can_use_tool callback."""
    # TypedDict is just a type hint - return a plain dict
    result: SyncHookJSONOutput = {'continue_': True}
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Permission System
# ─────────────────────────────────────────────────────────────────────────────

# Permission mode display names
MODE_DISPLAY = {
    'default': '🔒 Default (ask for permissions)',
    'acceptEdits': '📝 Accept Edits (auto-allow file changes)',
    'plan': '📋 Plan Mode (read-only)',
    'bypassPermissions': '⚠️ Dangerous (skip all permissions)',
}


def _format_mode_display(mode: str) -> str:
    """Format permission mode for display."""
    return MODE_DISPLAY.get(mode, f'🔒 {mode}')


VALID_MODES = ('default', 'acceptEdits', 'plan', 'bypassPermissions')


def _validate_permission_mode(mode: str) -> str:
    """Validate and return a permission mode, defaulting to 'default' if invalid."""
    if mode in VALID_MODES:
        return mode
    return 'default'


def _format_mode_short(mode: str) -> str:
    """Format permission mode as short label."""
    short = {
        'default': '🔒',
        'acceptEdits': '📝',
        'plan': '📋',
        'bypassPermissions': '⚠️',
    }
    return short.get(mode, '🔒')


def _format_model_short(model: str | None) -> str:
    """Format model name as short label."""
    if not model:
        return '⚡ sonnet'
    m = model.lower()
    if 'opus' in m:
        return '🧠 opus'
    if 'haiku' in m:
        return '🚀 haiku'
    return f'⚡ {model}'


def _parse_context_output(text: str) -> ContextUsage | None:
    """Parse /context command output to extract token usage.

    Expected formats:
        **Tokens:** 21.8k / 200.0k (11%)  (markdown from SDK)
        Tokens: 24.4k / 200.0k (12%)      (plain text from CLI)
    """
    # Match pattern like "**Tokens:** 21.8k / 200.0k (11%)" or "Tokens: 24.4k / 200.0k (12%)"
    match = re.search(r'\*?\*?Tokens:\*?\*?\s*([\d.]+)k\s*/\s*([\d.]+)k\s*\((\d+)%\)', text)
    if not match:
        return None

    used_str, max_str, percent_str = match.groups()

    # Parse token counts (values are in 'k' so multiply by 1000)
    return ContextUsage(
        tokens_used=int(float(used_str) * 1000),
        tokens_max=int(float(max_str) * 1000),
        percent_used=int(percent_str),
    )


def _format_pinned_status(session: 'UserSession') -> str:
    """Format the pinned status message content."""
    mode_icon = _format_mode_short(session.permission_mode)
    model_display = _format_model_short(session.current_model)

    parts = [f'{mode_icon} <b>{session.permission_mode}</b>', model_display]

    # Add context usage if available
    if session.context.tokens_max > 0:
        parts.append(f'📝 {session.context.percent_used}%')

    # Add cost if available
    if session.usage.total_cost_usd > 0:
        parts.append(f'💰 ${session.usage.total_cost_usd:.4f}')

    return ' | '.join(parts)


async def _update_pinned_message(
    bot: Bot,
    chat_id: int,
    session: 'UserSession',
) -> None:
    """Update the pinned status message, creating it if needed."""
    text = _format_pinned_status(session)

    try:
        if session.pinned_message_id:
            # Try to update existing message
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=session.pinned_message_id,
                parse_mode='HTML',
            )
        else:
            # Create new message and pin it
            msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            await msg.pin(disable_notification=True)
            session.pinned_message_id = msg.message_id
    except Exception as e:
        logger.warning(f'Failed to update pinned message: {e}')


# Tools that require interactive approval in default mode
APPROVAL_REQUIRED_TOOLS = {'Edit', 'Write', 'Bash', 'NotebookEdit'}

# Edit tools that are auto-allowed in acceptEdits mode
EDIT_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}


def generate_permission_rule(tool_name: str, input_data: dict[str, Any]) -> str:
    """Generate CC-compatible permission rule pattern."""
    if tool_name == 'Bash':
        command = input_data.get('command', '')
        # Extract base command (first word)
        base_cmd = command.split()[0] if command else ''
        return f'Bash({base_cmd}:*)'
    elif tool_name == 'Edit':
        file_path = input_data.get('file_path', '')
        return f'Edit(//{file_path})'
    elif tool_name == 'Write':
        file_path = input_data.get('file_path', '')
        return f'Write(//{file_path})'
    elif tool_name == 'NotebookEdit':
        notebook_path = input_data.get('notebook_path', '')
        return f'NotebookEdit(//{notebook_path})'
    else:
        return f'{tool_name}(*)'


# ─────────────────────────────────────────────────────────────────────────────
# Smart Permission Rule Generation (using Haiku)
# ─────────────────────────────────────────────────────────────────────────────

_SMART_RULE_SYSTEM_PROMPT = """You convert bash commands to permission patterns. Output ONLY the pattern, nothing else.

Rules:
- Keep: command name, subcommand, all flags (starting with - or --)
- Remove: all values (paths, names, URLs, numbers like -20)
- End with single *

Examples:
Input: git push origin main --tags
Output: git push --tags *

Input: head -20 file.txt
Output: head *

Input: tail -f log.txt
Output: tail -f *

Input: docker run -it --rm -v /a:/b img
Output: docker run -it --rm -v *

Input: kubectl get pods -n ns -o wide
Output: kubectl get pods -n -o *

Input: python3 script.py --verbose
Output: python3 --verbose *

Respond with ONLY the pattern, no explanation."""


def _pattern_matches_command(pattern: str, command: str) -> bool:
    """Check if a permission pattern matches the original command."""
    if not pattern.endswith(' *') and pattern != '*':
        if not pattern.endswith('*'):
            return False

    pattern_prefix = pattern.rstrip(' *').strip()

    if not pattern_prefix:
        return True

    try:
        pattern_tokens = shlex.split(pattern_prefix)
        command_tokens = shlex.split(command)
    except ValueError:
        pattern_tokens = pattern_prefix.split()
        command_tokens = command.split()

    if not command_tokens:
        return False

    cmd_idx = 0
    for pat_token in pattern_tokens:
        found = False
        while cmd_idx < len(command_tokens):
            if command_tokens[cmd_idx] == pat_token:
                found = True
                cmd_idx += 1
                break
            cmd_idx += 1

        if not found:
            return False

    return True


def _is_pattern_too_broad(pattern: str) -> bool:
    """Check if pattern is dangerously broad.

    Currently only rejects bare wildcard patterns.
    """
    pattern_prefix = pattern.rstrip(' *').strip()

    # Reject bare "*" - matches everything
    if not pattern_prefix:
        return True

    return False


async def _generate_pattern_once(command: str) -> str:
    """Single attempt to generate a pattern using Haiku."""
    options = ClaudeAgentOptions(
        tools=[],
        model='haiku',
        max_turns=1,
        system_prompt=_SMART_RULE_SYSTEM_PROMPT,
    )

    result = ''
    async for message in claude_query(prompt=command, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result += block.text

    pattern = result.strip()

    # Normalize: ensure ends with " *"
    if not pattern.endswith(' *'):
        if pattern.endswith('*'):
            pattern = pattern[:-1].rstrip() + ' *'
        else:
            pattern = pattern + ' *'

    return pattern


async def generate_smart_bash_rule(command: str, max_retries: int = 2) -> str:
    """Generate a smart permission rule pattern for a Bash command.

    Uses Haiku to intelligently extract the command intent (tool + subcommand + flags)
    while wildcarding values (paths, names, URLs, etc).

    Returns a pattern like "Bash(git push --tags *)" or falls back to simple
    "Bash(git:*)" if generation fails.
    """
    # Extract base command for fallback
    try:
        tokens = shlex.split(command)
        base_cmd = tokens[0] if tokens else command.split()[0]
    except (ValueError, IndexError):
        base_cmd = command.split()[0] if command else 'unknown'

    fallback = f'Bash({base_cmd}:*)'

    for attempt in range(max_retries + 1):
        try:
            pattern = await _generate_pattern_once(command)

            if not _pattern_matches_command(pattern, command):
                logger.warning(f'[SMART_RULE] Attempt {attempt + 1}: pattern {pattern!r} does not match command')
                continue

            if _is_pattern_too_broad(pattern):
                logger.warning(f'[SMART_RULE] Attempt {attempt + 1}: pattern {pattern!r} is too broad')
                continue

            logger.info(f'[SMART_RULE] Generated: {pattern}')
            return f'Bash({pattern})'

        except Exception as e:
            logger.warning(f'[SMART_RULE] Attempt {attempt + 1} error: {e}')

    logger.warning(f'[SMART_RULE] All attempts failed, using fallback: {fallback}')
    return fallback


def load_permission_rules(cwd: str) -> list[str]:
    """Load allow rules from .claude/settings.local.json."""
    settings_path = Path(cwd) / '.claude' / 'settings.local.json'
    if not settings_path.exists():
        return []
    try:
        settings = json.loads(settings_path.read_text())
        return settings.get('permissions', {}).get('allow', [])
    except (json.JSONDecodeError, KeyError):
        return []


def check_permission_rule(tool_name: str, input_data: dict[str, Any], rules: list[str]) -> bool:
    """Check if a tool call matches any allow rule."""
    # Generate the rule that would match this tool call
    generated_rule = generate_permission_rule(tool_name, input_data)

    # Check exact match first
    if generated_rule in rules:
        return True

    # For Bash commands, also check wildcard patterns
    if tool_name == 'Bash':
        command = input_data.get('command', '')
        base_cmd = command.split()[0] if command else ''
        # Check Bash(cmd:*) pattern
        if f'Bash({base_cmd}:*)' in rules:
            return True
        # Check Bash(*) pattern (all bash commands)
        if 'Bash(*)' in rules:
            return True

    return False


def add_permission_rule_to_file(cwd: str, rule: str) -> None:
    """Add a permission rule to .claude/settings.local.json in the project."""
    settings_path = Path(cwd) / '.claude' / 'settings.local.json'
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    if 'permissions' not in settings:
        settings['permissions'] = {'allow': [], 'deny': [], 'ask': []}

    # Add if not already present
    if rule not in settings['permissions']['allow']:
        settings['permissions']['allow'].append(rule)
        settings_path.write_text(json.dumps(settings, indent=2))
        logger.info(f'Added permission rule: {rule}')


def create_permission_handler(
    bot: Bot,
    user_id: int,
    session: UserSession,
) -> Callable[[str, dict[str, Any], ToolPermissionContext], Coroutine[Any, Any, PermissionResultAllow | PermissionResultDeny]]:
    """Create a permission handler bound to a specific Telegram context."""

    async def permission_handler(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Handle tool permission requests via Telegram."""
        logger.info(f'[PERMISSION] can_use_tool called: tool={tool_name}, mode={session.permission_mode}')

        # Check permission mode first
        if session.permission_mode == 'bypassPermissions':
            logger.info(f'[PERMISSION] Auto-allowing {tool_name} (bypass mode)')
            return PermissionResultAllow(updated_input=input_data)

        if session.permission_mode == 'acceptEdits' and tool_name in EDIT_TOOLS:
            logger.info(f'[PERMISSION] Auto-allowing {tool_name} (acceptEdits mode)')
            return PermissionResultAllow(updated_input=input_data)

        # Auto-allow tools that don't need approval in default mode
        if tool_name not in APPROVAL_REQUIRED_TOOLS:
            logger.info(f'[PERMISSION] Auto-allowing {tool_name} (not in approval list)')
            return PermissionResultAllow(updated_input=input_data)

        # Check if a saved rule allows this tool
        rules = load_permission_rules(session.cwd)
        if check_permission_rule(tool_name, input_data, rules):
            logger.info(f'[PERMISSION] Auto-allowing {tool_name} (matches saved rule)')
            return PermissionResultAllow(updated_input=input_data)

        # Create pending permission request
        request_id = str(uuid.uuid4())
        pending = PendingPermission(
            request_id=request_id,
            tool_name=tool_name,
            input_data=input_data,
        )
        session.pending_permission = pending
        logger.info(f'[PERMISSION] Created pending permission: {request_id}')

        # Format and send the permission prompt
        text = format_permission_prompt(tool_name, input_data)
        keyboard = create_permission_keyboard(tool_name)

        try:
            await asyncio.wait_for(
                bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                    disable_notification=False,  # User action required - notify with sound
                ),
                timeout=10,
            )
            logger.info('[PERMISSION] Sent permission prompt to Telegram')
        except asyncio.TimeoutError:
            logger.warning('[PERMISSION] Timeout sending permission prompt (10s), allowing operation')
            # On timeout, allow the operation (fail-open for usability)
            session.pending_permission = None
            return PermissionResultAllow(updated_input=input_data)
        except Exception as e:
            logger.error(f'Failed to send permission prompt: {e}')
            # On error, allow the operation (fail-open for usability)
            session.pending_permission = None
            return PermissionResultAllow(updated_input=input_data)

        # Wait for user response (no timeout - like CLI behavior)
        logger.info('[PERMISSION] Waiting for user response on event...')
        try:
            await pending.event.wait()
            logger.info(f'[PERMISSION] Event wait completed! result={pending.result}')
        except Exception as e:
            logger.error(f'[PERMISSION] Exception during event.wait(): {e}')
            session.pending_permission = None
            return PermissionResultDeny(message=f'Error waiting: {e}', interrupt=False)

        # Clear pending permission
        session.pending_permission = None

        if pending.result is not None:
            logger.info(f'[PERMISSION] Returning result: {type(pending.result).__name__}')
            return pending.result
        else:
            # Should not happen, but default to deny
            logger.warning('[PERMISSION] No result set, returning deny')
            return PermissionResultDeny(message='No response received', interrupt=False)

    return permission_handler


def can_resume_session(session_id: str, cwd: str) -> bool:
    """Check if a session can be resumed (exists and has actual conversation content)."""
    # Build the session file path (same logic as Claude Code uses)
    project_path = cwd.replace('/', '-').replace(':', '')
    if project_path.startswith('-'):
        project_path = project_path[1:]

    log_dir = Path.home() / '.claude' / 'projects' / f'-{project_path}'
    log_file = log_dir / f'{session_id}.jsonl'

    if not log_file.exists():
        logger.info(f'[SESSION] can_resume_session: {session_id[:8]}... -> False (file not found)')
        return False

    # Check if file has actual message content, not just summaries
    # Valid sessions have "type":"user" or "type":"assistant" messages
    try:
        with open(log_file) as f:
            for line in f:
                if '"type":"user"' in line or '"type":"assistant"' in line:
                    logger.info(f'[SESSION] can_resume_session: {session_id[:8]}... -> True (has messages)')
                    return True
        logger.info(f'[SESSION] can_resume_session: {session_id[:8]}... -> False (no messages, only metadata)')
        return False
    except Exception as e:
        logger.warning(f'[SESSION] can_resume_session: {session_id[:8]}... -> False (error: {e})')
        return False


logger = logging.getLogger('rclaude')


@dataclass
class TeleportRequest:
    """A pending teleport from Claude Code."""

    session_id: str
    cwd: str
    terminal_id: str
    permission_mode: str = 'default'


@dataclass
class PendingSetupLink:
    """A pending setup link token."""

    token: str
    event: asyncio.Event
    result: tuple[int, str] | None = None  # (user_id, username)


# Pending teleports waiting to be picked up
_pending_teleports: dict[int, TeleportRequest] = {}  # user_id -> teleport

# Pending setup links: token -> PendingSetupLink
_pending_setup_links: dict[str, PendingSetupLink] = {}


async def handle_teleport(request: web.Request) -> web.Response:
    """Handle POST /teleport from Claude Code /tg hook."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({'error': 'Invalid JSON'}, status=400)

    session_id = data.get('session_id')
    cwd = data.get('cwd', '.')
    permission_mode = data.get('permission_mode', 'default')
    terminal_id = data.get('terminal_id')

    if not session_id:
        return web.json_response({'error': 'session_id required'}, status=400)
    if not terminal_id:
        return web.json_response({'error': 'terminal_id required'}, status=400)

    # Get the config to find the user
    config: Config = request.app['config']
    user_id = config.telegram.user_id

    if not user_id:
        return web.json_response({'error': 'No Telegram user configured'}, status=400)

    logger.info(f'Teleport received: session={session_id[:8]}..., terminal={terminal_id[:8]}..., cwd={cwd}, mode={permission_mode}')

    # Store pending teleport (keyed by user, most recent wins)
    _pending_teleports[user_id] = TeleportRequest(
        session_id=session_id,
        cwd=cwd,
        terminal_id=terminal_id,
        permission_mode=permission_mode,
    )

    # Notify via Telegram (fire and forget with timeout to avoid blocking HTTP response)
    bot = request.app['telegram_app'].bot
    mode_display = _format_mode_display(permission_mode)

    async def send_notification():
        """Send notification with timeout to avoid blocking."""
        try:
            await asyncio.wait_for(
                bot.send_message(
                    chat_id=user_id,
                    text=f'📱 Session teleported from terminal!\n\n'
                    f'Session: `{session_id[:8]}...`\n'
                    f'Terminal: `{terminal_id[:8]}...`\n'
                    f'Directory: `{cwd}`\n'
                    f'Mode: {mode_display}\n\n'
                    f'Send any message to continue, or /cancel to ignore.',
                    parse_mode='Markdown',
                    disable_notification=False,  # Enable sound notification for teleport
                ),
                timeout=10,
            )
            logger.info('[TELEPORT] Notification sent successfully')
        except asyncio.TimeoutError:
            logger.warning('Timeout sending teleport notification (10s), continuing anyway')
        except Exception as e:
            logger.error(f'Failed to send Telegram notification: {e}')

    # Send notification asynchronously without blocking the HTTP response
    asyncio.create_task(send_notification())

    return web.json_response({'ok': True, 'message': 'Teleport initiated'})


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({'status': 'ok'})


async def handle_prepare_reload(request: web.Request) -> web.Response:
    """Prepare for hot-reload by saving session state."""
    config: Config = request.app['config']
    user_id = config.telegram.user_id

    if user_id:
        session = get_session(user_id)
        # Disconnect SDK client gracefully
        if session.client:
            try:
                await session.client.disconnect()
            except Exception as e:
                logger.warning(f'Error disconnecting client: {e}')
            session.client = None

    # Save session state to disk
    save_session_state()
    logger.info('Session state saved for hot-reload')

    return web.json_response({'ok': True, 'message': 'Ready for reload'})


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """SSE endpoint to stream session updates to terminal."""
    global _sse_connection_count

    config: Config = request.app['config']
    user_id = config.telegram.user_id

    if not user_id:
        return web.json_response({'error': 'No user configured'}, status=400)

    # Get terminal_id from query params
    terminal_id = request.query.get('terminal_id')
    if not terminal_id:
        return web.json_response({'error': 'terminal_id required'}, status=400)

    session = get_session(user_id)

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        },
    )
    await response.prepare(request)

    # Track connection
    _sse_connection_count += 1
    logger.info(f'[SSE] Connection opened for terminal {terminal_id[:8]}..., count={_sse_connection_count}')

    # Send initial connection message
    await response.write(b'event: connected\ndata: {}\n\n')

    try:
        while True:
            # Check if this terminal has been superseded by another teleport
            if session.terminal_id and session.terminal_id != terminal_id:
                logger.info(f'[SSE] Terminal {terminal_id[:8]}... superseded by {session.terminal_id[:8]}...')
                data = json.dumps({'type': 'superseded', 'content': 'Another terminal took over'})
                await response.write(f'event: update\ndata: {data}\n\n'.encode())
                break

            try:
                # Wait for updates with timeout
                update = await asyncio.wait_for(session.update_queue.get(), timeout=30)
                data = json.dumps({'type': update.type, 'content': update.content})
                await response.write(f'event: update\ndata: {data}\n\n'.encode())

                # Exit loop after sending return_to_terminal (session returning to terminal)
                if update.type == 'return_to_terminal':
                    logger.info('[SSE] Sent return_to_terminal, closing connection')
                    break
            except asyncio.TimeoutError:
                # Send keepalive
                await response.write(b'event: keepalive\ndata: {}\n\n')
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        # Track disconnection
        _sse_connection_count -= 1
        logger.info(f'[SSE] Connection closed for terminal {terminal_id[:8]}..., count={_sse_connection_count}')

        # Check if server should shut down (no more connections and no active TG session)
        if _sse_connection_count == 0 and session.client is None:
            logger.info('[SSE] No connections and no active session, triggering shutdown')
            _trigger_shutdown()

    return response


async def handle_setup_link_register(request: web.Request) -> web.Response:
    """Register a setup link token. Called by setup wizard."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({'error': 'Invalid JSON'}, status=400)

    token = data.get('token', '').upper()
    if not token:
        return web.json_response({'error': 'token required'}, status=400)

    # Register the pending link
    _pending_setup_links[token] = PendingSetupLink(
        token=token,
        event=asyncio.Event(),
    )

    return web.json_response({'ok': True, 'message': 'Link token registered'})


async def handle_setup_link_wait(request: web.Request) -> web.Response:
    """Wait for a setup link to complete. Long-polling endpoint."""
    token = request.match_info.get('token', '').upper()

    if token not in _pending_setup_links:
        return web.json_response({'error': 'Token not registered'}, status=404)

    pending = _pending_setup_links[token]

    # Wait for the link to complete (with timeout)
    try:
        await asyncio.wait_for(pending.event.wait(), timeout=300)
    except asyncio.TimeoutError:
        _pending_setup_links.pop(token, None)
        return web.json_response({'error': 'Timeout waiting for link'}, status=408)

    # Link completed
    result = pending.result
    _pending_setup_links.pop(token, None)

    if result:
        user_id, username = result
        return web.json_response({'ok': True, 'user_id': user_id, 'username': username})
    else:
        return web.json_response({'error': 'Link failed'}, status=500)


def create_http_app(config: Config) -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app['config'] = config

    app.router.add_post('/teleport', handle_teleport)
    app.router.add_get('/health', handle_health)
    app.router.add_post('/api/prepare-reload', handle_prepare_reload)
    app.router.add_get('/stream', handle_stream)

    # Setup link endpoints
    app.router.add_post('/api/setup-link', handle_setup_link_register)
    app.router.add_get('/api/setup-link/{token}', handle_setup_link_wait)

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Telegram Handlers
# ─────────────────────────────────────────────────────────────────────────────


async def tg_handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    user_id = update.effective_user.id

    if user_id != config.telegram.user_id:
        await update.message.reply_text(f'Not authorized. Your user ID: {user_id}')
        return

    await update.message.reply_text(
        '📱 rclaude - Claude Code Remote\n\n'
        '<b>Session:</b>\n'
        '/new - Start a new session\n'
        '/cc - Return to terminal\n'
        '/status - Show session status\n'
        '/stop - Interrupt current task\n\n'
        '<b>Settings:</b>\n'
        '/mode - Change permission mode\n'
        '/model - Change AI model\n\n'
        '<b>Context:</b>\n'
        '/context - Show context usage\n'
        '/compact - Compact conversation\n'
        '/todos - Show TODO items\n'
        '/cost - Show usage and cost\n\n'
        'Or just send a message to interact with Claude.',
        parse_mode='HTML',
    )


async def tg_handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)

    if session.client:
        await session.client.disconnect()
        session.client = None

    session.pending_question = None
    session.is_processing = False

    # Clear pending teleport
    _pending_teleports.pop(update.effective_user.id, None)

    await update.message.reply_text('✓ Session cleared. Ready for new conversation.')


async def tg_handle_cc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cc command - teleport back to terminal."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)

    if not session.client:
        await update.message.reply_text('No active session. Start one first.')
        return

    if not session.session_id:
        await update.message.reply_text('No resumable session (started fresh). Use terminal directly.')
        # Just clear the client without trying to resume
        session.client = None
        return

    # Signal wrapper to return to terminal
    session_id = session.session_id
    await session.update_queue.put(SessionUpdate('return_to_terminal', session_id))

    # Don't call disconnect() - causes cancel scope errors with concurrent_updates
    # Just clear the reference; SDK will clean up
    session.client = None

    await update.message.reply_text(
        f'💻 Returning to terminal...\n\nSession: `{session_id[:8]}...`\nDirectory: `{session.cwd}`',
        parse_mode='Markdown',
    )


async def tg_handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)

    mode_short = _format_mode_short(session.permission_mode)
    status_lines = [
        f'Working directory: `{session.cwd}`',
        f'Session active: {"Yes" if session.client else "No"}',
        f'Processing: {"Yes" if session.is_processing else "No"}',
        f'Mode: {mode_short} {session.permission_mode}',
    ]

    if update.effective_user.id in _pending_teleports:
        tp = _pending_teleports[update.effective_user.id]
        status_lines.append(f'Pending teleport: `{tp.session_id[:8]}...`')

    await update.message.reply_text('\n'.join(status_lines), parse_mode='Markdown')


async def tg_handle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mode command - show and switch permission modes."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)

    # Check for argument: /mode default, /mode accept, etc
    text = update.message.text or ''
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        mode_arg = parts[1].strip().lower()
        mode_map = {
            'default': 'default',
            'accept': 'acceptEdits',
            'acceptedits': 'acceptEdits',
            'plan': 'plan',
            'dangerous': 'bypassPermissions',
            'bypass': 'bypassPermissions',
        }
        new_mode = mode_map.get(mode_arg)
        if new_mode:
            session.permission_mode = cast(PermissionMode, new_mode)
            await update.message.reply_text(
                f'✓ Mode changed to: {_format_mode_display(new_mode)}',
                parse_mode='HTML',
            )
            # Update pinned status message
            assert update.effective_chat
            await _update_pinned_message(context.bot, update.effective_chat.id, session)
            return
        else:
            await update.message.reply_text(f'Unknown mode: {mode_arg}\n\nValid modes: default, accept, plan, dangerous')
            return

    # No argument - show current mode with keyboard
    keyboard = create_mode_keyboard(session.permission_mode)
    await update.message.reply_text(
        f'<b>Permission Mode</b>\n\nCurrent: {_format_mode_display(session.permission_mode)}\n\n<i>Select a new mode below:</i>',
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def tg_handle_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command - show and switch AI models."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)

    # Check for argument: /model sonnet, /model opus, etc
    text = update.message.text or ''
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        model_arg = parts[1].strip().lower()
        model_map = {
            'sonnet': 'sonnet',
            'opus': 'opus',
            'haiku': 'haiku',
        }
        new_model = model_map.get(model_arg, model_arg)  # Allow full model names too

        if session.client:
            try:
                await session.client.set_model(new_model)
                session.current_model = new_model
                await update.message.reply_text(f'✓ Model changed to: <b>{new_model}</b>', parse_mode='HTML')
                # Update pinned status message
                assert update.effective_chat
                await _update_pinned_message(context.bot, update.effective_chat.id, session)
            except Exception as e:
                await update.message.reply_text(f'Failed to change model: {e}')
        else:
            session.current_model = new_model
            await update.message.reply_text(
                f'✓ Model set to: <b>{new_model}</b>\n<i>(Will apply on next session)</i>',
                parse_mode='HTML',
            )
        return

    # No argument - show current model with keyboard
    keyboard = create_model_keyboard(session.current_model)
    current = session.current_model or 'default (sonnet)'
    await update.message.reply_text(
        f'<b>AI Model</b>\n\nCurrent: <b>{current}</b>\n\n<i>Select a model below:</i>',
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def tg_handle_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cost command - show session cost and usage."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)
    usage = session.usage

    lines = ['<b>Session Usage</b>\n']

    if usage.total_cost_usd > 0:
        lines.append(f'💰 Total cost: <b>${usage.total_cost_usd:.4f}</b>')
    else:
        lines.append('💰 Total cost: <i>Not available</i>')

    lines.append(f'📊 Turns: {usage.num_turns}')

    if usage.total_input_tokens > 0 or usage.total_output_tokens > 0:
        lines.append(f'📥 Input tokens: {usage.total_input_tokens:,}')
        lines.append(f'📤 Output tokens: {usage.total_output_tokens:,}')

    if usage.last_response_cost is not None:
        lines.append(f'\n<i>Last response: ${usage.last_response_cost:.4f}</i>')

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def _fetch_context_silently(session: UserSession) -> None:
    """Fetch context usage without displaying output."""
    if not session.client:
        return

    try:
        await session.client.query('/context')
        # Process messages silently, only extracting context
        async for message in session.client.receive_response():
            if isinstance(message, UserMessage):
                # /context output comes in UserMessage with local-command-stdout
                # content can be a string or a list of blocks
                content = message.content
                if isinstance(content, str):
                    if '<local-command-stdout>' in content:
                        context_usage = _parse_context_output(content)
                        if context_usage:
                            session.context = context_usage
                            return
                else:
                    for block in content:
                        if isinstance(block, TextBlock) and '<local-command-stdout>' in block.text:
                            context_usage = _parse_context_output(block.text)
                            if context_usage:
                                session.context = context_usage
                                return
            elif isinstance(message, SystemMessage):
                # Fallback for other formats
                data = message.data
                text_content = data.get('message') or data.get('text') or data.get('content') or data.get('result')
                if text_content:
                    context_usage = _parse_context_output(str(text_content))
                    if context_usage:
                        session.context = context_usage
                        return
    except Exception as e:
        logger.warning(f'Failed to fetch context: {e}')


async def _proxy_slash_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
) -> None:
    """Proxy a slash command to Claude."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)

    if not session.client:
        await update.message.reply_text('No active session. Send a message to start one.')
        return

    # Send the slash command as a query
    await update.message.reply_text(f'⏳ Running {command}...')
    try:
        await session.client.query(command)
        await _process_response(update, context, session)
    except Exception as e:
        await update.message.reply_text(f'Failed: {e}')


async def tg_handle_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /compact command - compact conversation context."""
    text = (update.message.text or '').strip() if update.message else ''
    # Pass any arguments after /compact
    parts = text.split(maxsplit=1)
    command = f'/compact {parts[1]}' if len(parts) > 1 else '/compact'
    await _proxy_slash_command(update, context, command)


async def tg_handle_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /context command - show context usage."""
    await _proxy_slash_command(update, context, '/context')


async def tg_handle_todos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /todos command - show current TODO items."""
    await _proxy_slash_command(update, context, '/todos')


async def tg_handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop command."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    session = get_session(update.effective_user.id)

    if session.client and session.is_processing:
        try:
            await session.client.interrupt()
            await update.message.reply_text('✓ Task interrupted.')
        except Exception as e:
            await update.message.reply_text(f'Failed to interrupt: {e}')
    else:
        await update.message.reply_text('No active task to interrupt.')


async def tg_handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command - cancel pending teleport."""
    assert update.effective_user
    assert update.message

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    if update.effective_user.id in _pending_teleports:
        del _pending_teleports[update.effective_user.id]
        await update.message.reply_text('✓ Teleport cancelled.')
    else:
        await update.message.reply_text('No pending teleport.')


async def tg_handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /link command - link Telegram account during setup."""
    assert update.effective_user
    assert update.message

    args = context.args or []
    if not args:
        await update.message.reply_text('Usage: /link <token>\n\nThe token was shown in your terminal during setup.')
        return

    provided_token = args[0].upper()

    # Check if this token is pending
    if provided_token not in _pending_setup_links:
        await update.message.reply_text('Invalid or expired token. Please check and try again.')
        return

    pending = _pending_setup_links[provided_token]
    user_id = update.effective_user.id
    username = update.effective_user.username or str(user_id)

    # Store result and signal completion
    pending.result = (user_id, username)
    pending.event.set()

    await update.message.reply_text(f'✓ Linked! You can close this chat.\n\nUser ID: {user_id}')


async def tg_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks for questions and permissions."""
    print('[CALLBACK] tg_handle_callback ENTERED', flush=True)
    logger.info('[CALLBACK] tg_handle_callback ENTERED')
    try:
        assert update.effective_user
        assert update.callback_query
        assert update.effective_chat
    except AssertionError as e:
        logger.error(f'[CALLBACK] Assertion failed: {e}, update={update}')
        return

    logger.info(f'[CALLBACK] Received callback query from user {update.effective_user.id}')

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        logger.warning(f'[CALLBACK] Unauthorized user {update.effective_user.id}')
        return

    query = update.callback_query
    await query.answer()

    session = get_session(update.effective_user.id)

    assert query.data
    data = query.data
    logger.info(f'[CALLBACK] Callback data: {data}')

    # Handle permission callbacks
    if data.startswith('perm:'):
        logger.info('[CALLBACK] Handling permission callback')
        await _handle_permission_callback(update, context, session, data)
        return

    # Handle question callbacks
    if data.startswith('q:'):
        logger.info('[CALLBACK] Handling question callback')
        await _handle_question_callback(update, context, session, data)
        return

    # Handle mode selection callbacks
    if data.startswith('mode:'):
        logger.info('[CALLBACK] Handling mode callback')
        await _handle_mode_callback(update, context, session, data)
        return

    # Handle model selection callbacks
    if data.startswith('model:'):
        logger.info('[CALLBACK] Handling model callback')
        await _handle_model_callback(update, context, session, data)
        return

    logger.warning(f'[CALLBACK] Unknown callback data: {data}')


async def _handle_permission_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: UserSession,
    data: str,
) -> None:
    """Handle permission inline keyboard callbacks."""
    assert update.callback_query
    query = update.callback_query

    pending = session.pending_permission
    logger.info(f'[PERM_CALLBACK] pending_permission: {pending}')
    if not pending:
        logger.warning('[PERM_CALLBACK] No pending permission found!')
        await query.edit_message_text('No pending permission request.')
        return

    action = data.split(':')[1]
    logger.info(f'[PERM_CALLBACK] Action: {action}, request_id: {pending.request_id}')

    if action == 'allow':
        # Allow once
        pending.result = PermissionResultAllow(updated_input=pending.input_data)
        await query.edit_message_text('✓ Allowed (once)')
        logger.info('[PERM_CALLBACK] Setting event for allow')
        pending.event.set()
        logger.info('[PERM_CALLBACK] Event set!')

    elif action == 'always':
        # Generate smart rule for Bash, simple rule for others
        if pending.tool_name == 'Bash':
            command = pending.input_data.get('command', '')
            await query.edit_message_text('⏳ Generating smart rule...', parse_mode='HTML')
            rule = await generate_smart_bash_rule(command)
        else:
            rule = generate_permission_rule(pending.tool_name, pending.input_data)

        # Add to CC permission file, then allow
        add_permission_rule_to_file(session.cwd, rule)
        pending.result = PermissionResultAllow(updated_input=pending.input_data)
        await query.edit_message_text(f'✓ Allowed (always)\n<code>{rule}</code>', parse_mode='HTML')
        logger.info('[PERM_CALLBACK] Setting event for always-allow')
        pending.event.set()

    elif action == 'accept_edits':
        # Enable acceptEdits mode and allow this tool
        session.permission_mode = cast(PermissionMode, 'acceptEdits')
        pending.result = PermissionResultAllow(updated_input=pending.input_data)
        await query.edit_message_text(
            '✓ Allowed\n\n📝 <b>Accept Edits mode enabled</b>\nFile changes will be auto-approved.',
            parse_mode='HTML',
        )
        logger.info('[PERM_CALLBACK] Enabled acceptEdits mode')
        pending.event.set()

    elif action == 'reject':
        # Ask for rejection reason
        session.waiting_for_rejection_reason = True
        await query.edit_message_text('Type your rejection reason:')
        logger.info('[PERM_CALLBACK] Waiting for rejection reason')
        # Don't signal yet - wait for text input


async def _handle_question_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: UserSession,
    data: str,
) -> None:
    """Handle question inline keyboard callbacks."""
    assert update.callback_query
    assert update.effective_chat
    query = update.callback_query

    if not session.pending_question:
        await query.edit_message_text('No pending question.')
        return

    parts = data.split(':')
    if len(parts) != 3:
        return

    _, _q_idx, opt_idx = parts

    pending = session.pending_question
    current_q = pending.questions[pending.current_question_idx]

    if opt_idx == 'other':
        await query.edit_message_text(f'Question: {current_q["question"]}\n\nType your answer:')
        assert context.user_data is not None
        context.user_data['waiting_for_answer'] = True
        return

    opt_idx_int = int(opt_idx)
    options = current_q.get('options', [])
    if opt_idx_int < len(options):
        selected = options[opt_idx_int]['label']
        pending.answers[current_q['question']] = selected

        await query.edit_message_text(f'Selected: {selected}')
        pending.current_question_idx += 1

        if pending.current_question_idx < len(pending.questions):
            next_q = pending.questions[pending.current_question_idx]
            keyboard = await create_question_keyboard(next_q)
            await update.effective_chat.send_message(
                f'{next_q.get("header", "Question")}: {next_q["question"]}',
                reply_markup=keyboard,
                disable_notification=False,  # Sound enabled - requires user action
            )
        else:
            session.pending_question = None
            await _continue_after_question(update, context, session, pending.answers)


async def _continue_after_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: UserSession,
    answers: dict[str, str],
) -> None:
    """Continue after AskUserQuestion is answered."""
    answer_text = '\n'.join(f'{q}: {a}' for q, a in answers.items())

    if session.client:
        await session.client.query(answer_text)
        await _process_response(update, context, session)


async def _handle_mode_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: UserSession,
    data: str,
) -> None:
    """Handle mode selection inline keyboard callbacks."""
    assert update.callback_query
    query = update.callback_query

    # Extract mode from callback data: "mode:default", "mode:acceptEdits", etc
    mode = data.split(':', 1)[1]
    if mode not in VALID_MODES:
        await query.edit_message_text(f'Unknown mode: {mode}')
        return

    session.permission_mode = cast(PermissionMode, mode)
    await query.edit_message_text(
        f'✓ Mode changed to: {_format_mode_display(mode)}',
        parse_mode='HTML',
    )
    # Update pinned status message
    assert update.effective_chat
    await _update_pinned_message(context.bot, update.effective_chat.id, session)


async def _handle_model_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: UserSession,
    data: str,
) -> None:
    """Handle model selection inline keyboard callbacks."""
    assert update.callback_query
    query = update.callback_query

    # Extract model from callback data: "model:sonnet", "model:opus", etc
    model = data.split(':', 1)[1]

    if session.client:
        try:
            await session.client.set_model(model)
            session.current_model = model
            await query.edit_message_text(f'✓ Model changed to: <b>{model}</b>', parse_mode='HTML')
            # Update pinned status message
            assert update.effective_chat
            await _update_pinned_message(context.bot, update.effective_chat.id, session)
        except Exception as e:
            await query.edit_message_text(f'Failed to change model: {e}')
    else:
        session.current_model = model
        await query.edit_message_text(
            f'✓ Model set to: <b>{model}</b>\n<i>(Will apply on next session)</i>',
            parse_mode='HTML',
        )


async def _handle_message_with_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: UserSession,
    caption_text: str,
) -> None:
    """Handle a message with photo - download, encode, and send to Claude."""
    assert update.message
    assert update.effective_user
    assert session.client

    user_id = update.effective_user.id
    image_path: Path | None = None

    if not update.message.photo:
        logger.error('[IMAGE] No photo in message despite has_photo flag')
        await update.message.reply_text('❌ No image found')
        return

    try:
        # Show processing indicator
        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)

        # Download image from Telegram
        logger.info('[IMAGE] Downloading photo from Telegram...')
        image_path = await download_telegram_photo(context.bot, update.message.photo, user_id)

        if not image_path:
            await update.message.reply_text('❌ Failed to download image')
            return

        # Prepare image for Claude (encode to base64)
        logger.info('[IMAGE] Preparing image for Claude...')
        image_data = await prepare_image_for_claude(image_path)

        if not image_data:
            await update.message.reply_text('❌ Failed to process image')
            cleanup_image_file(image_path)
            return

        base64_str, mime_type = image_data
        logger.info(f'[IMAGE] Image prepared: {mime_type}, {len(base64_str)} chars base64')

        # Show uploading status
        await update.message.reply_text('📸 Image received, analyzing...')

        # Build message with image data as base64 URI
        # The SDK's query() method expects a string, not content blocks
        image_uri = f'data:{mime_type};base64,{base64_str}'

        if caption_text.strip():
            message = f'{caption_text}\n\n{image_uri}'
        else:
            message = image_uri

        # Send to Claude as a text message with embedded image data
        logger.info('[IMAGE] Sending to Claude...')
        await session.client.query(message)
        await _process_response(update, context, session)

    except Exception as e:
        logger.error(f'[IMAGE] Error handling image: {e}', exc_info=True)
        await update.message.reply_text(f'❌ Error processing image: {e}')

    finally:
        # Cleanup temp image file
        if image_path and image_path.exists():
            cleanup_image_file(image_path)


async def _process_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: UserSession,
) -> None:
    """Process Claude response with rich Telegram formatting."""
    assert update.effective_chat

    if not session.client:
        return

    session.is_processing = True
    response_text = ''
    # Track tool call messages for editing with results: tool_use_id -> (message_id, text)
    tool_messages: dict[str, tuple[int, str]] = {}
    # Track whether we've received ResultMessage (signals task completion)
    is_final_message = False

    try:
        async for message in session.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text

                    elif isinstance(block, ToolUseBlock):
                        # Send any accumulated text first (silently - intermediate processing)
                        if response_text.strip():
                            await send_text(update, response_text, disable_notification=True)
                            await session.update_queue.put(SessionUpdate('text', response_text))
                            response_text = ''

                        # Handle AskUserQuestion specially
                        if block.name == 'AskUserQuestion':
                            questions = block.input.get('questions', [])
                            if questions:
                                session.pending_question = PendingQuestion(
                                    tool_use_id=block.id,
                                    questions=questions,
                                )
                                first_q = questions[0]
                                keyboard = await create_question_keyboard(first_q)
                                await update.effective_chat.send_message(
                                    f'<b>{first_q.get("header", "Question")}:</b> {first_q["question"]}',
                                    reply_markup=keyboard,
                                    parse_mode='HTML',
                                    disable_notification=False,  # Sound enabled - requires user action
                                )
                                await session.update_queue.put(SessionUpdate('question', first_q['question']))
                                session.is_processing = False
                                return

                        # Send tool call as formatted message and track for result editing (silently)
                        msg_info = await send_tool_call(update, block, disable_notification=True)
                        if msg_info and block.id:
                            tool_messages[block.id] = msg_info
                        tool_desc = (
                            f'{block.name}: {block.input.get("command", block.input.get("file_path", block.input.get("pattern", "")))}'
                        )
                        await session.update_queue.put(SessionUpdate('tool_call', tool_desc))

            elif isinstance(message, UserMessage):
                # Tool results and local command outputs come in UserMessage
                # content can be a string or a list of blocks
                content = message.content
                if isinstance(content, str):
                    # Raw string content (e.g., local command output)
                    if '<local-command-stdout>' in content:
                        context_usage = _parse_context_output(content)
                        if context_usage:
                            session.context = context_usage
                            logger.info(f'[CONTEXT] Parsed from UserMessage string: {context_usage.percent_used}%')
                else:
                    # List of blocks
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            # Send tool result silently (intermediate processing)
                            msg_info = tool_messages.get(block.tool_use_id)
                            await send_tool_result(update, block, msg_info, disable_notification=True)
                        elif isinstance(block, TextBlock):
                            if '<local-command-stdout>' in block.text:
                                context_usage = _parse_context_output(block.text)
                                if context_usage:
                                    session.context = context_usage
                                    logger.info(f'[CONTEXT] Parsed from UserMessage block: {context_usage.percent_used}%')

            elif isinstance(message, SystemMessage):
                # Handle system messages (slash command outputs, etc.)
                logger.info(f'[SYSTEM] subtype={message.subtype} data={message.data}')
                data = message.data
                # Extract text content from various system message formats
                text_content = None
                if 'message' in data:
                    text_content = data['message']
                elif 'text' in data:
                    text_content = data['text']
                elif 'content' in data:
                    text_content = data['content']
                elif 'result' in data:
                    text_content = data['result']

                if text_content:
                    text_str = str(text_content)
                    response_text += text_str

                    # Try to parse context usage from /context output
                    context_usage = _parse_context_output(text_str)
                    if context_usage:
                        session.context = context_usage
                        logger.info(f'[CONTEXT] Parsed: {context_usage.tokens_used}/{context_usage.tokens_max} ({context_usage.percent_used}%)')

            elif isinstance(message, ResultMessage):
                if message.is_error and message.result:
                    response_text += f'\n\n❌ Error: {message.result}'
                # Capture session_id for /cc functionality
                if message.session_id and not session.session_id:
                    session.session_id = message.session_id
                    logger.info(f'[SESSION] Captured session_id from ResultMessage: {message.session_id[:8]}...')

                # Track usage and cost
                session.usage.num_turns += message.num_turns
                if message.total_cost_usd is not None:
                    session.usage.last_response_cost = message.total_cost_usd
                    session.usage.total_cost_usd += message.total_cost_usd
                if message.usage:
                    session.usage.last_response_tokens = message.usage
                    session.usage.total_input_tokens += message.usage.get('input_tokens', 0)
                    session.usage.total_output_tokens += message.usage.get('output_tokens', 0)
                logger.info(f'[USAGE] Cost: ${message.total_cost_usd or 0:.4f}, Total: ${session.usage.total_cost_usd:.4f}')

                # Mark that the next message should be the final one (with sound notification)
                is_final_message = True

    except Exception as e:
        logger.error(f'Error processing response: {e}')
        response_text += f'\n\n❌ Error: {e}'
        # Errors are also final messages (should notify with sound)
        is_final_message = True

    finally:
        session.is_processing = False

    # Send any remaining text - with sound notification if this is the final message
    if response_text.strip():
        await send_text(update, response_text, disable_notification=not is_final_message)
        await session.update_queue.put(SessionUpdate('text', response_text))

    # Update pinned status message (cost/context may have changed)
    assert update.effective_chat
    await _update_pinned_message(context.bot, update.effective_chat.id, session)


async def tg_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text and photo messages."""
    assert update.effective_user
    assert update.message
    assert update.message.chat
    assert context.user_data is not None

    config: Config = context.bot_data['config']
    user_id = update.effective_user.id

    if user_id != config.telegram.user_id:
        await update.message.reply_text(f'Not authorized. Your user ID: {user_id}')
        return

    session = get_session(user_id)

    # Extract text and check for photos
    text = update.message.text or update.message.caption or ''
    has_photo = bool(update.message.photo)  # Check if photo list is non-empty

    # Handle case where photo has no caption/text
    if has_photo and not text:
        text = ''

    # Check for pending teleport
    if user_id in _pending_teleports:
        teleport = _pending_teleports.pop(user_id)
        session.cwd = teleport.cwd
        session.terminal_id = teleport.terminal_id
        session.permission_mode = cast(PermissionMode, _validate_permission_mode(teleport.permission_mode))

        # Check if we can resume the session (has conversation history)
        resumable = can_resume_session(teleport.session_id, teleport.cwd)

        # Build options - only include resume if session has content
        try:
            # Get bot for permission handler
            bot = context.bot
            permission_handler = create_permission_handler(bot, user_id, session)

            # Try to resume, but fall back to fresh session if it fails
            resume_id = teleport.session_id if resumable else None

            options = ClaudeAgentOptions(
                # Don't pass tools - defaults to all tools available (like CLI)
                # Don't use allowed_tools - it creates permission ALLOW rules that bypass can_use_tool!
                setting_sources=['user', 'project', 'local'],  # Load CC permission rules
                permission_mode=session.permission_mode,  # Use teleported mode
                can_use_tool=permission_handler,  # Interactive approval via Telegram
                cwd=session.cwd,
                resume=resume_id,
                cli_path=get_local_claude_cli(),
            )
            session.client = ClaudeSDKClient(options=options)
            # Only track session_id if we're actually resuming (for /cc to work)
            session.session_id = teleport.session_id if resumable else None
            logger.info(
                f'[DEBUG] Teleport: connecting with can_use_tool={options.can_use_tool is not None}, resume={resume_id is not None}'
            )

            try:
                await session.client.connect()
                logger.info('[DEBUG] Teleport: connected successfully')
            except Exception as connect_err:
                # If resume failed, try fresh session
                if resume_id:
                    logger.warning(f'[DEBUG] Resume failed, trying fresh session: {connect_err}')
                    options = ClaudeAgentOptions(
                        setting_sources=['user', 'project', 'local'],
                        permission_mode=session.permission_mode,
                        can_use_tool=permission_handler,
                        cwd=session.cwd,
                        resume=None,  # Fresh session
                        cli_path=get_local_claude_cli(),
                    )
                    session.client = ClaudeSDKClient(options=options)
                    await session.client.connect()
                    resumable = False  # Update for message below
                    session.session_id = None  # Can't resume this session
                    logger.info('[DEBUG] Teleport: connected with fresh session')
                else:
                    raise

            # Fetch context usage silently
            await _fetch_context_silently(session)

            # Send session start message
            if resumable:
                await update.message.reply_text('✓ Session resumed')
            else:
                await update.message.reply_text('✓ Connected (fresh session)')

            # Create/update pinned status message
            assert update.effective_chat
            await _update_pinned_message(context.bot, update.effective_chat.id, session)
        except Exception as e:
            logger.error(f'[DEBUG] Teleport failed: {e}')
            await update.message.reply_text(f'Failed to connect: {e}')
            return

    # Push user message to stream
    await session.update_queue.put(SessionUpdate('user', text))

    # Handle waiting for rejection reason
    if session.waiting_for_rejection_reason and session.pending_permission:
        session.waiting_for_rejection_reason = False
        pending = session.pending_permission
        pending.result = PermissionResultDeny(
            message=text,
            interrupt=False,  # Let Claude try something else
        )
        pending.event.set()
        await update.message.reply_text(f'✗ Rejected: {text}')
        return

    # Handle waiting for custom answer
    if context.user_data.get('waiting_for_answer') and session.pending_question:
        context.user_data['waiting_for_answer'] = False
        pending = session.pending_question
        current_q = pending.questions[pending.current_question_idx]
        pending.answers[current_q['question']] = text

        pending.current_question_idx += 1

        if pending.current_question_idx < len(pending.questions):
            next_q = pending.questions[pending.current_question_idx]
            keyboard = await create_question_keyboard(next_q)
            await update.message.reply_text(
                f'{next_q.get("header", "Question")}: {next_q["question"]}',
                reply_markup=keyboard,
            )
        else:
            session.pending_question = None
            await _continue_after_question(update, context, session, pending.answers)
        return

    if session.is_processing:
        await update.message.reply_text('⏳ Still processing. Use /stop to interrupt.')
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        if not session.client:
            # Get bot for permission handler
            bot = context.bot
            permission_handler = create_permission_handler(bot, user_id, session)

            # Check if we should resume a saved session (from hot-reload)
            resume_session = None
            if session.session_id and can_resume_session(session.session_id, session.cwd):
                resume_session = session.session_id

            options = ClaudeAgentOptions(
                # Don't pass tools - defaults to all tools available (like CLI)
                # Don't use allowed_tools - it creates permission ALLOW rules that bypass can_use_tool!
                setting_sources=['user', 'project', 'local'],  # Load CC permission rules
                permission_mode='default',  # Use default mode - SDK handles via can_use_tool
                can_use_tool=permission_handler,  # Interactive approval via Telegram
                # Required: PreToolUse hook keeps stream open for can_use_tool callback
                # hooks={'PreToolUse': [HookMatcher(matcher=None, hooks=[dummy_pretool_hook])]},
                cwd=session.cwd,
                resume=resume_session,
                cli_path=get_local_claude_cli(),
            )
            session.client = ClaudeSDKClient(options=options)
            logger.info(f'[DEBUG] Connecting with can_use_tool={options.can_use_tool is not None}')
            try:
                await session.client.connect()
                logger.info('[DEBUG] Connected successfully')
            except ValueError as e:
                logger.error(f'[DEBUG] ValueError during connect: {e}')
                raise

            if resume_session:
                await update.message.reply_text('✓ Session resumed after reload.')

        # Handle image if present
        if has_photo:
            await _handle_message_with_image(update, context, session, text)
        else:
            await session.client.query(text)
            await _process_response(update, context, session)

    except Exception as e:
        logger.error(f'Error: {e}', exc_info=True)
        await update.message.reply_text(f'❌ Error: {e}')

        if session.client:
            try:
                await session.client.disconnect()
            except Exception:
                pass
            session.client = None


async def tg_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors."""
    print(f'[TG_ERROR] Error: {context.error}', flush=True)
    logger.error(f'[TG_ERROR] Error: {context.error}', exc_info=context.error)


async def debug_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug handler to log ALL updates."""
    print(f'[DEBUG_ALL] Update type: {type(update)}', flush=True)
    print(f'[DEBUG_ALL] Has callback_query: {update.callback_query is not None}', flush=True)
    if update.callback_query:
        print(f'[DEBUG_ALL] Callback data: {update.callback_query.data}', flush=True)


def create_telegram_app(config: Config) -> Application:
    """Create the Telegram application."""
    # Enable concurrent updates so callback queries can be processed while waiting for permission
    app = Application.builder().token(config.telegram.bot_token).concurrent_updates(True).build()
    app.bot_data['config'] = config

    app.add_handler(CommandHandler('stop', tg_handle_stop))
    app.add_handler(CommandHandler('context', tg_handle_context))
    app.add_handler(CommandHandler('compact', tg_handle_compact))
    app.add_handler(CommandHandler('cc', tg_handle_cc))
    app.add_handler(CommandHandler('todos', tg_handle_todos))
    app.add_handler(CommandHandler('model', tg_handle_model))
    app.add_handler(CommandHandler('mode', tg_handle_mode))
    app.add_handler(CommandHandler('cost', tg_handle_cost))
    app.add_handler(CommandHandler('status', tg_handle_status))
    app.add_handler(CommandHandler('new', tg_handle_new))
    app.add_handler(CommandHandler('start', tg_handle_start))
    app.add_handler(CommandHandler('cancel', tg_handle_cancel))
    app.add_handler(CommandHandler('link', tg_handle_link))
    app.add_handler(CallbackQueryHandler(tg_handle_callback))
    # Handle both text messages and photos (with optional captions)
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO, tg_handle_message))

    # Debug: catch ALL updates in a separate group
    from telegram.ext import TypeHandler

    app.add_handler(TypeHandler(Update, debug_all_updates), group=999)

    app.add_error_handler(tg_error_handler)

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Combined Server
# ─────────────────────────────────────────────────────────────────────────────


async def run_server(config: Config) -> None:
    """Run both HTTP and Telegram servers."""
    # Create apps
    http_app = create_http_app(config)
    tg_app = create_telegram_app(config)

    # Store telegram app reference for HTTP handlers
    http_app['telegram_app'] = tg_app

    # Start HTTP server
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, config.server.host, config.server.port)
    await site.start()

    logger.info(f'HTTP server running on {config.server.host}:{config.server.port}')

    # Start Telegram bot
    async with tg_app:
        await tg_app.start()

        # Register commands with Telegram for menu display
        await tg_app.bot.set_my_commands([
            ('stop', 'Interrupt current task'),
            ('context', 'Show context usage'),
            ('compact', 'Compact conversation'),
            ('cc', 'Return to terminal'),
            ('todos', 'Show TODO items'),
            ('model', 'Change AI model'),
            ('mode', 'Change permission mode'),
            ('cost', 'Show usage and cost'),
            ('status', 'Show session status'),
            ('new', 'Start a new session'),
            ('start', 'Show help'),
            ('cancel', 'Cancel pending teleport'),
        ])

        assert tg_app.updater is not None
        await tg_app.updater.start_polling(
            allowed_updates=['message', 'callback_query', 'edited_message'],
            drop_pending_updates=True,
        )

        logger.info('Telegram bot started')

        # Check for saved session state from hot-reload
        saved_state = load_session_state()
        if saved_state and config.telegram.user_id:
            user_id = config.telegram.user_id
            if user_id in saved_state:
                state = saved_state[user_id]
                # Restore session metadata
                session = get_session(user_id)
                session.cwd = state.get('cwd', os.getcwd())
                session.session_id = state.get('session_id')

                # Notify user to continue
                try:
                    await tg_app.bot.send_message(
                        chat_id=user_id,
                        text='🔄 Server reloaded. Send any message to reconnect to your session.',
                    )
                    logger.info(f'Notified user {user_id} to reconnect after hot-reload')
                except Exception as e:
                    logger.warning(f'Failed to notify user after reload: {e}')

                # Clear saved state
                clear_session_state()

        # Wait for shutdown signal or external cancellation
        shutdown_event = _get_shutdown_event()
        try:
            await shutdown_event.wait()
            logger.info('[SERVER] Shutdown event received, stopping...')
        except asyncio.CancelledError:
            logger.info('[SERVER] Cancelled, stopping...')

        await tg_app.updater.stop()
        await tg_app.stop()

    await runner.cleanup()
