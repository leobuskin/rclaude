"""Event types for the message bus between core and frontends."""

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

EventType = Literal[
    'text',
    'tool_call',
    'tool_result',
    'question',
    'permission_request',
    'user',
    'session_start',
    'session_end',
    'return_to_terminal',
    'superseded',
    'error',
]


@dataclass
class Event:
    """Base event for message bus."""

    session_id: str
    type: ClassVar[EventType]


@dataclass
class TextEvent(Event):
    """Text content from Claude."""

    type: ClassVar[Literal['text']] = 'text'
    content: str = ''
    is_final: bool = False  # True for last message (enables notification sound)


@dataclass
class ToolCallEvent(Event):
    """Tool invocation by Claude."""

    type: ClassVar[Literal['tool_call']] = 'tool_call'
    tool_name: str = ''
    tool_id: str = ''
    input_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultEvent(Event):
    """Result of a tool execution."""

    type: ClassVar[Literal['tool_result']] = 'tool_result'
    tool_id: str = ''
    tool_name: str = ''
    content: str = ''
    is_error: bool = False


@dataclass
class QuestionEvent(Event):
    """AskUserQuestion from Claude."""

    type: ClassVar[Literal['question']] = 'question'
    question_id: str = ''
    questions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PermissionRequestEvent(Event):
    """Permission request for a tool."""

    type: ClassVar[Literal['permission_request']] = 'permission_request'
    request_id: str = ''
    tool_name: str = ''
    input_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserMessageEvent(Event):
    """User message sent to Claude."""

    type: ClassVar[Literal['user']] = 'user'
    content: str = ''


@dataclass
class SessionStartEvent(Event):
    """Session started/resumed."""

    type: ClassVar[Literal['session_start']] = 'session_start'
    cwd: str = ''
    resumed: bool = False


@dataclass
class SessionEndEvent(Event):
    """Session ended."""

    type: ClassVar[Literal['session_end']] = 'session_end'
    reason: str = ''


@dataclass
class ReturnToTerminalEvent(Event):
    """Signal to return session to terminal."""

    type: ClassVar[Literal['return_to_terminal']] = 'return_to_terminal'
    claude_session_id: str = ''


@dataclass
class ErrorEvent(Event):
    """Error occurred."""

    type: ClassVar[Literal['error']] = 'error'
    message: str = ''


@dataclass
class SupersededEvent(Event):
    """Session superseded by another terminal."""

    type: ClassVar[Literal['superseded']] = 'superseded'
