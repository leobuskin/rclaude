"""Combined HTTP + Telegram server for glaude."""

import asyncio
import json
import logging
from dataclasses import dataclass

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ChatAction

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

from pathlib import Path

from glaude.settings import Config
from glaude.session import get_session, PendingQuestion, UserSession
from glaude.formatting import send_text, send_tool_call, send_tool_result, create_question_keyboard


def can_resume_session(session_id: str, cwd: str) -> bool:
    """Check if a session can be resumed (exists and has content)."""
    # Build the session file path (same logic as Claude Code uses)
    project_path = cwd.replace('/', '-').replace(':', '')
    if project_path.startswith('-'):
        project_path = project_path[1:]

    log_dir = Path.home() / '.claude' / 'projects' / f'-{project_path}'
    log_file = log_dir / f'{session_id}.jsonl'

    # Session must exist and have content
    return log_file.exists() and log_file.stat().st_size > 0


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

    # Get session ID from the client
    # Note: we'd need to track this - for now just show the cwd
    await update.message.reply_text(
        f'💻 Return to terminal:\n\n```\ncd {session.cwd}\nclaude --resume\n```\n\nOr start fresh with: `claude`',
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
    """Handle inline keyboard callbacks."""
    assert update.effective_user
    assert update.callback_query
    assert update.effective_chat

    config: Config = context.bot_data['config']
    if update.effective_user.id != config.telegram.user_id:
        return

    query = update.callback_query
    await query.answer()

    session = get_session(update.effective_user.id)

    if not session.pending_question:
        await query.edit_message_text('No pending question.')
        return

    assert query.data
    data = query.data
    if not data.startswith('q:'):
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
                                session.is_processing = False
                                return

                        # Send tool call as formatted message
                        await send_tool_call(update, block)

                    elif isinstance(block, ToolResultBlock):
                        # Send tool result as expandable quote
                        await send_tool_result(update, block)

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
            options = ClaudeAgentOptions(
                allowed_tools=[
                    'Read',
                    'Write',
                    'Edit',
                    'Bash',
                    'Glob',
                    'Grep',
                    'Task',
                    'WebFetch',
                    'WebSearch',
                    'TodoWrite',
                    'AskUserQuestion',
                    'NotebookEdit',
                ],
                permission_mode='acceptEdits',
                cwd=session.cwd,
                resume=teleport.session_id if resumable else None,
            )
            session.client = ClaudeSDKClient(options=options)
            await session.client.connect()

            if resumable:
                await update.message.reply_text('✓ Session resumed. Continuing...')
            else:
                await update.message.reply_text('✓ Connected. Starting fresh session (no prior conversation).')
        except Exception as e:
            await update.message.reply_text(f'Failed to connect: {e}')
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
            options = ClaudeAgentOptions(
                allowed_tools=[
                    'Read',
                    'Write',
                    'Edit',
                    'Bash',
                    'Glob',
                    'Grep',
                    'Task',
                    'WebFetch',
                    'WebSearch',
                    'TodoWrite',
                    'AskUserQuestion',
                    'NotebookEdit',
                ],
                permission_mode='acceptEdits',
                cwd=session.cwd,
            )
            session.client = ClaudeSDKClient(options=options)
            await session.client.connect()

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
    logger.error(f'Error: {context.error}')


def create_telegram_app(config: Config) -> Application:
    """Create the Telegram application."""
    app = Application.builder().token(config.telegram.bot_token).build()
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
        await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        logger.info('Telegram bot started')

        # Run forever
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

        await tg_app.updater.stop()
        await tg_app.stop()

    await runner.cleanup()
