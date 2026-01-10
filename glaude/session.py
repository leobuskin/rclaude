"""Session management for Claude SDK clients."""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeSDKClient


@dataclass
class PendingQuestion:
    """Tracks a pending AskUserQuestion interaction."""

    tool_use_id: str
    questions: list[dict[str, Any]]
    answers: dict[str, str] = field(default_factory=dict)
    current_question_idx: int = 0


@dataclass
class SessionUpdate:
    """An update to broadcast to listeners."""

    type: str  # 'text', 'tool_call', 'tool_result', 'user', 'error'
    content: str


@dataclass
class UserSession:
    """Manages a user's Claude session state."""

    client: ClaudeSDKClient | None = None
    is_processing: bool = False
    pending_question: PendingQuestion | None = None
    cwd: str = field(default_factory=os.getcwd)
    session_id: str | None = None  # Track current session ID for /cc
    # Queue for streaming updates to terminal
    update_queue: asyncio.Queue[SessionUpdate] = field(default_factory=asyncio.Queue)


# Global session storage (in production, use Redis or similar)
_sessions: dict[int, UserSession] = {}


def get_session(user_id: int) -> UserSession:
    """Get or create a session for a user."""
    if user_id not in _sessions:
        _sessions[user_id] = UserSession()
    return _sessions[user_id]


async def clear_session(user_id: int) -> None:
    """Clear and disconnect a user's session."""
    if user_id in _sessions:
        session = _sessions[user_id]
        if session.client:
            await session.client.disconnect()
        del _sessions[user_id]
