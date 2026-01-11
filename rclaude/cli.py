"""CLI commands for rclaude."""

import asyncio
import sys

import click

from rclaude import __version__
from rclaude.settings import load_config, CONFIG_FILE


# Claude args extracted from sys.argv by __main__.run() before Click processes them
_claude_args: list[str] = []


def _run_wrapper(claude_args: list[str], reload: bool = False, verbose: bool = False) -> None:
    """Run Claude Code wrapper with given arguments."""
    import os

    # CLI flags override env vars
    reload = reload or os.environ.get('RCLAUDE_RELOAD') == '1'
    verbose = verbose or os.environ.get('RCLAUDE_VERBOSE') == '1'

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    config = load_config()

    if not config.is_configured():
        click.echo('rclaude is not configured. Run: rclaude setup')
        sys.exit(1)

    from rclaude.wrapper import run_claude_wrapper

    sys.exit(run_claude_wrapper(config, claude_args, reload=reload, verbose=verbose))


@click.group(invoke_without_command=True)
@click.option('--version', '-V', is_flag=True, help='Show version')
@click.option('--reload', '-r', is_flag=True, help='Start server in reload mode (dev)')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.pass_context
def main(ctx: click.Context, version: bool, reload: bool, verbose: bool) -> None:
    """rclaude - Remote Claude Code control via Telegram.

    Run without arguments to start Claude Code with teleportation support.

    \b
    Pass arguments to Claude after --:
      rclaude -- --resume <id>
      rclaude -- -c
      rclaude -- -p "query"

    \b
    Dev options (flags or environment):
      -r, --reload / RCLAUDE_RELOAD=1   Hot-reload server on code changes
      -v, --verbose / RCLAUDE_VERBOSE=1 Enable debug logging
    """
    if version:
        click.echo(f'rclaude {__version__}')
        return

    if ctx.invoked_subcommand is None:
        # Default action: run claude with extra args (after --)
        _run_wrapper(_claude_args, reload=reload, verbose=verbose)


@main.command()
def setup() -> None:
    """Interactive setup wizard."""
    from rclaude.setup_wizard import run_setup

    run_setup()


@main.command()
@click.option('--reload', '-r', is_flag=True, help='Auto-reload on code changes (dev mode)')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def serve(reload: bool, verbose: bool) -> None:
    """Start the rclaude server (HTTP + Telegram bot)."""
    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    config = load_config()

    if not config.is_configured():
        click.echo('rclaude is not configured. Run: rclaude setup')
        sys.exit(1)

    if reload:
        _serve_with_reload(config, verbose=verbose)
    else:
        from rclaude.server import run_server

        click.echo(f'Starting rclaude server on {config.server.host}:{config.server.port}...')
        asyncio.run(run_server(config))


def _serve_with_reload(config, verbose: bool = False) -> None:
    """Run server with hot-reload on code changes.

    Uses deferred reload - waits for agent to finish processing before restarting.
    """
    import json
    import subprocess
    import time
    import urllib.request
    from pathlib import Path

    import watchfiles

    rclaude_dir = Path(__file__).parent
    base_url = f'http://{config.server.host}:{config.server.port}'

    click.echo(f'Starting rclaude server on {config.server.host}:{config.server.port} (reload mode)...')
    click.echo(f'Watching: {rclaude_dir}')

    proc = None

    def start_server():
        nonlocal proc
        cmd = [sys.executable, '-m', 'rclaude', 'serve']
        if verbose:
            cmd.append('--verbose')
        proc = subprocess.Popen(cmd)
        return proc

    def api_call(endpoint: str, method: str = 'GET') -> dict | None:
        """Make API call to server, return JSON or None on error."""
        try:
            req = urllib.request.Request(f'{base_url}{endpoint}', method=method)
            with urllib.request.urlopen(req, timeout=2) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def wait_for_idle() -> bool:
        """Wait until all sessions are idle or force reload requested."""
        while True:
            status = api_call('/api/can-reload')
            if status is None:
                return True  # Server down, can reload

            if status.get('can_reload') or status.get('force_reload'):
                return True

            processing = status.get('processing', 0)
            click.echo(f'  Waiting for {processing} session(s) to finish...')
            time.sleep(0.5)

    def stop_server_gracefully():
        """Stop server with graceful shutdown to save session state."""
        nonlocal proc
        if proc is None:
            return

        # Save state before shutdown
        api_call('/api/prepare-reload', method='POST')

        # Send SIGTERM for graceful shutdown
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc = None

    # Start initial server
    start_server()

    try:
        for changes in watchfiles.watch(
            rclaude_dir,
            watch_filter=watchfiles.PythonFilter(),
            debounce=1500,
            step=300,
        ):
            changed_files = [str(c[1]) for c in changes]
            click.echo(f'Changes detected: {changed_files}')

            # Notify user that reload is pending
            result = api_call('/api/request-reload', method='POST')
            if result and result.get('waiting'):
                click.echo('Reload pending - waiting for agent to finish...')
                wait_for_idle()

            click.echo('Reloading...')
            stop_server_gracefully()
            start_server()
    except KeyboardInterrupt:
        click.echo('\nStopping...')
        stop_server_gracefully()


