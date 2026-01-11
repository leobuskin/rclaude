"""Frontend-agnostic session management."""

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import ClaudeSDKClient

from .events import Event

# Permission modes matching Claude Code CLI
PermissionMode = Literal['default', 'acceptEdits', 'plan', 'bypassPermissions']

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
class ContextUsage:
    """Tracks context window usage."""

    tokens_used: int = 0
    tokens_max: int = 0
    percent_used: int = 0


@dataclass
class SessionUsage:
    """Tracks usage and cost for a session."""

    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    num_turns: int = 0
    last_response_cost: float | None = None
    last_response_tokens: dict[str, Any] | None = None


@dataclass
class Session:
    """Frontend-agnostic session state."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    claude_session_id: str | None = None  # Claude Code's session ID for resumption
    cwd: str = field(default_factory=os.getcwd)
    permission_mode: PermissionMode = 'default'
    current_model: str | None = None
    terminal_id: str | None = None  # Which terminal this session belongs to

    # Claude SDK client
    client: ClaudeSDKClient | None = None
    is_processing: bool = False

    # Pending interactions
    pending_question: PendingQuestion | None = None
    pending_permission: PendingPermission | None = None
    waiting_for_rejection_reason: bool = False
    waiting_for_question_answer: bool = False

    # Usage tracking
    usage: SessionUsage = field(default_factory=SessionUsage)
    context: ContextUsage = field(default_factory=ContextUsage)

    # Event queue for streaming updates
    event_queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)

    async def emit(self, event: Event) -> None:
        """Emit an event to listeners."""
        await self.event_queue.put(event)

    async def disconnect(self) -> None:
        """Disconnect the Claude client."""
        if self.client:
            await self.client.disconnect()
            self.client = None


class SessionManager:
    """Manages sessions across all frontends."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}  # session_id -> Session
        self._frontend_mappings: dict[str, str] = {}  # frontend_user_id -> session_id

    def get(self, session_id: str) -> Session | None:
        """Get session by ID."""
        return self._sessions.get(session_id)

    def get_by_frontend_user(self, frontend_user_id: str) -> Session | None:
        """Get session for a frontend user."""
        session_id = self._frontend_mappings.get(frontend_user_id)
        if session_id:
            return self._sessions.get(session_id)
        return None

    def get_or_create(self, frontend_user_id: str) -> Session:
        """Get existing session or create new one for a frontend user."""
        existing = self.get_by_frontend_user(frontend_user_id)
        if existing:
            return existing

        session = Session()
        self._sessions[session.id] = session
        self._frontend_mappings[frontend_user_id] = session.id
        return session

    def link_frontend(self, session_id: str, frontend_user_id: str) -> bool:
        """Link a frontend user to an existing session."""
        if session_id not in self._sessions:
            return False
        self._frontend_mappings[frontend_user_id] = session_id
        return True

    async def clear(self, frontend_user_id: str) -> None:
        """Clear and disconnect a session for a frontend user."""
        session_id = self._frontend_mappings.pop(frontend_user_id, None)
        if session_id and session_id in self._sessions:
            session = self._sessions.pop(session_id)
            await session.disconnect()

    def all_sessions(self) -> list[Session]:
        """Get all active sessions."""
        return list(self._sessions.values())

    def save_state(self) -> None:
        """Save session state to disk for hot-reload persistence."""
        state = {}
        for frontend_id, session_id in self._frontend_mappings.items():
            session = self._sessions.get(session_id)
            if session and session.claude_session_id:
                state[frontend_id] = {
                    'session_id': session.id,
                    'claude_session_id': session.claude_session_id,
                    'terminal_id': session.terminal_id,
                    'cwd': session.cwd,
                    'is_processing': session.is_processing,
                    'permission_mode': session.permission_mode,
                }
        if state:
            SESSION_STATE_FILE.write_text(json.dumps(state))
        elif SESSION_STATE_FILE.exists():
            SESSION_STATE_FILE.unlink()

    def load_state(self) -> None:
        """Load saved session state from disk."""
        if not SESSION_STATE_FILE.exists():
            return
        try:
            data = json.loads(SESSION_STATE_FILE.read_text())
            for frontend_id, state in data.items():
                session = Session(
                    id=state['session_id'],
                    claude_session_id=state.get('claude_session_id'),
                    terminal_id=state.get('terminal_id'),
                    cwd=state.get('cwd', os.getcwd()),
                    is_processing=state.get('is_processing', False),
                    permission_mode=state.get('permission_mode', 'default'),
                )
                self._sessions[session.id] = session
                self._frontend_mappings[frontend_id] = session.id
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    def clear_state_file(self) -> None:
        """Clear saved session state file."""
        if SESSION_STATE_FILE.exists():
            SESSION_STATE_FILE.unlink()


# Global session manager instance
_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Get or create the global session manager."""
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager
