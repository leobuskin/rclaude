"""Message formatting for Telegram output.

Uses Telegram's HTML parse mode for rich formatting:
- <b>bold</b>, <i>italic</i>, <code>inline code</code>
- <pre><code class="language-python">code block</code></pre>
- <blockquote expandable>collapsible content</blockquote>
"""

import html
import re
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from claude_agent_sdk import ToolUseBlock, ToolResultBlock

from glaude.config import MAX_MESSAGE_LENGTH, logger


def escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(text)


def markdown_to_telegram_html(text: str) -> str:
    """Convert Claude's markdown to Telegram HTML.

    Handles:
    - **bold** -> <b>bold</b>
    - *italic* -> <i>italic</i>
    - `code` -> <code>code</code>
    - ```lang\ncode\n``` -> <pre><code class="language-lang">code</code></pre>
    - [text](url) -> <a href="url">text</a>
    """
    if not text:
        return ''

    # First, extract and protect code blocks (they shouldn't be processed)
    code_blocks: list[str] = []

    def save_code_block(match: re.Match) -> str:
        lang = match.group(1) or ''
        code = match.group(2)
        escaped_code = escape_html(code.strip())
        if lang:
            block = f'<pre><code class="language-{lang}">{escaped_code}</code></pre>'
        else:
            block = f'<pre><code>{escaped_code}</code></pre>'
        code_blocks.append(block)
        return f'\x00CODE{len(code_blocks) - 1}\x00'

    text = re.sub(r'```(\w*)\n(.*?)```', save_code_block, text, flags=re.DOTALL)

    # Extract inline code and protect it
    inline_codes: list[str] = []

    def save_inline_code(match: re.Match) -> str:
        code = escape_html(match.group(1))
        inline_codes.append(f'<code>{code}</code>')
        return f'\x00INLINE{len(inline_codes) - 1}\x00'

    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # Now escape HTML in the rest of the text
    text = escape_html(text)

    # Convert markdown formatting
    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # Italic: *text* or _text_ (but not inside words)
    text = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'<i>\1</i>', text)

    # Links: [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f'\x00CODE{i}\x00', block)

    # Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f'\x00INLINE{i}\x00', code)

    return text


async def send_text(update: Update, text: str) -> None:
    """Send a text message with HTML formatting (markdown converted)."""
    assert update.effective_chat

    if not text.strip():
        return

    html_text = markdown_to_telegram_html(text)

    # Split into chunks if needed
    chunks = _split_text(html_text, MAX_MESSAGE_LENGTH)

    for chunk in chunks:
        if chunk.strip():
            try:
                await update.effective_chat.send_message(chunk, parse_mode='HTML')
            except Exception as e:
                logger.error(f'Failed to send HTML message: {e}')
                # Fallback to plain text
                try:
                    plain = re.sub(r'<[^>]+>', '', chunk)
                    await update.effective_chat.send_message(plain[:MAX_MESSAGE_LENGTH])
                except Exception as e2:
                    logger.error(f'Failed to send plain message: {e2}')


async def send_tool_call(update: Update, block: ToolUseBlock) -> None:
    """Send a tool call as a formatted message."""
    assert update.effective_chat

    tool_name = block.name
    tool_input: dict[str, Any] = block.input

    if tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        escaped_cmd = escape_html(cmd)
        text = f'<b>$</b> <pre><code class="language-bash">{escaped_cmd}</code></pre>'
    elif tool_name == 'Read':
        path = escape_html(tool_input.get('file_path', ''))
        text = f'📖 <b>Reading</b> <code>{path}</code>'
    elif tool_name == 'Write':
        path = escape_html(tool_input.get('file_path', ''))
        text = f'📝 <b>Writing</b> <code>{path}</code>'
    elif tool_name == 'Edit':
        path = escape_html(tool_input.get('file_path', ''))
        text = f'✏️ <b>Editing</b> <code>{path}</code>'
    elif tool_name == 'Glob':
        pattern = escape_html(tool_input.get('pattern', ''))
        text = f'🔍 <b>Finding</b> <code>{pattern}</code>'
    elif tool_name == 'Grep':
        pattern = escape_html(tool_input.get('pattern', ''))
        text = f'🔎 <b>Searching</b> <code>{pattern}</code>'
    elif tool_name == 'Task':
        desc = escape_html(tool_input.get('description', ''))
        text = f'🤖 <b>Subagent:</b> {desc}'
    elif tool_name == 'WebFetch':
        url = escape_html(tool_input.get('url', ''))
        text = f'🌐 <b>Fetching</b> <code>{url}</code>'
    elif tool_name == 'WebSearch':
        query = escape_html(tool_input.get('query', ''))
        text = f'🔍 <b>Web search:</b> {query}'
    elif tool_name == 'TodoWrite':
        text = '📋 <b>Updating todos</b>'
    elif tool_name == 'AskUserQuestion':
        return  # Handled specially
    else:
        text = f'🔧 <b>{escape_html(tool_name)}</b>'

    try:
        await update.effective_chat.send_message(text, parse_mode='HTML')
    except Exception as e:
        logger.error(f'Failed to send tool call: {e}')


async def send_tool_result(update: Update, block: ToolResultBlock) -> None:
    """Send a tool result as an expandable quote."""
    assert update.effective_chat

    content = block.content
    if isinstance(content, str):
        result_text = content
    elif isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                texts.append(item.get('text', ''))
        result_text = '\n'.join(texts)
    else:
        result_text = str(content) if content else ''

    if not result_text.strip():
        return

    # Truncate very long results
    if len(result_text) > 2000:
        result_text = result_text[:2000] + '\n...(truncated)'

    escaped = escape_html(result_text)
    icon = '❌' if block.is_error else '✅'

    # Use expandable blockquote for results
    if len(result_text) > 200:
        text = f'{icon} <blockquote expandable>{escaped}</blockquote>'
    else:
        text = f'{icon} <blockquote>{escaped}</blockquote>'

    try:
        await update.effective_chat.send_message(text, parse_mode='HTML')
    except Exception as e:
        logger.error(f'Failed to send tool result: {e}')
        # Fallback without formatting
        try:
            await update.effective_chat.send_message(f'{icon} {result_text[:1000]}')
        except Exception:
            pass


def _split_text(text: str, max_length: int) -> list[str]:
    """Split text into chunks respecting max length."""
    chunks: list[str] = []
    current_chunk = ''

    for line in text.split('\n'):
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = f'{current_chunk}\n{line}' if current_chunk else line

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


async def create_question_keyboard(question: dict[str, Any]) -> InlineKeyboardMarkup:
    """Create an inline keyboard for an AskUserQuestion option."""
    buttons: list[list[InlineKeyboardButton]] = []
    options = question.get('options', [])

    for i, opt in enumerate(options):
        label = opt.get('label', f'Option {i + 1}')
        buttons.append([InlineKeyboardButton(label, callback_data=f'q:0:{i}')])

    buttons.append([InlineKeyboardButton('Other (type answer)', callback_data='q:0:other')])

    return InlineKeyboardMarkup(buttons)
