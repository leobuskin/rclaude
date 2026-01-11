"""Authorization logic."""

from rclaude.config import ALLOWED_USERS


def is_authorized(user_id: int) -> bool:
    """Check if user is authorized to use the bot."""
    return user_id in ALLOWED_USERS