@main.command()
def status() -> None:
    """Show rclaude status."""
    config = load_config()

    click.echo('rclaude Status')
    click.echo('─' * 40)

    if not CONFIG_FILE.exists():
        click.echo('Config: Not configured')
        click.echo('\nRun: rclaude setup')
        return

    click.echo(f'Config: {CONFIG_FILE}')

    if config.telegram.bot_token:
        masked_token = config.telegram.bot_token[:10] + '...'
        click.echo(f'Bot token: {masked_token}')
    else:
        click.echo('Bot token: Not set')

    if config.telegram.user_id:
        click.echo(f'Telegram user: @{config.telegram.username} ({config.telegram.user_id})')
    else:
        click.echo('Telegram user: Not linked')

    click.echo(f'Server: {config.server.host}:{config.server.port}')
    click.echo(f'Hook installed: {"Yes" if config.claude.hook_installed else "No"}')

    # Check if server is running
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex((config.server.host, config.server.port))
    sock.close()

    if result == 0:
        click.echo('Server status: Running')
    else:
        click.echo('Server status: Not running')


@main.command('teleport-hook')
def teleport_hook() -> None:
    """Handle /tg hook - reads session info from stdin and POSTs to server.

    This is called by Claude Code's UserPromptSubmit hook when user runs /tg.
    """
    import json
    import os
    import signal
    import urllib.request
    import urllib.error
    from pathlib import Path

    from rclaude.wrapper import is_server_running, start_server_background

    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        click.echo('Error: Invalid JSON on stdin', err=True)
        sys.exit(1)

    session_id = hook_input.get('session_id')
    cwd = hook_input.get('cwd', '.')
    prompt = hook_input.get('prompt', '')
    permission_mode = hook_input.get('permission_mode', 'default')

    if not session_id:
        click.echo('Error: No session_id in hook input', err=True)
        sys.exit(1)

    # Only teleport if the prompt is exactly "/tg"
    # The matcher in settings.json should handle this, but we double-check here
    if prompt.strip() != '/tg':
        sys.exit(0)

    # Only teleport if called from the rclaude wrapper (terminal mode)
    # The SDK also triggers this hook but we don't want to teleport from TG -> TG
    wrapper_pid = os.environ.get('RCLAUDE_WRAPPER_PID')
    terminal_id = os.environ.get('RCLAUDE_TERMINAL_ID')
    if not wrapper_pid or not terminal_id:
        # Not running under wrapper - likely SDK triggering the hook
        sys.exit(0)

    config = load_config()

    # Auto-start server if not running
    if not is_server_running(config):
        try:
            reload_mode = os.environ.get('RCLAUDE_RELOAD') == '1'
            verbose_mode = os.environ.get('RCLAUDE_VERBOSE') == '1'
            start_server_background(config, reload=reload_mode, verbose=verbose_mode)
        except RuntimeError as e:
            click.echo(f'Error: Failed to start server - {e}', err=True)
            sys.exit(1)

    # POST to teleport endpoint
    url = f'http://{config.server.host}:{config.server.port}/teleport'
    data = json.dumps(
        {
            'session_id': session_id,
            'cwd': cwd,
            'permission_mode': permission_mode,
            'terminal_id': terminal_id,
        }
    ).encode()

    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if not result.get('ok'):
                click.echo(f'Error: {result.get("error", "Unknown error")}', err=True)
                sys.exit(1)
    except urllib.error.URLError as e:
        click.echo(f'Error: Could not connect to server - {e.reason}', err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f'Error: {e}', err=True)
        sys.exit(1)

    # Signal the wrapper to switch to tail mode
    wrapper_pid = os.environ.get('RCLAUDE_WRAPPER_PID')
    if wrapper_pid:
        try:
            pid = int(wrapper_pid)
            # Write signal file with session info
            signal_file = Path(f'/tmp/rclaude-{pid}.signal')
            signal_file.write_text(json.dumps({'session_id': session_id, 'cwd': cwd}))
            # Send signal to wrapper
            os.kill(pid, signal.SIGUSR1)
        except (ValueError, OSError, ProcessLookupError):
            # Wrapper not running or invalid PID - that's okay, server still notified
            pass


@main.command()
def uninstall() -> None:
    """Remove rclaude configuration and hooks."""
    import json

    from rclaude.settings import CONFIG_DIR, HOOK_DIR, CLAUDE_SETTINGS_FILE

    click.confirm('This will remove all rclaude configuration. Continue?', abort=True)

    # Remove slash command
    hook_file = HOOK_DIR / 'tg.md'
    if hook_file.exists():
        hook_file.unlink()
        click.echo(f'Removed: {hook_file}')

    # Remove hook from settings.json
    if CLAUDE_SETTINGS_FILE.exists():
        try:
            with open(CLAUDE_SETTINGS_FILE) as f:
                settings = json.load(f)

            if 'hooks' in settings and 'UserPromptSubmit' in settings['hooks']:
                settings['hooks']['UserPromptSubmit'] = [h for h in settings['hooks']['UserPromptSubmit'] if h.get('matcher') != '^/tg$']
                # Clean up empty structures
                if not settings['hooks']['UserPromptSubmit']:
                    del settings['hooks']['UserPromptSubmit']
                if not settings['hooks']:
                    del settings['hooks']

                with open(CLAUDE_SETTINGS_FILE, 'w') as f:
                    json.dump(settings, f, indent=2)
                click.echo(f'Removed /tg hook from: {CLAUDE_SETTINGS_FILE}')
        except (json.JSONDecodeError, OSError) as e:
            click.echo(f'Warning: Could not update {CLAUDE_SETTINGS_FILE}: {e}')

    # Remove config
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        click.echo(f'Removed: {CONFIG_FILE}')

    if CONFIG_DIR.exists():
        try:
            CONFIG_DIR.rmdir()
            click.echo(f'Removed: {CONFIG_DIR}')
        except OSError:
            click.echo(f'Note: {CONFIG_DIR} not empty, left in place')

    click.echo('rclaude uninstalled.')
