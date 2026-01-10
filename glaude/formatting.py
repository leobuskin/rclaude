"""Message formatting for Telegram output."""

import json
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from claude_agent_sdk import ToolUseBlock, ToolResultBlock

from glaude.config import MAX_MESSAGE_LENGTH, logger


async def split_and_send(update: Update, text: str) -> None:
    """Split long messages and send them.

    Note: Callers must ensure update.message is not None before calling.
    """
    assert update.message

    if not text.strip():
        return

    chunks: list[str] = []
    current_chunk = ''

    for line in text.split('\n'):
        if len(current_chunk) + len(line) + 1 > MAX_MESSAGE_LENGTH:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = f'{current_chunk}\n{line}' if current_chunk else line

    if current_chunk:
        chunks.append(current_chunk)

    for chunk in chunks:
        if chunk.strip():
            try:
                await update.message.reply_text(chunk)
            except Exception as e:
                logger.error(f'Failed to send message: {e}')
                await update.message.reply_text(chunk[:MAX_MESSAGE_LENGTH])


def format_tool_use(block: ToolUseBlock) -> str:
    """Format a tool use block for display."""
    tool_name = block.name
    tool_input: dict[str, Any] = block.input

    if tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        return f'$ {cmd}'
    elif tool_name == 'Read':
        path = tool_input.get('file_path', '')
        return f'Reading: {path}'
    elif tool_name == 'Write':
        path = tool_input.get('file_path', '')
        return f'Writing: {path}'
    elif tool_name == 'Edit':
        path = tool_input.get('file_path', '')
        return f'Editing: {path}'
    elif tool_name == 'Glob':
        pattern = tool_input.get('pattern', '')
        return f'Finding files: {pattern}'
    elif tool_name == 'Grep':
        pattern = tool_input.get('pattern', '')
        return f'Searching: {pattern}'
    elif tool_name == 'Task':
        desc = tool_input.get('description', '')
        return f'Spawning subagent: {desc}'
    else:
        return f'{tool_name}: {json.dumps(tool_input, indent=2)[:200]}'


def format_tool_result(block: ToolResultBlock) -> str:
    """Format a tool result block for display."""
    content = block.content
    if isinstance(content, str):
        if len(content) > 500:
            return content[:500] + '...(truncated)'
        return content
    elif isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                texts.append(item.get('text', ''))
        result = '\n'.join(texts)
        if len(result) > 500:
            return result[:500] + '...(truncated)'
        return result
    return str(content)[:500] if content else '(no output)'


async def create_question_keyboard(question: dict[str, Any]) -> InlineKeyboardMarkup:
    """Create an inline keyboard for an AskUserQuestion option."""
    buttons: list[list[InlineKeyboardButton]] = []
    options = question.get('options', [])

    for i, opt in enumerate(options):
        label = opt.get('label', f'Option {i + 1}')
        buttons.append([InlineKeyboardButton(label, callback_data=f'q:0:{i}')])

    buttons.append([InlineKeyboardButton('Other (type custom answer)', callback_data='q:0:other')])

    return InlineKeyboardMarkup(buttons)
