"""Message formatting for Telegram output.

Uses Telegram's HTML parse mode for rich formatting:
- <b>bold</b>, <i>italic</i>, <code>inline code</code>
- <pre><code class="language-python">code block</code></pre>
- <blockquote expandable>collapsible content</blockquote>
"""

import html
import json
import logging
import re
from typing import Any

logger = logging.getLogger('rclaude')

# Maximum message length for Telegram
MAX_MESSAGE_LENGTH = 4096


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


def split_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
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


def format_tool_call(tool_name: str, input_data: dict[str, Any]) -> str | None:
    """Format a tool call for display. Returns None for AskUserQuestion."""
    if tool_name == 'Bash':
        cmd = input_data.get('command', '')
        escaped_cmd = escape_html(cmd)
        if '\n' in cmd:
            return f'<pre><code class="language-bash">{escaped_cmd}</code></pre>'
        else:
            return f'<b>$</b> <code>{escaped_cmd}</code>'
    elif tool_name == 'Read':
        path = escape_html(input_data.get('file_path', ''))
        return f'📖 <b>Reading</b> <code>{path}</code>'
    elif tool_name == 'Write':
        path = escape_html(input_data.get('file_path', ''))
        return f'📝 <b>Writing</b> <code>{path}</code>'
    elif tool_name == 'Edit':
        path = escape_html(input_data.get('file_path', ''))
        return f'✏️ <b>Editing</b> <code>{path}</code>'
    elif tool_name == 'Glob':
        pattern = escape_html(input_data.get('pattern', ''))
        return f'🔍 <b>Finding</b> <code>{pattern}</code>'
    elif tool_name == 'Grep':
        pattern = escape_html(input_data.get('pattern', ''))
        return f'🔎 <b>Searching</b> <code>{pattern}</code>'
    elif tool_name == 'Task':
        desc = escape_html(input_data.get('description', ''))
        return f'🤖 <b>Subagent:</b> {desc}'
    elif tool_name == 'WebFetch':
        url = escape_html(input_data.get('url', ''))
        return f'🌐 <b>Fetching</b> <code>{url}</code>'
    elif tool_name == 'WebSearch':
        query = escape_html(input_data.get('query', ''))
        return f'🔍 <b>Web search:</b> {query}'
    elif tool_name == 'TodoWrite':
        todos = input_data.get('todos', [])
        if not todos:
            return '📋 <b>Clearing todos</b>'
        lines = ['📋 <b>Todos:</b>']
        for todo in todos:
            status = todo.get('status', 'pending')
            content = escape_html(todo.get('content', ''))
            if status == 'completed':
                lines.append(f'  ✅ <s>{content}</s>')
            elif status == 'in_progress':
                lines.append(f'  🔄 {content}')
            else:
                lines.append(f'  ⬜ {content}')
        return '\n'.join(lines)
    elif tool_name == 'AskUserQuestion':
        return None
    else:
        return f'🔧 <b>{escape_html(tool_name)}</b>'


def format_tool_result(content: str | list, is_error: bool = False) -> str | None:
    """Format a tool result for display. Returns None if empty."""
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

    if '\n' in result_text or len(result_text) > 200:
        prefix = '❌ ' if is_error else ''
        if len(result_text) > 200:
            return f'{prefix}<blockquote expandable>{escaped}</blockquote>'
        else:
            return f'{prefix}<blockquote>{escaped}</blockquote>'
    else:
        icon = '❌' if is_error else '✅'
        return f'{icon} {escaped}'


def format_permission_edit(input_data: dict[str, Any]) -> str:
    """Format Edit tool permission prompt showing changes."""
    file_path = input_data.get('file_path', '')
    old_string = input_data.get('old_string', '')
    new_string = input_data.get('new_string', '')

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
        return f'<b>🔧 {escape_html(tool_name)}</b>\n\n<pre>{escape_html(json.dumps(input_data, indent=2)[:1000])}</pre>'


def format_pinned_status(
    permission_mode: str,
    current_model: str | None,
    context_percent: int,
    total_cost: float,
) -> str:
    """Format the pinned status message content."""
    from rclaude.core import format_mode_short, format_model_short

    mode_icon = format_mode_short(permission_mode)
    model_display = format_model_short(current_model)

    parts = [f'{mode_icon} <b>{permission_mode}</b>', model_display]

    if context_percent > 0:
        parts.append(f'📝 {context_percent}%')

    if total_cost > 0:
        parts.append(f'💰 ${total_cost:.4f}')

    return ' | '.join(parts)
