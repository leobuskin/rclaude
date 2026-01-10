"""Telegram command and message handlers.

Type narrowing notes:
- Telegram's Update type has many Optional fields (message, effective_user, etc.)
  because updates can come from various sources (channels, callbacks, inline queries).
- For our text message handlers, these fields are always present.
- We use `assert` for type narrowing - these are stripped with `python -O`.
- error_handler uses `object` instead of `Update` to match python-telegram-bot's
  HandlerCallback signature (error handlers can receive malformed updates).
"""

import os

from telegram import Update
from telegram.ext import ContextTypes
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

from glaude.auth import is_authorized
from glaude.config import logger
from glaude.session import get_session, UserSession, PendingQuestion
from glaude.formatting import (
    split_and_send,
    format_tool_use,
    format_tool_result,
    create_question_keyboard,
)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    assert update.effective_user
    assert update.message

    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text(f'Not authorized. Your user ID: {user_id}')
        return

    await update.message.reply_text(
        'Claude Code Remote Control\n\n'
        'Send any message to interact with Claude Code on your dev machine.\n\n'
        'Commands:\n'
        '/start - Show this help\n'
        '/new - Start a new session\n'
        '/stop - Interrupt current task\n'
        '/status - Show session status\n'
        '/cwd <path> - Change working directory'
    )


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command - start a fresh session."""
    assert update.effective_user
    assert update.message

    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    session = get_session(user_id)

    if session.client:
        await session.client.disconnect()
        session.client = None

    session.pending_question = None
    session.is_processing = False

    await update.message.reply_text('Session cleared. Ready for new conversation.')


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop command - interrupt current task."""
    assert update.effective_user
    assert update.message

    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    session = get_session(user_id)

    if session.client and session.is_processing:
        try:
            await session.client.interrupt()
            await update.message.reply_text('Task interrupted.')
        except Exception as e:
            await update.message.reply_text(f'Failed to interrupt: {e}')
    else:
        await update.message.reply_text('No active task to interrupt.')


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    assert update.effective_user
    assert update.message

    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    session = get_session(user_id)

    status_lines = [
        f'Working directory: {session.cwd}',
        f'Session active: {"Yes" if session.client else "No"}',
        f'Processing: {"Yes" if session.is_processing else "No"}',
        f'Pending question: {"Yes" if session.pending_question else "No"}',
    ]

    await update.message.reply_text('\n'.join(status_lines))


async def handle_cwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cwd command - change working directory."""
    assert update.effective_user
    assert update.message

    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    session = get_session(user_id)

    if context.args:
        new_cwd = ' '.join(context.args)
        if os.path.isdir(new_cwd):
            session.cwd = os.path.abspath(new_cwd)
            await update.message.reply_text(f'Working directory: {session.cwd}')
        else:
            await update.message.reply_text(f'Directory not found: {new_cwd}')
    else:
        await update.message.reply_text(f'Current: {session.cwd}\nUsage: /cwd <path>')


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    assert update.effective_user
    assert update.callback_query
    assert update.effective_chat

    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    query = update.callback_query
    await query.answer()

    session = get_session(user_id)

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
            await _continue_after_question(update, session, pending.answers)


async def _continue_after_question(
    update: Update,
    session: UserSession,
    answers: dict[str, str],
) -> None:
    """Continue Claude session after AskUserQuestion is answered."""
    answer_text = '\n'.join(f'{q}: {a}' for q, a in answers.items())

    if session.client:
        await session.client.query(answer_text)
        await _process_claude_response(update, session)


async def _process_claude_response(update: Update, session: UserSession) -> None:
    """Process and display Claude's response."""
    assert update.effective_chat

    if not session.client:
        return

    session.is_processing = True
    response_text = ''
    tool_outputs: list[str] = []

    try:
        async for message in session.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        tool_desc = format_tool_use(block)
                        tool_outputs.append(f'[{tool_desc}]')

                        if block.name == 'AskUserQuestion':
                            questions = block.input.get('questions', [])
                            if questions:
                                session.pending_question = PendingQuestion(
                                    tool_use_id=block.id,
                                    questions=questions,
                                )
                                first_q = questions[0]
                                keyboard = await create_question_keyboard(first_q)

                                if response_text.strip():
                                    await split_and_send(update, response_text)
                                    response_text = ''

                                await update.effective_chat.send_message(
                                    f'{first_q.get("header", "Question")}: {first_q["question"]}',
                                    reply_markup=keyboard,
                                )
                                session.is_processing = False
                                return

                    elif isinstance(block, ToolResultBlock):
                        result = format_tool_result(block)
                        if result.strip():
                            tool_outputs.append(result)

            elif isinstance(message, ResultMessage):
                if message.is_error and message.result:
                    response_text += f'\n\nError: {message.result}'

                if message.total_cost_usd:
                    response_text += f'\n\n[Cost: ${message.total_cost_usd:.4f}]'

    except Exception as e:
        logger.error(f'Error processing Claude response: {e}')
        response_text += f'\n\nError: {e}'

    finally:
        session.is_processing = False

    if tool_outputs:
        tools_text = '\n'.join(tool_outputs[-10:])
        if len(tool_outputs) > 10:
            tools_text = f'...({len(tool_outputs) - 10} more operations)\n' + tools_text
        await split_and_send(update, tools_text)

    if response_text.strip():
        await split_and_send(update, response_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages."""
    assert update.effective_user
    assert update.message
    assert update.message.text
    assert update.message.chat
    assert context.user_data is not None

    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text(f'Not authorized. Your user ID: {user_id}')
        return

    session = get_session(user_id)
    text = update.message.text

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
            await _continue_after_question(update, session, pending.answers)
        return

    if session.is_processing:
        await update.message.reply_text('Still processing previous request. Use /stop to interrupt.')
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
        await _process_claude_response(update, session)

    except Exception as e:
        logger.error(f'Error handling message: {e}', exc_info=True)
        await update.message.reply_text(f'Error: {e}')

        if session.client:
            try:
                await session.client.disconnect()
            except Exception:
                pass
            session.client = None


# TODO: First param is `object` instead of `Update` to match python-telegram-bot's
# HandlerCallback signature - error handlers can receive malformed/non-Update objects.
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors."""
    logger.error(f'Update {update} caused error {context.error}')
