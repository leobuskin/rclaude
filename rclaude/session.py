"""Session management for Claude SDK clients."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

# File to persist session state across hot-reloads
SESSION_STATE_FILE = Path('/tmp/rclaude-session-state.json')


@dataclass
class PendingQuestion:
    """Tracks a pending AskUserQuestion interaction."""

    tool_use_id: str
    questions: list[dict[str, Any]]
    answers: dict[str, str] = field(default_factory=dict)
    current_question_idx: int = 0


@dataclass
class PendingPermission:
    """Tracks a pending tool permission request."""

    request_id: str
    tool_name: str
    input_data: dict[str, Any]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Any = None  # Will be PermissionResultAllow or PermissionResultDeny


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
    pending_permission: PendingPermission | None = None
    waiting_for_rejection_reason: bool = False
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


def save_session_state() -> None:
    """Save session state to disk for hot-reload persistence."""
    state = {}
    for user_id, session in _sessions.items():
        if session.session_id:  # Only save if there's a session to resume
            state[str(user_id)] = {
                'session_id': session.session_id,
                'cwd': session.cwd,
                'is_processing': session.is_processing,
            }
    if state:
        SESSION_STATE_FILE.write_text(json.dumps(state))
    elif SESSION_STATE_FILE.exists():
        SESSION_STATE_FILE.unlink()


def load_session_state() -> dict[int, dict[str, Any]]:
    """Load saved session state from disk."""
    if not SESSION_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(SESSION_STATE_FILE.read_text())
        # Convert string keys back to int
        return {int(k): v for k, v in data.items()}
    except (json.JSONDecodeError, ValueError):
        return {}


def clear_session_state() -> None:
    """Clear saved session state file."""
    if SESSION_STATE_FILE.exists():
        SESSION_STATE_FILE.unlink()
