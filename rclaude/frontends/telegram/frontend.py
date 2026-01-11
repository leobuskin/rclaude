"""Telegram frontend implementation."""

import logging
import re
from typing import Any, cast

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from rclaude.core import (
    PermissionMode,
    Session,
    add_permission_rule,
    can_resume_session,
    create_client,
    create_permission_handler,
    fetch_context,
    generate_permission_rule,
    generate_smart_bash_rule,
    process_response,
    validate_permission_mode,
)
from rclaude.core.events import (
    ErrorEvent,
    QuestionEvent,
    ReturnToTerminalEvent,
    TextEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from rclaude.core.session import PendingPermission
from rclaude.frontends.base import Frontend
from rclaude.settings import Config

from .formatting import (
    format_permission_prompt,
    format_pinned_status,
    format_tool_call,
    format_tool_result,
    markdown_to_telegram_html,
    split_text,
)
from .keyboards import (
    create_mode_keyboard,
    create_model_keyboard,
    create_permission_keyboard,
    create_question_keyboard,
)

logger = logging.getLogger('rclaude')


class TelegramFrontend(Frontend):
    """Telegram implementation of Frontend."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.bot_token = config.telegram.bot_token
        self.allowed_user_id = config.telegram.user_id
        self.app: Application | None = None

        # Track message refs: session_id -> {tool_id -> (msg_id, text)}
        self._tool_messages: dict[str, dict[str, tuple[int, str]]] = {}
        # Pinned message tracking: session_id -> msg_id
        self._pinned_messages: dict[str, int] = {}
        # Pending teleports: user_id -> TeleportRequest
        self._pending_teleports: dict[int, dict[str, Any]] = {}
        # Session manager reference (set by server)
        self._session_manager: Any = None

    def set_session_manager(self, manager: Any) -> None:
        """Set the session manager reference."""
        self._session_manager = manager

    @property
    def bot(self) -> Bot:
        """Get the bot instance."""
        assert self.app is not None
        return self.app.bot

    async def start(self) -> None:
        """Start the Telegram bot."""
        self.app = Application.builder().token(self.bot_token).concurrent_updates(True).build()

        # Store config and frontend ref in bot_data
        self.app.bot_data['config'] = self.config
        self.app.bot_data['frontend'] = self

        # Register handlers
        self._register_handlers()

        # Initialize and start
        await self.app.initialize()
        await self.app.start()
        if self.app.updater:
            await self.app.updater.start_polling(drop_pending_updates=True)

        logger.info('Telegram bot started')

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self.app:
            if self.app.updater:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info('Telegram bot stopped')

    def _register_handlers(self) -> None:
        """Register all Telegram handlers."""
        assert self.app is not None

        # Command handlers
        self.app.add_handler(CommandHandler('start', self._handle_start))
        self.app.add_handler(CommandHandler('new', self._handle_new))
        self.app.add_handler(CommandHandler('cc', self._handle_cc))
        self.app.add_handler(CommandHandler('status', self._handle_status))
        self.app.add_handler(CommandHandler('mode', self._handle_mode))
        self.app.add_handler(CommandHandler('model', self._handle_model))
        self.app.add_handler(CommandHandler('cost', self._handle_cost))
        self.app.add_handler(CommandHandler('context', self._handle_context))
        self.app.add_handler(CommandHandler('compact', self._handle_compact))
        self.app.add_handler(CommandHandler('todos', self._handle_todos))
        self.app.add_handler(CommandHandler('stop', self._handle_stop))
        self.app.add_handler(CommandHandler('cancel', self._handle_cancel))
        self.app.add_handler(CommandHandler('link', self._handle_link))

        # Callback query handler
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Message handler
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

    def _get_session(self, user_id: int) -> Session:
        """Get or create session for a user."""
        frontend_user_id = f'telegram:{user_id}'
        return self._session_manager.get_or_create(frontend_user_id)

    async def _check_auth(self, update: Update) -> bool:
        """Check if user is authorized."""
        if not update.effective_user:
            return False
        if update.effective_user.id != self.allowed_user_id:
            if update.message:
                await update.message.reply_text(f'Not authorized. Your user ID: {update.effective_user.id}')
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Frontend Protocol Implementation
    # ─────────────────────────────────────────────────────────────────────────

    async def send_text(
        self,
        session: Session,
        text: str,
        is_final: bool = False,
    ) -> None:
        """Send text message to user."""
        if not text.strip():
            return

        html_text = markdown_to_telegram_html(text)
        chunks = split_text(html_text)

        for i, chunk in enumerate(chunks):
            if chunk.strip():
                is_last_chunk = i == len(chunks) - 1
                disable_notification = not is_final if is_last_chunk else True

                try:
                    await self.bot.send_message(
                        chat_id=self.allowed_user_id,
                        text=chunk,
                        parse_mode='HTML',
                        disable_notification=disable_notification,
                    )
                except Exception as e:
                    logger.error(f'Failed to send HTML message: {e}')
                    # Fallback to plain text
                    try:
                        plain = re.sub(r'<[^>]+>', '', chunk)
                        await self.bot.send_message(
                            chat_id=self.allowed_user_id,
                            text=plain[:4096],
                            disable_notification=disable_notification,
                        )
                    except Exception as e2:
                        logger.error(f'Failed to send plain message: {e2}')

    async def send_tool_call(
        self,
        session: Session,
        event: ToolCallEvent,
    ) -> Any:
        """Send tool call notification."""
        text = format_tool_call(event.tool_name, event.input_data)
        if text is None:
            return None

        try:
            msg = await self.bot.send_message(
                chat_id=self.allowed_user_id,
                text=text,
                parse_mode='HTML',
                disable_notification=True,
            )
            msg_info = (msg.message_id, text)

            # Store for later result editing
            if session.id not in self._tool_messages:
                self._tool_messages[session.id] = {}
            self._tool_messages[session.id][event.tool_id] = msg_info

            return msg_info
        except Exception as e:
            logger.error(f'Failed to send tool call: {e}')
            return None

    async def send_tool_result(
        self,
        session: Session,
        event: ToolResultEvent,
        tool_msg_ref: Any,
    ) -> None:
        """Send/update tool result."""
        result_text = format_tool_result(event.content, event.is_error)
        if result_text is None:
            return

        # Try to get stored message ref if not provided
        if tool_msg_ref is None and session.id in self._tool_messages:
            tool_msg_ref = self._tool_messages[session.id].get(event.tool_id)

        if tool_msg_ref:
            message_id, original_text = tool_msg_ref
            combined_text = f'{original_text}\n{result_text}'
            try:
                await self.bot.edit_message_text(
                    text=combined_text,
                    chat_id=self.allowed_user_id,
                    message_id=message_id,
                    parse_mode='HTML',
                )
                return
            except Exception as e:
                logger.error(f'Failed to edit tool message: {e}')

        # Fallback: send standalone message
        try:
            await self.bot.send_message(
                chat_id=self.allowed_user_id,
                text=result_text,
                parse_mode='HTML',
                disable_notification=True,
            )
        except Exception as e:
            logger.error(f'Failed to send tool result: {e}')

    async def request_permission(
        self,
        session: Session,
        pending: PendingPermission,
    ) -> None:
        """Show permission request UI."""
        text = format_permission_prompt(pending.tool_name, pending.input_data)
        keyboard = create_permission_keyboard(pending.tool_name)

        await self.bot.send_message(
            chat_id=self.allowed_user_id,
            text=text,
            reply_markup=keyboard,
            parse_mode='HTML',
            disable_notification=False,
        )

    async def request_question_answer(
        self,
        session: Session,
        event: QuestionEvent,
    ) -> None:
        """Show question UI with options."""
        if not event.questions:
            return

        first_q = event.questions[0]
        keyboard = create_question_keyboard(first_q)

        await self.bot.send_message(
            chat_id=self.allowed_user_id,
            text=f'<b>{first_q.get("header", "Question")}:</b> {first_q["question"]}',
            reply_markup=keyboard,
            parse_mode='HTML',
            disable_notification=False,
        )

    async def update_status(self, session: Session) -> None:
        """Update pinned status message."""
        text = format_pinned_status(
            session.permission_mode,
            session.current_model,
            session.context.percent_used,
            session.usage.total_cost_usd,
        )

        try:
            pinned_id = self._pinned_messages.get(session.id)
            if pinned_id:
                await self.bot.edit_message_text(
                    text=text,
                    chat_id=self.allowed_user_id,
                    message_id=pinned_id,
                    parse_mode='HTML',
                )
            else:
                msg = await self.bot.send_message(
                    chat_id=self.allowed_user_id,
                    text=text,
                    parse_mode='HTML',
                )
                await msg.pin(disable_notification=True)
                self._pinned_messages[session.id] = msg.message_id
        except Exception as e:
            logger.warning(f'Failed to update pinned message: {e}')

    async def notify_teleport(
        self,
        session: Session,
        session_id: str,
        cwd: str,
        permission_mode: str,
    ) -> None:
        """Notify user of incoming session teleport."""
        from rclaude.core import format_mode_display

        mode_display = format_mode_display(permission_mode)
        await self.bot.send_message(
            chat_id=self.allowed_user_id,
            text=f'📱 <b>Session teleported!</b>\n\nMode: {mode_display}\nSend a message to continue.',
            parse_mode='HTML',
            disable_notification=False,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Command Handlers
    # ─────────────────────────────────────────────────────────────────────────

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not await self._check_auth(update):
            return
        assert update.message
        await update.message.reply_text(
            '👋 <b>rclaude</b> - Remote Claude Code\n\n'
            'Use /tg in Claude Code to teleport your session here.\n\n'
            'Commands:\n'
            '/new - Start fresh session\n'
            '/cc - Get terminal resume command\n'
            '/mode - Change permission mode\n'
            '/model - Change model\n'
            '/cost - Show session cost\n'
            '/context - Show context usage\n'
            '/compact - Compact conversation\n'
            '/todos - Show todo list\n'
            '/stop - Stop current task\n'
            '/cancel - Cancel and disconnect',
            parse_mode='HTML',
        )

    async def _handle_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /new command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)
        if session.client:
            await session.disconnect()

        # Clear session state
        session.claude_session_id = None
        session.is_processing = False
        session.pending_question = None
        session.pending_permission = None

        # Clear pending teleport
        self._pending_teleports.pop(update.effective_user.id, None)

        await update.message.reply_text('✓ Session cleared. Send a message to start fresh.')

    async def _handle_cc(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cc command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)

        if session.claude_session_id:
            cmd = f'claude --resume {session.claude_session_id}'
            await update.message.reply_text(
                f'Resume in terminal:\n<pre>{cmd}</pre>',
                parse_mode='HTML',
            )

            # Notify that session is returning to terminal
            await session.emit(ReturnToTerminalEvent(session_id=session.id, claude_session_id=session.claude_session_id))
        else:
            await update.message.reply_text('No active session to resume.')

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        user_id = update.effective_user.id
        session = self._get_session(user_id)

        status_parts = [
            f'<b>Mode:</b> {session.permission_mode}',
            f'<b>Model:</b> {session.current_model or "default"}',
            f'<b>Processing:</b> {"Yes" if session.is_processing else "No"}',
            f'<b>Session ID:</b> {session.claude_session_id[:8] if session.claude_session_id else "None"}...',
        ]

        if session.context.tokens_max > 0:
            status_parts.append(f'<b>Context:</b> {session.context.percent_used}%')

        if session.usage.total_cost_usd > 0:
            status_parts.append(f'<b>Cost:</b> ${session.usage.total_cost_usd:.4f}')

        # Show pending teleport if any
        if user_id in self._pending_teleports:
            tp = self._pending_teleports[user_id]
            status_parts.append(f'<b>Pending teleport:</b> <code>{tp["session_id"][:8]}...</code>')

        await update.message.reply_text('\n'.join(status_parts), parse_mode='HTML')

    async def _handle_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /mode command - show and switch permission modes."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)

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
                if session.client:
                    session.client.set_permission_mode(new_mode)
                from rclaude.core import format_mode_display

                await update.message.reply_text(
                    f'✓ Mode changed to: {format_mode_display(new_mode)}',
                    parse_mode='HTML',
                )
                await self.update_status(session)
                return
            else:
                await update.message.reply_text(f'Unknown mode: {mode_arg}\n\nValid modes: default, accept, plan, dangerous')
                return

        # No argument - show current mode with keyboard
        from rclaude.core import format_mode_display

        keyboard = create_mode_keyboard(session.permission_mode)
        await update.message.reply_text(
            f'<b>Permission Mode</b>\n\nCurrent: {format_mode_display(session.permission_mode)}\n\n<i>Select a new mode below:</i>',
            parse_mode='HTML',
            reply_markup=keyboard,
        )

    async def _handle_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /model command - show and switch AI models."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)

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
                    await self.update_status(session)
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

    async def _handle_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cost command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)
        usage = session.usage

        text = (
            f'<b>Session Cost</b>\n\n'
            f'Total: <b>${usage.total_cost_usd:.4f}</b>\n'
            f'Input tokens: {usage.total_input_tokens:,}\n'
            f'Output tokens: {usage.total_output_tokens:,}\n'
            f'Turns: {usage.num_turns}'
        )

        if usage.last_response_cost is not None:
            text += f'\n\nLast response: ${usage.last_response_cost:.4f}'

        await update.message.reply_text(text, parse_mode='HTML')

    async def _handle_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /context command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)

        await self._query_and_process(session, '/context')

        ctx = session.context
        if ctx.tokens_max > 0:
            await update.message.reply_text(
                f'<b>Context Usage</b>\n\nUsed: {ctx.tokens_used:,} / {ctx.tokens_max:,}\nPercentage: <b>{ctx.percent_used}%</b>',
                parse_mode='HTML',
            )
        else:
            await update.message.reply_text('No context usage data available.')

    async def _handle_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /compact command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)

        if session.client:
            await update.message.reply_text('Compacting conversation...')
            await self._query_and_process(session, '/compact')
            await update.message.reply_text('✓ Conversation compacted')
        else:
            await update.message.reply_text('No active session.')

    async def _handle_todos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /todos command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)

        if session.client:
            await self._query_and_process(session, '/todos')
        else:
            await update.message.reply_text('No active session.')

    async def _handle_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop command."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        session = self._get_session(update.effective_user.id)

        if session.client and session.is_processing:
            await session.client.interrupt()
            await update.message.reply_text('✓ Interrupted')
        else:
            await update.message.reply_text('Nothing to stop.')

    async def _handle_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cancel command - cancel pending teleport or disconnect session."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message

        user_id = update.effective_user.id

        # Check for pending teleport first
        if user_id in self._pending_teleports:
            del self._pending_teleports[user_id]
            await update.message.reply_text('✓ Teleport cancelled.')
            return

        # Otherwise disconnect current session
        session = self._get_session(user_id)
        if session.client:
            await session.disconnect()
        session.claude_session_id = None
        session.is_processing = False

        await update.message.reply_text('✓ Cancelled and disconnected')

    async def _handle_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /link command for setup wizard."""
        assert update.message and update.message.text and update.effective_user

        args = update.message.text.split()
        if len(args) != 2:
            await update.message.reply_text('Usage: /link <token>')
            return

        token = args[1]

        # Check pending setup links (set by server)
        pending_links = context.bot_data.get('pending_setup_links', {})
        if token in pending_links:
            pending = pending_links[token]
            pending['result'] = (update.effective_user.id, update.effective_user.username or '')
            pending['event'].set()
            await update.message.reply_text(f'✓ Linked! User ID: {update.effective_user.id}')
        else:
            await update.message.reply_text('Invalid or expired token.')

    # ─────────────────────────────────────────────────────────────────────────
    # Callback Query Handler
    # ─────────────────────────────────────────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard callbacks."""
        if not await self._check_auth(update):
            return
        assert update.callback_query and update.effective_user

        query = update.callback_query
        await query.answer()

        data = query.data or ''
        session = self._get_session(update.effective_user.id)

        if data.startswith('perm:'):
            await self._handle_permission_callback(update, context, session, data)
        elif data.startswith('q:'):
            await self._handle_question_callback(update, context, session, data)
        elif data.startswith('mode:'):
            await self._handle_mode_callback(update, context, session, data)
        elif data.startswith('model:'):
            await self._handle_model_callback(update, context, session, data)

    async def _handle_permission_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session: Session,
        data: str,
    ) -> None:
        """Handle permission approval/rejection."""
        assert update.callback_query
        query = update.callback_query

        pending = session.pending_permission
        if not pending:
            await query.edit_message_text('Permission request expired.')
            return

        action = data.split(':', 1)[1]

        if action == 'allow':
            pending.result = PermissionResultAllow(updated_input=pending.input_data)
            await query.edit_message_text('✓ Allowed')
            pending.event.set()

        elif action == 'always':
            # Generate and save rule
            if pending.tool_name == 'Bash':
                rule = await generate_smart_bash_rule(pending.input_data.get('command', ''))
            else:
                rule = generate_permission_rule(pending.tool_name, pending.input_data)
            add_permission_rule(session.cwd, rule)
            pending.result = PermissionResultAllow(updated_input=pending.input_data)
            await query.edit_message_text(f'✓ Allowed always\nRule: <code>{rule}</code>', parse_mode='HTML')
            pending.event.set()

        elif action == 'accept_edits':
            # Enable acceptEdits mode
            session.permission_mode = 'acceptEdits'
            if session.client:
                session.client.set_permission_mode('acceptEdits')
            pending.result = PermissionResultAllow(updated_input=pending.input_data)
            await query.edit_message_text('✓ Allowed + Accept Edits mode enabled')
            await self.update_status(session)
            pending.event.set()

        elif action == 'reject':
            session.waiting_for_rejection_reason = True
            await query.edit_message_text('✗ Rejected. Send rejection reason:')

    async def _handle_question_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session: Session,
        data: str,
    ) -> None:
        """Handle question answer selection."""
        assert update.callback_query and context.user_data is not None
        query = update.callback_query

        pending = session.pending_question
        if not pending:
            await query.edit_message_text('Question expired.')
            return

        parts = data.split(':')
        if len(parts) < 3:
            return

        answer = parts[2]
        current_q = pending.questions[pending.current_question_idx]

        if answer == 'other':
            context.user_data['waiting_for_answer'] = True
            await query.edit_message_text('Type your answer:')
            return

        # Get the selected option label
        options = current_q.get('options', [])
        try:
            answer_idx = int(answer)
            selected_label = options[answer_idx]['label'] if answer_idx < len(options) else answer
        except (ValueError, IndexError):
            selected_label = answer

        pending.answers[current_q['question']] = selected_label
        pending.current_question_idx += 1

        await query.edit_message_text(f'✓ Selected: {selected_label}')

        # Check if more questions
        if pending.current_question_idx < len(pending.questions):
            next_q = pending.questions[pending.current_question_idx]
            keyboard = create_question_keyboard(next_q)
            await self.bot.send_message(
                chat_id=self.allowed_user_id,
                text=f'<b>{next_q.get("header", "Question")}:</b> {next_q["question"]}',
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        else:
            # All questions answered - submit formatted answers to Claude
            await self._submit_question_answers(session, pending.answers)

    async def _handle_mode_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session: Session,
        data: str,
    ) -> None:
        """Handle mode selection."""
        assert update.callback_query
        query = update.callback_query

        mode = data.split(':', 1)[1]
        session.permission_mode = cast(PermissionMode, validate_permission_mode(mode))

        if session.client:
            session.client.set_permission_mode(session.permission_mode)

        await query.edit_message_text(f'✓ Mode changed to: <b>{session.permission_mode}</b>', parse_mode='HTML')
        await self.update_status(session)

    async def _handle_model_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session: Session,
        data: str,
    ) -> None:
        """Handle model selection."""
        assert update.callback_query
        query = update.callback_query

        model = data.split(':', 1)[1]

        if session.client:
            try:
                await session.client.set_model(model)
                session.current_model = model
                await query.edit_message_text(f'✓ Model changed to: <b>{model}</b>', parse_mode='HTML')
                await self.update_status(session)
            except Exception as e:
                await query.edit_message_text(f'Failed to change model: {e}')
        else:
            session.current_model = model
            await query.edit_message_text(
                f'✓ Model set to: <b>{model}</b>\n<i>(Will apply on next session)</i>',
                parse_mode='HTML',
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Message Handler
    # ─────────────────────────────────────────────────────────────────────────

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text messages."""
        if not await self._check_auth(update):
            return
        assert update.effective_user and update.message and update.message.text and context.user_data is not None

        session = self._get_session(update.effective_user.id)
        text = update.message.text

        # Check for pending teleport
        if update.effective_user.id in self._pending_teleports:
            teleport = self._pending_teleports.pop(update.effective_user.id)
            await self._setup_session_from_teleport(session, teleport, update, context)

        # Handle waiting for rejection reason
        if session.waiting_for_rejection_reason and session.pending_permission:
            session.waiting_for_rejection_reason = False
            pending = session.pending_permission
            pending.result = PermissionResultDeny(message=text, interrupt=False)
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
                keyboard = create_question_keyboard(next_q)
                await self.bot.send_message(
                    chat_id=self.allowed_user_id,
                    text=f'<b>{next_q.get("header", "Question")}:</b> {next_q["question"]}',
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            else:
                await self._submit_question_answers(session, pending.answers)
            return

        # Normal message - send to Claude
        if not session.client:
            # Create new client
            permission_handler = create_permission_handler(
                session,
                lambda s, p: self.request_permission(s, p),
            )
            await create_client(session, permission_handler)
            await fetch_context(session)

        if session.client:
            session.is_processing = True
            await self._query_and_process(session, text)
            await self.update_status(session)

    async def _setup_session_from_teleport(
        self,
        session: Session,
        teleport: dict[str, Any],
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Set up session from a teleport request."""
        assert update.message

        session.cwd = teleport['cwd']
        session.terminal_id = teleport['terminal_id']
        session.permission_mode = cast(PermissionMode, validate_permission_mode(teleport['permission_mode']))

        resumable = can_resume_session(teleport['session_id'], teleport['cwd'])
        session.claude_session_id = teleport['session_id'] if resumable else None

        try:
            permission_handler = create_permission_handler(
                session,
                lambda s, p: self.request_permission(s, p),
            )
            await create_client(session, permission_handler)
            await fetch_context(session)

            if resumable:
                await update.message.reply_text('✓ Session resumed')
            else:
                await update.message.reply_text('✓ Connected (fresh session)')

            await self.update_status(session)
        except Exception as e:
            logger.error(f'Teleport failed: {e}')
            await update.message.reply_text(f'Failed to connect: {e}')

    async def _handle_event_internal(self, session: Session, event: Any) -> None:
        """Handle an event from Claude internally."""
        if isinstance(event, TextEvent):
            await self.send_text(session, event.content, event.is_final)
        elif isinstance(event, ToolCallEvent):
            await self.send_tool_call(session, event)
        elif isinstance(event, ToolResultEvent):
            await self.send_tool_result(session, event, None)
        elif isinstance(event, QuestionEvent):
            await self.request_question_answer(session, event)
        elif isinstance(event, ErrorEvent):
            await self.send_text(session, f'❌ Error: {event.message}', is_final=True)

    async def _query_and_process(self, session: Session, prompt: str) -> None:
        """Send a query to Claude and process the response."""
        if not session.client:
            return
        await session.client.query(prompt)
        async for event in process_response(session):
            await self._handle_event_internal(session, event)

    async def _submit_question_answers(self, session: Session, answers: dict[str, str]) -> None:
        """Submit question answers to Claude and process response."""
        session.pending_question = None
        answer_text = '\n'.join(f'{q}: {a}' for q, a in answers.items())
        await self._query_and_process(session, answer_text)

    def store_teleport(self, user_id: int, teleport: dict[str, Any]) -> None:
        """Store a pending teleport request."""
        self._pending_teleports[user_id] = teleport
