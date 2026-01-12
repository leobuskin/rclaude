"""Claude SDK wrapper for frontend-agnostic client management."""

import asyncio
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable, Coroutine

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput

from .events import (
    ErrorEvent,
    Event,
    QuestionEvent,
    TextEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from .permissions import PermissionChecker, parse_context_output
from .session import PendingPermission, PendingQuestion, Session

logger = logging.getLogger('rclaude')


def get_local_claude_cli() -> str | None:
    """Find local Claude CLI, prefer it over SDK bundled version."""
    local_claude = Path.home() / '.claude' / 'local' / 'claude'
    if local_claude.exists():
        return str(local_claude)

    claude_path = shutil.which('claude')
    if claude_path:
        return claude_path

    return None


async def dummy_pretool_hook(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> SyncHookJSONOutput:
    """Keep stream open for can_use_tool callback.

    Required workaround: In Python, can_use_tool requires a PreToolUse hook that
    returns {"continue_": True} to keep the stream open. Without this hook, the
    stream closes before the permission callback can be invoked.
    """
    result: SyncHookJSONOutput = {'continue_': True}
    return result


PermissionHandler = Callable[
    [str, dict[str, Any], ToolPermissionContext], Coroutine[Any, Any, PermissionResultAllow | PermissionResultDeny]
]


def create_permission_handler(
    session: Session,
    request_permission: Callable[[Session, PendingPermission], Coroutine[Any, Any, None]],
) -> PermissionHandler:
    """Create a permission handler that routes permission requests to a callback.

    Args:
        session: The session to handle permissions for
        request_permission: Async callback to show permission UI to user
    """
    checker = PermissionChecker()

    async def permission_handler(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Handle tool permission requests."""
        logger.info(f'[PERMISSION] can_use_tool called: tool={tool_name}, mode={session.permission_mode}')

        # Check if should auto-allow
        if checker.should_auto_allow(tool_name, input_data, session.permission_mode, session.cwd):
            logger.info(f'[PERMISSION] Auto-allowing {tool_name}')
            return PermissionResultAllow(updated_input=input_data)

        # Create pending permission request
        request_id = str(uuid.uuid4())
        pending = PendingPermission(
            request_id=request_id,
            tool_name=tool_name,
            input_data=input_data,
        )
        session.pending_permission = pending
        logger.info(f'[PERMISSION] Created pending permission: {request_id}')

        # Request permission via callback (frontend will show UI)
        try:
            await asyncio.wait_for(
                request_permission(session, pending),
                timeout=10,
            )
            logger.info('[PERMISSION] Sent permission request to frontend')
        except asyncio.TimeoutError:
            logger.warning('[PERMISSION] Timeout sending permission request (10s), allowing operation')
            session.pending_permission = None
            return PermissionResultAllow(updated_input=input_data)
        except Exception as e:
            logger.error(f'Failed to send permission request: {e}')
            session.pending_permission = None
            return PermissionResultAllow(updated_input=input_data)

        # Wait for user response
        logger.info('[PERMISSION] Waiting for user response...')
        try:
            await pending.event.wait()
            logger.info(f'[PERMISSION] Got response: {type(pending.result).__name__}')
        except Exception as e:
            logger.error(f'[PERMISSION] Exception during wait: {e}')
            session.pending_permission = None
            return PermissionResultDeny(message=f'Error waiting: {e}', interrupt=False)

        session.pending_permission = None

        if pending.result is not None:
            return pending.result
        else:
            logger.warning('[PERMISSION] No result set, returning deny')
            return PermissionResultDeny(message='No response received', interrupt=False)

    return permission_handler


async def create_client(
    session: Session,
    permission_handler: PermissionHandler,
) -> ClaudeSDKClient:
    """Create and connect a Claude SDK client for a session."""
    options = ClaudeAgentOptions(
        setting_sources=['user', 'project', 'local'],
        permission_mode=session.permission_mode,
        can_use_tool=permission_handler,
        cwd=session.cwd,
        resume=session.claude_session_id,
        cli_path=get_local_claude_cli(),
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    session.client = client
    return client


async def process_response(session: Session) -> AsyncIterator[Event]:
    """Process Claude response and yield events.

    This is a generator that yields events as they come from the SDK.
    The caller is responsible for handling each event type appropriately.
    """
    logger.info('[PROCESS] process_response called')
    if not session.client:
        logger.warning('[PROCESS] No client, returning')
        return

    session.is_processing = True
    response_text = ''
    tool_calls: dict[str, ToolCallEvent] = {}  # tool_id -> event
    is_final = False

    try:
        logger.info('[PROCESS] Starting to receive response from SDK')
        message_count = 0
        async for message in session.client.receive_response():
            message_count += 1
            logger.info(f'[PROCESS] Received message #{message_count}: {type(message).__name__}')
            if isinstance(message, AssistantMessage):
                logger.debug(f'[SDK] AssistantMessage: {len(message.content)} blocks')
                for block in message.content:
                    if isinstance(block, TextBlock):
                        logger.debug(f'[SDK] TextBlock: len={len(block.text)}')
                        response_text += block.text

                    elif isinstance(block, ToolUseBlock):
                        # Send any accumulated text first
                        if response_text.strip():
                            logger.info(f'[YIELD] TextEvent (pre-tool): len={len(response_text)}')
                            yield TextEvent(session_id=session.id, content=response_text, is_final=False)
                            response_text = ''

                        # Handle AskUserQuestion specially
                        if block.name == 'AskUserQuestion':
                            questions = block.input.get('questions', [])
                            if questions:
                                session.pending_question = PendingQuestion(
                                    tool_use_id=block.id,
                                    questions=questions,
                                )
                                yield QuestionEvent(
                                    session_id=session.id,
                                    question_id=block.id,
                                    questions=questions,
                                )
                                session.is_processing = False
                                return

                        # Emit tool call event
                        event = ToolCallEvent(
                            session_id=session.id,
                            tool_name=block.name,
                            tool_id=block.id,
                            input_data=block.input,
                        )
                        tool_calls[block.id] = event
                        yield event

            elif isinstance(message, UserMessage):
                content = message.content
                if isinstance(content, str):
                    if '<local-command-stdout>' in content:
                        context_usage = parse_context_output(content)
                        if context_usage:
                            session.context = context_usage
                            logger.info(f'[CONTEXT] Parsed: {context_usage.percent_used}%')
                else:
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            tool_event = tool_calls.get(block.tool_use_id)
                            yield ToolResultEvent(
                                session_id=session.id,
                                tool_id=block.tool_use_id,
                                tool_name=tool_event.tool_name if tool_event else '',
                                content=block.content if isinstance(block.content, str) else str(block.content),
                                is_error=block.is_error or False,
                            )
                        elif isinstance(block, TextBlock):
                            if '<local-command-stdout>' in block.text:
                                context_usage = parse_context_output(block.text)
                                if context_usage:
                                    session.context = context_usage

            elif isinstance(message, SystemMessage):
                logger.info(f'[SYSTEM] subtype={message.subtype} data={message.data}')
                data = message.data
                text_content = data.get('message') or data.get('text') or data.get('content') or data.get('result')

                if text_content:
                    text_str = str(text_content)
                    response_text += text_str
                    context_usage = parse_context_output(text_str)
                    if context_usage:
                        session.context = context_usage

            elif isinstance(message, ResultMessage):
                logger.info(f'[RESULT] is_error={message.is_error}, result={message.result}, session_id={message.session_id[:8] if message.session_id else None}..., num_turns={message.num_turns}')
                if message.is_error and message.result:
                    response_text += f'\n\n❌ Error: {message.result}'

                if message.session_id and not session.claude_session_id:
                    session.claude_session_id = message.session_id
                    logger.info(f'[SESSION] Captured session_id: {message.session_id[:8]}...')

                # Track usage
                session.usage.num_turns += message.num_turns
                if message.total_cost_usd is not None:
                    session.usage.last_response_cost = message.total_cost_usd
                    session.usage.total_cost_usd += message.total_cost_usd
                if message.usage:
                    session.usage.last_response_tokens = message.usage
                    session.usage.total_input_tokens += message.usage.get('input_tokens', 0)
                    session.usage.total_output_tokens += message.usage.get('output_tokens', 0)

                is_final = True

        # Send any remaining text - inside try so is_processing stays True during handling
        if response_text.strip():
            logger.info(f'[YIELD] TextEvent (final): len={len(response_text)}, is_final={is_final}')
            yield TextEvent(session_id=session.id, content=response_text, is_final=is_final)
        else:
            logger.info(f'[YIELD] No final text (response_text empty or whitespace)')

    except Exception as e:
        logger.error(f'[PROCESS] Exception in process_response: {e}')
        yield ErrorEvent(session_id=session.id, message=str(e))

    finally:
        logger.info('[PROCESS] process_response finished, setting is_processing=False')
        session.is_processing = False


async def fetch_context(session: Session) -> None:
    """Fetch context usage by running /context command."""
    if not session.client:
        return

    try:
        await session.client.query('/context')
        # Must consume ALL messages from the stream, not just until we find context
        async for message in session.client.receive_response():
            if isinstance(message, SystemMessage):
                text_content = message.data.get('message') or message.data.get('text') or message.data.get('result')
                if text_content:
                    context_usage = parse_context_output(str(text_content))
                    if context_usage:
                        session.context = context_usage
                        logger.debug(f'[CONTEXT] Fetched: {context_usage.percent_used}%')
                        # Don't return - keep consuming until stream ends
            elif isinstance(message, UserMessage):
                content = message.content
                if isinstance(content, str):
                    context_usage = parse_context_output(content)
                    if context_usage:
                        session.context = context_usage
                        logger.debug(f'[CONTEXT] Fetched from UserMessage: {context_usage.percent_used}%')
                        # Don't return - keep consuming until stream ends
            elif isinstance(message, ResultMessage):
                logger.debug(f'[CONTEXT] ResultMessage received, stream complete')
    except Exception as e:
        logger.warning(f'Failed to fetch context: {e}')
