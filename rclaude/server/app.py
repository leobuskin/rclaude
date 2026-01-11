"""HTTP server and main entry point."""

import asyncio
import json
import logging
import os
import signal as sig
from pathlib import Path

from aiohttp import web

from rclaude.core import SessionManager, get_session_manager
from rclaude.core.events import ReturnToTerminalEvent, SupersededEvent
from rclaude.frontends import FrontendRegistry
from rclaude.frontends.telegram import TelegramFrontend
from rclaude.settings import Config

logger = logging.getLogger('rclaude')

# Connection tracking for auto-shutdown
_sse_connection_count = 0
_shutdown_event: asyncio.Event | None = None


def _get_shutdown_event() -> asyncio.Event:
    """Get or create the shutdown event."""
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def _get_watcher_pid_file(wrapper_pid: int) -> Path:
    """Get the watcher PID file path for a given wrapper."""
    return Path(f'/tmp/rclaude-watcher-{wrapper_pid}.pid')


def _trigger_shutdown() -> None:
    """Trigger server shutdown if started by wrapper (not standalone)."""
    wrapper_pid = os.environ.get('RCLAUDE_WRAPPER_PID')
    if not wrapper_pid:
        logger.info('[SHUTDOWN] Standalone server, not shutting down')
        return

    event = _get_shutdown_event()
    event.set()
    logger.info('[SHUTDOWN] Server shutdown triggered')

    pid_file = _get_watcher_pid_file(int(wrapper_pid))
    if pid_file.exists():
        try:
            watcher_pid = int(pid_file.read_text().strip())
            os.kill(watcher_pid, sig.SIGTERM)
            logger.info(f'[SHUTDOWN] Sent SIGTERM to watcher pid {watcher_pid}')
            pid_file.unlink(missing_ok=True)
        except (ValueError, OSError, ProcessLookupError) as e:
            logger.warning(f'[SHUTDOWN] Could not kill watcher: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Route Handlers
# ─────────────────────────────────────────────────────────────────────────────


async def handle_teleport(request: web.Request) -> web.Response:
    """Handle POST /teleport from Claude Code /tg hook."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({'error': 'Invalid JSON'}, status=400)

    session_id = data.get('session_id')
    cwd = data.get('cwd', '.')
    permission_mode = data.get('permission_mode', 'default')
    terminal_id = data.get('terminal_id')

    if not session_id:
        return web.json_response({'error': 'session_id required'}, status=400)
    if not terminal_id:
        return web.json_response({'error': 'terminal_id required'}, status=400)

    config: Config = request.app['config']
    user_id = config.telegram.user_id

    if not user_id:
        return web.json_response({'error': 'No Telegram user configured'}, status=400)

    logger.info(f'Teleport received: session={session_id[:8]}..., terminal={terminal_id[:8]}..., mode={permission_mode}')

    # Get frontend and store teleport
    frontend: TelegramFrontend = request.app['frontends'].get('telegram')
    session_manager: SessionManager = request.app['session_manager']

    if frontend:
        # Store pending teleport
        frontend.store_teleport(
            user_id,
            {
                'session_id': session_id,
                'cwd': cwd,
                'terminal_id': terminal_id,
                'permission_mode': permission_mode,
            },
        )

        # Get or create session and update terminal_id
        frontend_user_id = f'telegram:{user_id}'
        session = session_manager.get_or_create(frontend_user_id)
        session.terminal_id = terminal_id

        # Send notification asynchronously
        async def send_notification() -> None:
            try:
                await frontend.notify_teleport(session, session_id, cwd, permission_mode)
            except Exception as e:
                logger.error(f'Failed to send teleport notification: {e}')

        asyncio.create_task(send_notification())

    return web.json_response({'ok': True, 'message': 'Teleport initiated'})


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({'status': 'ok'})


async def handle_can_reload(request: web.Request) -> web.Response:
    """Check if server can safely reload (all sessions idle)."""
    session_manager: SessionManager = request.app['session_manager']

    sessions = session_manager.all_sessions()
    any_processing = any(s.is_processing for s in sessions)
    reload_pending = request.app.get('reload_pending', False)
    force_reload = request.app.get('force_reload', False)

    return web.json_response({
        'can_reload': not any_processing,
        'force_reload': force_reload,
        'reload_pending': reload_pending,
        'sessions': len(sessions),
        'processing': sum(1 for s in sessions if s.is_processing),
    })


async def handle_request_reload(request: web.Request) -> web.Response:
    """Signal that a reload is requested. Notifies users and sets pending flag."""
    request.app['reload_pending'] = True

    # Notify via Telegram frontend if available
    frontend = request.app.get('telegram_frontend')
    if frontend:
        await frontend.notify_reload_pending()

    session_manager: SessionManager = request.app['session_manager']
    sessions = session_manager.all_sessions()
    any_processing = any(s.is_processing for s in sessions)

    return web.json_response({
        'ok': True,
        'can_reload': not any_processing,
        'waiting': any_processing,
    })


async def handle_force_reload(request: web.Request) -> web.Response:
    """Force reload flag - allows reload even if processing."""
    request.app['force_reload'] = True
    return web.json_response({'ok': True, 'message': 'Force reload enabled'})


async def handle_prepare_reload(request: web.Request) -> web.Response:
    """Prepare for hot-reload by saving session state."""
    session_manager: SessionManager = request.app['session_manager']

    # Clear reload flags
    request.app['reload_pending'] = False
    request.app['force_reload'] = False

    # Notify user that reload is happening now
    frontend = request.app.get('telegram_frontend')
    if frontend:
        await frontend.notify_reloading()

    # Disconnect all clients
    for session in session_manager.all_sessions():
        if session.client:
            try:
                await session.client.disconnect()
            except Exception as e:
                logger.warning(f'Error disconnecting client: {e}')
            session.client = None

    # Save state
    session_manager.save_state()
    logger.info('Session state saved for hot-reload')

    return web.json_response({'ok': True, 'message': 'Ready for reload'})


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """SSE endpoint to stream session updates to terminal."""
    global _sse_connection_count

    config: Config = request.app['config']
    user_id = config.telegram.user_id

    if not user_id:
        return web.json_response({'error': 'No user configured'}, status=400)

    terminal_id = request.query.get('terminal_id')
    if not terminal_id:
        return web.json_response({'error': 'terminal_id required'}, status=400)

    session_manager: SessionManager = request.app['session_manager']
    frontend_user_id = f'telegram:{user_id}'
    session = session_manager.get_or_create(frontend_user_id)

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        },
    )
    await response.prepare(request)

    _sse_connection_count += 1
    logger.info(f'[SSE] Connection opened for terminal {terminal_id[:8]}..., count={_sse_connection_count}')

    await response.write(b'event: connected\ndata: {}\n\n')

    try:
        while True:
            # Check if this terminal has been superseded
            if session.terminal_id and session.terminal_id != terminal_id:
                logger.info(f'[SSE] Terminal {terminal_id[:8]}... superseded by {session.terminal_id[:8]}...')
                data = json.dumps({'type': 'superseded', 'content': 'Another terminal took over'})
                await response.write(f'event: update\ndata: {data}\n\n'.encode())
                break

            try:
                event = await asyncio.wait_for(session.event_queue.get(), timeout=30)

                # Convert event to dict for JSON - handle different event types
                if isinstance(event, ReturnToTerminalEvent):
                    event_data = {
                        'type': event.type,
                        'content': event.claude_session_id or '',
                    }
                elif isinstance(event, SupersededEvent):
                    event_data = {
                        'type': event.type,
                        'content': 'Another terminal took over',
                    }
                else:
                    event_data = {
                        'type': event.type,
                        'content': getattr(event, 'content', getattr(event, 'message', '')),
                    }
                data = json.dumps(event_data)
                await response.write(f'event: update\ndata: {data}\n\n'.encode())

                if isinstance(event, (ReturnToTerminalEvent, SupersededEvent)):
                    logger.info(f'[SSE] Sent {event.type}, closing connection')
                    break
            except asyncio.TimeoutError:
                await response.write(b'event: keepalive\ndata: {}\n\n')
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _sse_connection_count -= 1
        logger.info(f'[SSE] Connection closed for terminal {terminal_id[:8]}..., count={_sse_connection_count}')

        if _sse_connection_count == 0 and session.client is None:
            logger.info('[SSE] No connections and no active session, triggering shutdown')
            _trigger_shutdown()

    return response


async def handle_setup_link_register(request: web.Request) -> web.Response:
    """Register a setup link token."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({'error': 'Invalid JSON'}, status=400)

    token = data.get('token', '').upper()
    if not token:
        return web.json_response({'error': 'token required'}, status=400)

    # Register in Telegram frontend bot_data (single source of truth)
    frontend: TelegramFrontend | None = request.app['frontends'].get('telegram')
    if not frontend or not frontend.app:
        return web.json_response({'error': 'Telegram frontend not available'}, status=503)

    if 'pending_setup_links' not in frontend.app.bot_data:
        frontend.app.bot_data['pending_setup_links'] = {}

    frontend.app.bot_data['pending_setup_links'][token] = {
        'event': asyncio.Event(),
        'result': None,
    }

    return web.json_response({'ok': True, 'message': 'Link token registered'})


async def handle_setup_link_wait(request: web.Request) -> web.Response:
    """Wait for a setup link to complete."""
    token = request.match_info.get('token', '').upper()

    frontend: TelegramFrontend | None = request.app['frontends'].get('telegram')
    if not frontend or not frontend.app:
        return web.json_response({'error': 'Telegram frontend not available'}, status=503)

    pending_links = frontend.app.bot_data.get('pending_setup_links', {})
    if token not in pending_links:
        return web.json_response({'error': 'Token not registered'}, status=404)

    pending = pending_links[token]

    try:
        await asyncio.wait_for(pending['event'].wait(), timeout=300)
    except asyncio.TimeoutError:
        pending_links.pop(token, None)
        return web.json_response({'error': 'Timeout waiting for link'}, status=408)

    result = pending.get('result')
    pending_links.pop(token, None)

    if result:
        user_id, username = result
        return web.json_response({'ok': True, 'user_id': user_id, 'username': username})

    return web.json_response({'error': 'Link failed'}, status=500)


def create_app(
    config: Config,
    session_manager: SessionManager,
    frontend_registry: FrontendRegistry,
) -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app['config'] = config
    app['session_manager'] = session_manager
    app['frontends'] = frontend_registry

    app.router.add_post('/teleport', handle_teleport)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/api/can-reload', handle_can_reload)
    app.router.add_post('/api/request-reload', handle_request_reload)
    app.router.add_post('/api/force-reload', handle_force_reload)
    app.router.add_post('/api/prepare-reload', handle_prepare_reload)
    app.router.add_get('/stream', handle_stream)
    app.router.add_post('/api/setup-link', handle_setup_link_register)
    app.router.add_get('/api/setup-link/{token}', handle_setup_link_wait)

    return app


async def run_server(config: Config) -> None:
    """Run the multi-frontend server."""
    # Create core components
    session_manager = get_session_manager()
    session_manager.load_state()

    frontend_registry = FrontendRegistry()

    # Register Telegram frontend if configured
    if config.telegram.bot_token and config.telegram.user_id:
        telegram = TelegramFrontend(config)
        telegram.set_session_manager(session_manager)
        frontend_registry.register('telegram', telegram)

    # Create HTTP app
    http_app = create_app(config, session_manager, frontend_registry)

    # Store telegram_frontend reference for reload notifications
    telegram_frontend = frontend_registry.get('telegram')
    if isinstance(telegram_frontend, TelegramFrontend):
        http_app['telegram_frontend'] = telegram_frontend
        telegram_frontend.set_http_app(http_app)

    # Start HTTP server
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, config.server.host, config.server.port)
    await site.start()

    logger.info(f'HTTP server listening on {config.server.host}:{config.server.port}')

    # Start all frontends
    await frontend_registry.start_all()

    # Wait for shutdown
    shutdown_event = _get_shutdown_event()
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info('Shutting down...')
        await frontend_registry.stop_all()
        await runner.cleanup()
        logger.info('Server stopped')
