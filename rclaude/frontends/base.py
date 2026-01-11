"""Frontend abstraction protocol."""

from abc import ABC, abstractmethod
from typing import Any

from rclaude.core.events import (
    Event,
    PermissionRequestEvent,
    QuestionEvent,
    TextEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from rclaude.core.session import PendingPermission, Session


class Frontend(ABC):
    """Abstract base class for UI frontends.

    Frontends are responsible for:
    - Displaying Claude's responses to users
    - Collecting user input (messages, permission decisions, question answers)
    - Showing status updates (cost, context usage, etc.)
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the frontend (e.g., start polling for Telegram)."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the frontend gracefully."""
        pass

    @abstractmethod
    async def send_text(
        self,
        session: Session,
        text: str,
        is_final: bool = False,
    ) -> None:
        """Send text message to user.

        Args:
            session: The session this message belongs to
            text: The text content (may contain markdown)
            is_final: True if this is the final message (enable sound notification)
        """
        pass

    @abstractmethod
    async def send_tool_call(
        self,
        session: Session,
        event: ToolCallEvent,
    ) -> Any:
        """Send tool call notification.

        Args:
            session: The session this belongs to
            event: The tool call event

        Returns:
            A reference to the sent message (for later editing with result)
        """
        pass

    @abstractmethod
    async def send_tool_result(
        self,
        session: Session,
        event: ToolResultEvent,
        tool_msg_ref: Any,
    ) -> None:
        """Send/update tool result.

        Args:
            session: The session this belongs to
            event: The tool result event
            tool_msg_ref: Reference from send_tool_call (for editing)
        """
        pass

    @abstractmethod
    async def request_permission(
        self,
        session: Session,
        pending: PendingPermission,
    ) -> None:
        """Show permission request UI.

        The frontend should display the permission request and wait for user input.
        When the user responds, set pending.result and call pending.event.set().

        Args:
            session: The session requesting permission
            pending: The pending permission with tool_name and input_data
        """
        pass

    @abstractmethod
    async def request_question_answer(
        self,
        session: Session,
        event: QuestionEvent,
    ) -> None:
        """Show question UI with options.

        The frontend should display the question(s) and collect answers.
        When complete, the answers should be submitted back to Claude.

        Args:
            session: The session with the question
            event: The question event with questions list
        """
        pass

    @abstractmethod
    async def update_status(
        self,
        session: Session,
    ) -> None:
        """Update status display (e.g., pinned message with cost/context).

        Args:
            session: The session whose status changed
        """
        pass

    @abstractmethod
    async def notify_teleport(
        self,
        session: Session,
        session_id: str,
        cwd: str,
        permission_mode: str,
    ) -> None:
        """Notify user of incoming session teleport.

        Args:
            session: The session (may be new)
            session_id: Claude Code session ID
            cwd: Working directory
            permission_mode: Current permission mode
        """
        pass

    async def handle_event(self, session: Session, event: Event) -> Any:
        """Handle an event from Claude.

        Default implementation dispatches to specific handlers.
        Returns message reference for tool calls.
        """
        if isinstance(event, TextEvent):
            await self.send_text(session, event.content, event.is_final)
        elif isinstance(event, ToolCallEvent):
            return await self.send_tool_call(session, event)
        elif isinstance(event, QuestionEvent):
            await self.request_question_answer(session, event)
        elif isinstance(event, PermissionRequestEvent):
            pending = session.pending_permission
            if pending:
                await self.request_permission(session, pending)
        return None


class FrontendRegistry:
    """Registry of active frontends."""

    def __init__(self) -> None:
        self._frontends: dict[str, Frontend] = {}

    def register(self, name: str, frontend: Frontend) -> None:
        """Register a frontend with a name."""
        self._frontends[name] = frontend

    def get(self, name: str) -> Frontend | None:
        """Get a frontend by name."""
        return self._frontends.get(name)

    def all(self) -> dict[str, Frontend]:
        """Get all registered frontends."""
        return dict(self._frontends)

    async def start_all(self) -> None:
        """Start all registered frontends."""
        for frontend in self._frontends.values():
            await frontend.start()

    async def stop_all(self) -> None:
        """Stop all registered frontends."""
        for frontend in self._frontends.values():
            await frontend.stop()
