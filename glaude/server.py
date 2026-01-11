"""Combined HTTP + Telegram server for glaude."""

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

from aiohttp import web
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ChatAction

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    HookMatcher,
)
from claude_agent_sdk.types import HookInput, HookContext, SyncHookJSONOutput

from glaude.settings import Config
from glaude.session import (
    get_session,
    PendingQuestion,
    PendingPermission,
    UserSession,
    SessionUpdate,
    save_session_state,
    load_session_state,
    clear_session_state,
)
from glaude.formatting import (
    send_text,
    send_tool_call,
    send_tool_result,
    create_question_keyboard,
    format_permission_prompt,
    create_permission_keyboard,
)


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

# Tools that require interactive approval
APPROVAL_REQUIRED_TOOLS = {'Edit', 'Write', 'Bash', 'NotebookEdit'}


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


async def add_permission_rule(cwd: str, tool_name: str, input_data: dict[str, Any]) -> None:
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

    # Generate rule pattern
    rule = generate_permission_rule(tool_name, input_data)

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
        logger.info(f'[PERMISSION] can_use_tool called: tool={tool_name}')
        # Auto-allow tools that don't need approval
        if tool_name not in APPROVAL_REQUIRED_TOOLS:
            logger.info(f'[PERMISSION] Auto-allowing {tool_name} (not in approval list)')
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
        keyboard = create_permission_keyboard()

        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
            logger.info(f'[PERMISSION] Sent permission prompt to Telegram')
        except Exception as e:
            logger.error(f'Failed to send permission prompt: {e}')
            # On error, allow the operation (fail-open for usability)
            session.pending_permission = None
            return PermissionResultAllow(updated_input=input_data)

        # Wait for user response (no timeout - like CLI behavior)
        logger.info(f'[PERMISSION] Waiting for user response on event...')
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


logger = logging.getLogger('glaude')


@dataclass
class TeleportRequest:
    """A pending teleport from Claude Code."""

    session_id: str
    cwd: str


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

    if not session_id:
        return web.json_response({'error': 'session_id required'}, status=400)

    # Get the config to find the user
    config: Config = request.app['config']
    user_id = config.telegram.user_id

    if not user_id:
        return web.json_response({'error': 'No Telegram user configured'}, status=400)

    logger.info(f'Teleport received: session_id={session_id[:8]}..., cwd={cwd}')

    # Store pending teleport
    _pending_teleports[user_id] = TeleportRequest(session_id=session_id, cwd=cwd)

    # Notify via Telegram
    bot = request.app['telegram_app'].bot
    try:
        await bot.send_message(
            chat_id=user_id,
            text=f'📱 Session teleported from terminal!\n\n'
            f'Session: `{session_id[:8]}...`\n'
            f'Directory: `{cwd}`\n\n'
            f'Send any message to continue, or /cancel to ignore.',
            parse_mode='Markdown',
        )
    except Exception as e:
        logger.error(f'Failed to send Telegram notification: {e}')
        return web.json_response({'error': f'Failed to notify: {e}'}, status=500)

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
    config: Config = request.app['config']
    user_id = config.telegram.user_id

    if not user_id:
        return web.json_response({'error': 'No user configured'}, status=400)

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

    # Send initial connection message
    await response.write(b'event: connected\ndata: {}\n\n')

    try:
        while True:
            try:
                # Wait for updates with timeout
                update = await asyncio.wait_for(session.update_queue.get(), timeout=30)
                data = json.dumps({'type': update.type, 'content': update.content})
                await response.write(f'event: update\ndata: {data}\n\n'.encode())
            except asyncio.TimeoutError:
                # Send keepalive
                await response.write(b'event: keepalive\ndata: {}\n\n')
    except asyncio.CancelledError:
        pass

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
        '📱 Glaude - Claude Code Remote\n\n'
        'Commands:\n'
        '/start - Show this help\n'
        '/new - Start a new session\n'
        '/cc - Get command to return to terminal\n'
        '/status - Show session status\n'
        '/stop - Interrupt current task\n\n'
        'Or just send a message to interact with Claude.'
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

    # Signal wrapper to return to terminal
    session_id = session.session_id or 'latest'
    await session.update_queue.put(SessionUpdate('return_to_terminal', session_id))

    # Disconnect the TG session
    if session.client:
        await session.client.disconnect()
        session.client = None

    await update.message.reply_text(
        f'💻 Returning to terminal...\n\nSession: `{session_id[:8] if len(session_id) > 8 else session_id}...`\nDirectory: `{session.cwd}`',
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

    status_lines = [
        f'Working directory: `{session.cwd}`',
        f'Session active: {"Yes" if session.client else "No"}',
        f'Processing: {"Yes" if session.is_processing else "No"}',
    ]

    if update.effective_user.id in _pending_teleports:
        tp = _pending_teleports[update.effective_user.id]
        status_lines.append(f'Pending teleport: `{tp.session_id[:8]}...`')

    await update.message.reply_text('\n'.join(status_lines), parse_mode='Markdown')


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
    logger.info(f'[CALLBACK] tg_handle_callback ENTERED')
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
        logger.info(f'[CALLBACK] Handling permission callback')
        await _handle_permission_callback(update, context, session, data)
        return

    # Handle question callbacks
    if data.startswith('q:'):
        logger.info(f'[CALLBACK] Handling question callback')
        await _handle_question_callback(update, context, session, data)
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
        logger.info(f'[PERM_CALLBACK] Setting event for allow')
        pending.event.set()
        logger.info(f'[PERM_CALLBACK] Event set!')

    elif action == 'always':
        # Add to CC permission file, then allow
        await add_permission_rule(session.cwd, pending.tool_name, pending.input_data)
        pending.result = PermissionResultAllow(updated_input=pending.input_data)
        rule = generate_permission_rule(pending.tool_name, pending.input_data)
        await query.edit_message_text(f'✓ Allowed (always)\n<code>{rule}</code>', parse_mode='HTML')
        logger.info(f'[PERM_CALLBACK] Setting event for always-allow')
        pending.event.set()

    elif action == 'reject':
        # Ask for rejection reason
        session.waiting_for_rejection_reason = True
        await query.edit_message_text('Type your rejection reason:')
        logger.info(f'[PERM_CALLBACK] Waiting for rejection reason')
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

    try:
        async for message in session.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text

                    elif isinstance(block, ToolUseBlock):
                        # Send any accumulated text first
                        if response_text.strip():
                            await send_text(update, response_text)
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
                                )
                                await session.update_queue.put(SessionUpdate('question', first_q['question']))
                                session.is_processing = False
                                return

                        # Send tool call as formatted message
                        await send_tool_call(update, block)
                        tool_desc = (
                            f'{block.name}: {block.input.get("command", block.input.get("file_path", block.input.get("pattern", "")))}'
                        )
                        await session.update_queue.put(SessionUpdate('tool_call', tool_desc))

            elif isinstance(message, UserMessage):
                # Tool results come in UserMessage
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        await send_tool_result(update, block)
                        # Don't send full tool results to terminal (too verbose)

            elif isinstance(message, ResultMessage):
                if message.is_error and message.result:
                    response_text += f'\n\n❌ Error: {message.result}'
                # Note: cost display removed (subscription-based)

    except Exception as e:
        logger.error(f'Error processing response: {e}')
        response_text += f'\n\n❌ Error: {e}'

    finally:
        session.is_processing = False

    # Send any remaining text
    if response_text.strip():
        await send_text(update, response_text)
        await session.update_queue.put(SessionUpdate('text', response_text))


