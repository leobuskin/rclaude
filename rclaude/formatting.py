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

from rclaude.config import MAX_MESSAGE_LENGTH, logger


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


def format_tool_call(block: ToolUseBlock) -> str | None:
    """Format a tool call for display. Returns None for AskUserQuestion (handled specially)."""
    tool_name = block.name
    tool_input: dict[str, Any] = block.input

    if tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        escaped_cmd = escape_html(cmd)
        if '\n' in cmd:
            return f'<pre><code class="language-bash">{escaped_cmd}</code></pre>'
        else:
            return f'<b>$</b> <code>{escaped_cmd}</code>'
    elif tool_name == 'Read':
        path = escape_html(tool_input.get('file_path', ''))
        return f'📖 <b>Reading</b> <code>{path}</code>'
    elif tool_name == 'Write':
        path = escape_html(tool_input.get('file_path', ''))
        return f'📝 <b>Writing</b> <code>{path}</code>'
    elif tool_name == 'Edit':
        path = escape_html(tool_input.get('file_path', ''))
        return f'✏️ <b>Editing</b> <code>{path}</code>'
    elif tool_name == 'Glob':
        pattern = escape_html(tool_input.get('pattern', ''))
        return f'🔍 <b>Finding</b> <code>{pattern}</code>'
    elif tool_name == 'Grep':
        pattern = escape_html(tool_input.get('pattern', ''))
        return f'🔎 <b>Searching</b> <code>{pattern}</code>'
    elif tool_name == 'Task':
        desc = escape_html(tool_input.get('description', ''))
        return f'🤖 <b>Subagent:</b> {desc}'
    elif tool_name == 'WebFetch':
        url = escape_html(tool_input.get('url', ''))
        return f'🌐 <b>Fetching</b> <code>{url}</code>'
    elif tool_name == 'WebSearch':
        query = escape_html(tool_input.get('query', ''))
        return f'🔍 <b>Web search:</b> {query}'
    elif tool_name == 'TodoWrite':
        return '📋 <b>Updating todos</b>'
    elif tool_name == 'AskUserQuestion':
        return None  # Handled specially
    else:
        return f'🔧 <b>{escape_html(tool_name)}</b>'


async def send_tool_call(update: Update, block: ToolUseBlock) -> tuple[int, str] | None:
    """Send a tool call as a formatted message. Returns (message_id, text) for later editing."""
    assert update.effective_chat

    text = format_tool_call(block)
    if text is None:
        return None

    try:
        msg = await update.effective_chat.send_message(text, parse_mode='HTML')
        return (msg.message_id, text)
    except Exception as e:
        logger.error(f'Failed to send tool call: {e}')
        return None


def format_tool_result(block: ToolResultBlock) -> str | None:
    """Format a tool result for display. Returns None if empty."""
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
        return None

    # Truncate very long results
    if len(result_text) > 2000:
        result_text = result_text[:2000] + '\n...(truncated)'

    escaped = escape_html(result_text)

    # Use expandable blockquote for long/multiline results (no icon - looks cleaner)
    # Only show icon for errors or short inline results
    if '\n' in result_text or len(result_text) > 200:
        if block.is_error:
            prefix = '❌ '
        else:
            prefix = ''
        if len(result_text) > 200:
            return f'{prefix}<blockquote expandable>{escaped}</blockquote>'
        else:
            return f'{prefix}<blockquote>{escaped}</blockquote>'
    else:
        # Short inline result - show icon
        icon = '❌' if block.is_error else '✅'
        return f'{icon} {escaped}'


async def send_tool_result(
    update: Update,
    block: ToolResultBlock,
    tool_msg_info: tuple[int, str] | None = None,
) -> None:
    """Send/update a tool result. If tool_msg_info provided, edit the original message."""
    assert update.effective_chat

    result_text = format_tool_result(block)
    if result_text is None:
        return

    message_id = tool_msg_info[0] if tool_msg_info else None

    # If we have the original tool call message, edit it to append the result
    if tool_msg_info:
        original_text = tool_msg_info[1]
        combined_text = f'{original_text}\n{result_text}'
        try:
            bot = update.get_bot()
            await bot.edit_message_text(
                text=combined_text,
                chat_id=update.effective_chat.id,
                message_id=message_id,
                parse_mode='HTML',
            )
            return
        except Exception as e:
            logger.error(f'Failed to edit tool message: {e}')
            # Fall through to reply instead

    # Fallback: reply to original message (or send standalone if no original)
    try:
        await update.effective_chat.send_message(
            result_text,
            parse_mode='HTML',
            reply_to_message_id=message_id,
        )
    except Exception as e:
        logger.error(f'Failed to send tool result: {e}')
        # Last resort: plain text without reply
        icon = '❌' if block.is_error else '✅'
        try:
            content = block.content
            plain = str(content)[:1000] if content else ''
            await update.effective_chat.send_message(f'{icon} {plain}')
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


# ─────────────────────────────────────────────────────────────────────────────
# Permission Prompt Formatting
# ─────────────────────────────────────────────────────────────────────────────


