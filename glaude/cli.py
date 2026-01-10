"""CLI commands for glaude."""

import asyncio
import sys

import click

from glaude import __version__
from glaude.settings import load_config, CONFIG_FILE


@click.group(invoke_without_command=True)
@click.option('--version', '-V', is_flag=True, help='Show version')
@click.option('--reload', '-r', is_flag=True, help='Start server in reload mode (dev)')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.pass_context
def main(ctx: click.Context, version: bool, reload: bool, verbose: bool) -> None:
    """Glaude - Remote Claude Code control via Telegram.

    Run without arguments to start Claude Code with teleportation support.
    """
    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    if version:
        click.echo(f'glaude {__version__}')
        return

    if ctx.invoked_subcommand is None:
        # Default action: run the wrapper
        ctx.invoke(run, reload=reload)


@main.command()
def setup() -> None:
    """Interactive setup wizard."""
    from glaude.setup_wizard import run_setup

    run_setup()


@main.command()
@click.option('--foreground', '-f', is_flag=True, help="Run in foreground (don't daemonize)")
@click.option('--reload', '-r', is_flag=True, help='Auto-reload on code changes (dev mode)')
def serve(foreground: bool, reload: bool) -> None:
    """Start the glaude server (HTTP + Telegram bot)."""
    config = load_config()

    if not config.is_configured():
        click.echo('Glaude is not configured. Run: glaude setup')
        sys.exit(1)

    if reload:
        _serve_with_reload(config)
    else:
        from glaude.server import run_server

        click.echo(f'Starting glaude server on {config.server.host}:{config.server.port}...')
        asyncio.run(run_server(config))


def _run_server_subprocess():
    """Target function for watchfiles - must be at module level for pickling."""
    import subprocess

    cmd = [sys.executable, '-m', 'glaude', 'serve']
    subprocess.run(cmd)


def _serve_with_reload(config) -> None:
    """Run server with hot-reload on code changes."""
    from pathlib import Path

    import watchfiles

    # Find the glaude package directory
    glaude_dir = Path(__file__).parent

    click.echo(f'Starting glaude server on {config.server.host}:{config.server.port} (reload mode)...')
    click.echo(f'Watching: {glaude_dir}')

    # Watch Python files in the glaude directory
    watchfiles.run_process(
        glaude_dir,
        target=_run_server_subprocess,
        watch_filter=watchfiles.PythonFilter(),
        callback=lambda changes: click.echo(f'Reloading... (changed: {[str(c[1]) for c in changes]})'),
    )


@main.command()
@click.option('--reload', '-r', is_flag=True, help='Start server in reload mode (dev)')
@click.argument('args', nargs=-1)
def run(reload: bool, args: tuple[str, ...]) -> None:
    """Run Claude Code with teleportation support.

    Any arguments are passed through to Claude.
    """
    config = load_config()

    if not config.is_configured():
        click.echo('Glaude is not configured. Run: glaude setup')
        sys.exit(1)

    from glaude.wrapper import run_claude_wrapper

    sys.exit(run_claude_wrapper(config, list(args), reload=reload))


@main.command()
def status() -> None:
    """Show glaude status."""
    config = load_config()

    click.echo('Glaude Status')
    click.echo('─' * 40)

    if not CONFIG_FILE.exists():
        click.echo('Config: Not configured')
        click.echo('\nRun: glaude setup')
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

    from glaude.wrapper import is_server_running, start_server_background

    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        click.echo('Error: Invalid JSON on stdin', err=True)
        sys.exit(1)

    session_id = hook_input.get('session_id')
    cwd = hook_input.get('cwd', '.')

    if not session_id:
        click.echo('Error: No session_id in hook input', err=True)
        sys.exit(1)

    config = load_config()

    # Auto-start server if not running
    if not is_server_running(config):
        try:
            start_server_background(config)
        except RuntimeError as e:
            click.echo(f'Error: Failed to start server - {e}', err=True)
            sys.exit(1)

    # POST to teleport endpoint
    url = f'http://{config.server.host}:{config.server.port}/teleport'
    data = json.dumps({'session_id': session_id, 'cwd': cwd}).encode()

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
    wrapper_pid = os.environ.get('GLAUDE_WRAPPER_PID')
    if wrapper_pid:
        try:
            pid = int(wrapper_pid)
            # Write signal file with session info
            signal_file = Path(f'/tmp/glaude-{pid}.signal')
            signal_file.write_text(json.dumps({'session_id': session_id, 'cwd': cwd}))
            # Send signal to wrapper
            os.kill(pid, signal.SIGUSR1)
        except (ValueError, OSError, ProcessLookupError):
            # Wrapper not running or invalid PID - that's okay, server still notified
            pass


@main.command()
def uninstall() -> None:
    """Remove glaude configuration and hooks."""
    import json

    from glaude.settings import CONFIG_DIR, HOOK_DIR, CLAUDE_SETTINGS_FILE

    click.confirm('This will remove all glaude configuration. Continue?', abort=True)

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

    click.echo('Glaude uninstalled.')