async def tg_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages."""
    assert update.effective_user
    assert update.message
    assert update.message.text
    assert update.message.chat
    assert context.user_data is not None

    config: Config = context.bot_data['config']
    user_id = update.effective_user.id

    if user_id != config.telegram.user_id:
        await update.message.reply_text(f'Not authorized. Your user ID: {user_id}')
        return

    session = get_session(user_id)
    text = update.message.text

    # Check for pending teleport
    if user_id in _pending_teleports:
        teleport = _pending_teleports.pop(user_id)
        session.cwd = teleport.cwd

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
                permission_mode='default',  # Use default mode - SDK handles via can_use_tool
                can_use_tool=permission_handler,  # Interactive approval via Telegram
                cwd=session.cwd,
                resume=resume_id,
                cli_path=get_local_claude_cli(),
            )
            session.client = ClaudeSDKClient(options=options)
            session.session_id = teleport.session_id  # Track for /cc
            logger.info(f'[DEBUG] Teleport: connecting with can_use_tool={options.can_use_tool is not None}, resume={resume_id is not None}')

            try:
                await session.client.connect()
                logger.info('[DEBUG] Teleport: connected successfully')
            except Exception as connect_err:
                # If resume failed, try fresh session
                if resume_id:
                    logger.warning(f'[DEBUG] Resume failed, trying fresh session: {connect_err}')
                    options = ClaudeAgentOptions(
                        setting_sources=['user', 'project', 'local'],
                        permission_mode='default',
                        can_use_tool=permission_handler,
                        cwd=session.cwd,
                        resume=None,  # Fresh session
                        cli_path=get_local_claude_cli(),
                    )
                    session.client = ClaudeSDKClient(options=options)
                    await session.client.connect()
                    resumable = False  # Update for message below
                    logger.info('[DEBUG] Teleport: connected with fresh session')
                else:
                    raise

            if resumable:
                await update.message.reply_text('✓ Session resumed. Continuing...')
            else:
                await update.message.reply_text('✓ Connected. Starting fresh session.')
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

    app.add_handler(CommandHandler('start', tg_handle_start))
    app.add_handler(CommandHandler('new', tg_handle_new))
    app.add_handler(CommandHandler('cc', tg_handle_cc))
    app.add_handler(CommandHandler('status', tg_handle_status))
    app.add_handler(CommandHandler('stop', tg_handle_stop))
    app.add_handler(CommandHandler('cancel', tg_handle_cancel))
    app.add_handler(CommandHandler('link', tg_handle_link))
    app.add_handler(CallbackQueryHandler(tg_handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_handle_message))

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

        # Run forever
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

        await tg_app.updater.stop()
        await tg_app.stop()

    await runner.cleanup()