def format_permission_edit(input_data: dict[str, Any]) -> str:
    """Format Edit tool permission prompt showing changes."""
    file_path = input_data.get('file_path', '')
    old_string = input_data.get('old_string', '')
    new_string = input_data.get('new_string', '')

    # Truncate if too long
    max_len = 500
    old_display = old_string[:max_len] + '...' if len(old_string) > max_len else old_string
    new_display = new_string[:max_len] + '...' if len(new_string) > max_len else new_string

    return (
        f'<b>✏️ Edit:</b> <code>{escape_html(file_path)}</code>\n\n'
        f'<b>Remove:</b>\n<pre>{escape_html(old_display)}</pre>\n\n'
        f'<b>Add:</b>\n<pre>{escape_html(new_display)}</pre>'
    )


def format_permission_bash(input_data: dict[str, Any]) -> str:
    """Format Bash tool permission prompt."""
    command = input_data.get('command', '')
    description = input_data.get('description', '')

    escaped_cmd = escape_html(command)
    if '\n' in command:
        text = f'<pre>{escaped_cmd}</pre>'
    else:
        text = f'<b>$</b> <code>{escaped_cmd}</code>'
    if description:
        text += f'\n\n<i>{escape_html(description)}</i>'
    return text


def format_permission_write(input_data: dict[str, Any]) -> str:
    """Format Write tool permission prompt."""
    file_path = input_data.get('file_path', '')
    content = input_data.get('content', '')

    # Truncate preview
    max_len = 1000
    preview = content[:max_len] + '...' if len(content) > max_len else content

    return (
        f'<b>📝 Write:</b> <code>{escape_html(file_path)}</code>\n\n<blockquote expandable><pre>{escape_html(preview)}</pre></blockquote>'
    )


def format_permission_notebook(input_data: dict[str, Any]) -> str:
    """Format NotebookEdit tool permission prompt."""
    notebook_path = input_data.get('notebook_path', '')
    cell_type = input_data.get('cell_type', 'code')
    edit_mode = input_data.get('edit_mode', 'replace')
    new_source = input_data.get('new_source', '')

    max_len = 500
    source_preview = new_source[:max_len] + '...' if len(new_source) > max_len else new_source

    return (
        f'<b>📓 Notebook {edit_mode}:</b> <code>{escape_html(notebook_path)}</code>\n'
        f'Cell type: <code>{escape_html(cell_type)}</code>\n\n'
        f'<pre>{escape_html(source_preview)}</pre>'
    )


def format_permission_prompt(tool_name: str, input_data: dict[str, Any]) -> str:
    """Format a permission request for display in Telegram."""
    if tool_name == 'Edit':
        return format_permission_edit(input_data)
    elif tool_name == 'Bash':
        return format_permission_bash(input_data)
    elif tool_name == 'Write':
        return format_permission_write(input_data)
    elif tool_name == 'NotebookEdit':
        return format_permission_notebook(input_data)
    else:
        # Generic format for unknown tools
        import json

        return f'<b>🔧 {escape_html(tool_name)}</b>\n\n<pre>{escape_html(json.dumps(input_data, indent=2)[:1000])}</pre>'


def create_permission_keyboard(tool_name: str | None = None) -> InlineKeyboardMarkup:
    """Create inline keyboard for permission approval.

    For edit tools (Edit, Write, NotebookEdit), shows 'Accept Edits' instead of 'Always'.
    """
    edit_tools = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}

    if tool_name and tool_name in edit_tools:
        # For edit tools, offer to enable acceptEdits mode
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton('✓ Allow', callback_data='perm:allow'),
                    InlineKeyboardButton('📝 Accept Edits', callback_data='perm:accept_edits'),
                ],
                [
                    InlineKeyboardButton('✗ Reject', callback_data='perm:reject'),
                ],
            ]
        )
    else:
        # For other tools (Bash, etc.), offer to add rule to settings
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton('✓ Allow', callback_data='perm:allow'),
                    InlineKeyboardButton('✓ Always', callback_data='perm:always'),
                ],
                [
                    InlineKeyboardButton('✗ Reject', callback_data='perm:reject'),
                ],
            ]
        )


def create_mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    """Create inline keyboard for mode selection."""
    modes = [
        ('default', '🔒 Default'),
        ('acceptEdits', '📝 Accept Edits'),
        ('plan', '📋 Plan Mode'),
        ('bypassPermissions', '⚠️ Dangerous'),
    ]

    buttons: list[list[InlineKeyboardButton]] = []
    for mode_id, label in modes:
        if mode_id == current_mode:
            label = f'• {label}'  # Mark current
        buttons.append([InlineKeyboardButton(label, callback_data=f'mode:{mode_id}')])

    return InlineKeyboardMarkup(buttons)


def create_model_keyboard(current_model: str | None = None) -> InlineKeyboardMarkup:
    """Create inline keyboard for model selection."""
    models = [
        ('sonnet', '⚡ Sonnet', 'Fast, balanced'),
        ('opus', '🧠 Opus', 'Most capable'),
        ('haiku', '🚀 Haiku', 'Fastest, lightweight'),
    ]

    buttons: list[list[InlineKeyboardButton]] = []
    for model_id, label, desc in models:
        display = f'• {label}' if current_model and model_id in current_model.lower() else label
        buttons.append([InlineKeyboardButton(f'{display} - {desc}', callback_data=f'model:{model_id}')])

    return InlineKeyboardMarkup(buttons)
