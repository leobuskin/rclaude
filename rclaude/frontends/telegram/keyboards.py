"""Telegram inline keyboard builders."""

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Edit tools that get special keyboard options
EDIT_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}


def create_permission_keyboard(tool_name: str | None = None) -> InlineKeyboardMarkup:
    """Create inline keyboard for permission approval.

    For edit tools (Edit, Write, NotebookEdit), shows 'Accept Edits' instead of 'Always'.
    """
    if tool_name and tool_name in EDIT_TOOLS:
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


def create_question_keyboard(question: dict[str, Any]) -> InlineKeyboardMarkup:
    """Create an inline keyboard for an AskUserQuestion option."""
    buttons: list[list[InlineKeyboardButton]] = []
    options = question.get('options', [])

    for i, opt in enumerate(options):
        label = opt.get('label', f'Option {i + 1}')
        buttons.append([InlineKeyboardButton(label, callback_data=f'q:0:{i}')])

    buttons.append([InlineKeyboardButton('Other (type answer)', callback_data='q:0:other')])

    return InlineKeyboardMarkup(buttons)


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
            label = f'• {label}'
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
