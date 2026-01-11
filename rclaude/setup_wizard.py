"""Interactive setup wizard for rclaude."""

import asyncio
import json
import secrets
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import click

from rclaude.settings import (
    Config,
    save_config,
    load_config,
    HOOK_DIR,
    CONFIG_FILE,
    CLAUDE_SETTINGS_FILE,
)


# Default server address
DEFAULT_SERVER_HOST = '127.0.0.1'
DEFAULT_SERVER_PORT = 7680


def is_server_running(host: str = DEFAULT_SERVER_HOST, port: int = DEFAULT_SERVER_PORT) -> bool:
    """Check if the rclaude server is running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex((host, port))
    sock.close()
    return result == 0


def link_via_http(token: str, host: str = DEFAULT_SERVER_HOST, port: int = DEFAULT_SERVER_PORT) -> tuple[int, str] | None:
    """Link via running server's HTTP API."""
    base_url = f'http://{host}:{port}'

    # Register the token
    try:
        req = urllib.request.Request(
            f'{base_url}/api/setup-link',
            data=json.dumps({'token': token}).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
            if not data.get('ok'):
                return None
    except Exception as e:
        click.echo(f'\n  Error registering token: {e}')
        return None

    # Wait for link completion (long-polling)
    try:
        with urllib.request.urlopen(f'{base_url}/api/setup-link/{token}', timeout=310) as response:
            data = json.loads(response.read())
            if data.get('ok'):
                return (data['user_id'], data['username'])
            return None
    except urllib.error.HTTPError as e:
        if e.code == 408:
            return None  # Timeout
        click.echo(f'\n  Error waiting for link: {e}')
        return None
    except Exception as e:
        click.echo(f'\n  Error waiting for link: {e}')
        return None


# Pending link tokens: token -> asyncio.Event (only used when server not running)
_pending_links: dict[str, asyncio.Event] = {}
_link_results: dict[str, tuple[int, str]] = {}


def generate_link_token() -> str:
    """Generate a short token for linking."""
    return secrets.token_hex(4).upper()


async def wait_for_link(token: str, timeout: float = 300) -> tuple[int, str] | None:
    """Wait for a link token to be claimed."""
    event = asyncio.Event()
    _pending_links[token] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return _link_results.get(token)
    except asyncio.TimeoutError:
        return None
    finally:
        _pending_links.pop(token, None)
        _link_results.pop(token, None)


def complete_link(token: str, user_id: int, username: str) -> bool:
    """Complete a pending link (called from bot handler)."""
    if token not in _pending_links:
        return False

    _link_results[token] = (user_id, username)
    _pending_links[token].set()
    return True


def install_hook() -> bool:
    """Install the /tg hook for Claude Code."""
    import json
    import shutil

    # Install UserPromptSubmit hook in settings.json
    settings: dict = {}
    if CLAUDE_SETTINGS_FILE.exists():
        with open(CLAUDE_SETTINGS_FILE) as f:
            settings = json.load(f)

    # Find rclaude executable path
    rclaude_path = shutil.which('rclaude')
    if not rclaude_path:
        # Fallback to python -m rclaude
        rclaude_path = f'{sys.executable} -m rclaude'

    # Define our hook
    tg_hook = {
        'matcher': '^/tg$',
        'hooks': [
            {
                'type': 'command',
                'command': f'{rclaude_path} teleport-hook',
            }
        ],
    }

    # Merge into settings
    if 'hooks' not in settings:
        settings['hooks'] = {}
    if 'UserPromptSubmit' not in settings['hooks']:
        settings['hooks']['UserPromptSubmit'] = []

    # Remove any existing /tg hook and add fresh one
    existing_hooks = settings['hooks']['UserPromptSubmit']
    settings['hooks']['UserPromptSubmit'] = [h for h in existing_hooks if h.get('matcher') != '^/tg$']
    settings['hooks']['UserPromptSubmit'].append(tg_hook)

    # Write settings.json
    CLAUDE_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CLAUDE_SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

    # Also install the /tg slash command (provides user feedback)
    HOOK_DIR.mkdir(parents=True, exist_ok=True)

    hook_content = """\
---
description: Teleport session to Telegram
---

Session teleported to Telegram. You can continue on your phone.

The terminal will switch to showing live updates. Press Ctrl+C to exit.
"""

    hook_file = HOOK_DIR / 'tg.md'
    hook_file.write_text(hook_content)
    return True


def run_setup() -> None:
    """Run the interactive setup wizard."""
    click.echo()
    click.echo('┌' + '─' * 50 + '┐')
    click.echo('│' + '           rclaude Setup Wizard'.center(50) + '│')
    click.echo('└' + '─' * 50 + '┘')
    click.echo()

    # Check if already configured
    existing = load_config()
    if existing.is_configured():
        if not click.confirm('rclaude is already configured. Reconfigure?'):
            return

    config = Config()

    # Step 1: Bot token
    click.echo('Step 1: Create your Telegram bot')
    click.echo('─' * 40)
    click.echo('  1. Open Telegram and message @BotFather')
    click.echo('  2. Send /newbot and follow the prompts')
    click.echo('  3. Copy the bot token')
    click.echo()

    while True:
        token = click.prompt('  Bot token', hide_input=False)
        token = token.strip()

        if ':' in token and len(token) > 30:
            # Validate token by making a test request
            if validate_bot_token(token):
                config.telegram.bot_token = token
                click.echo('  ✓ Token validated')
                break
            else:
                click.echo('  ✗ Invalid token. Please check and try again.')
        else:
            click.echo('  ✗ Token format looks wrong. Should be like: 123456:ABC-DEF...')

    click.echo()

    # Step 2: Link Telegram account
    click.echo('Step 2: Link your Telegram account')
    click.echo('─' * 40)

    link_token = generate_link_token()

    click.echo(f'  Message your bot with: /link {link_token}')
    click.echo()

    # Check if server is already running
    server_running = is_server_running()
    if server_running:
        click.echo('  (Using running rclaude server)')

    click.echo('  Waiting for link...', nl=False)

    # Use HTTP API if server is running, otherwise start temporary bot
    if server_running:
        result = link_via_http(link_token)
    else:
        result = asyncio.run(run_link_bot(config.telegram.bot_token, link_token))

    if result:
        user_id, username = result
        config.telegram.user_id = user_id
        config.telegram.username = username
        click.echo(' ✓')
        click.echo(f'  Linked: @{username} ({user_id})')
    else:
        click.echo(' ✗ Timeout')
        click.echo('  Setup cancelled.')
        sys.exit(1)

    click.echo()

    # Step 3: Install hook
    click.echo('Step 3: Install Claude Code hook')
    click.echo('─' * 40)

    if install_hook():
        config.claude.hook_installed = True
        click.echo(f'  ✓ Installed /tg command to {HOOK_DIR}/tg.md')
    else:
        click.echo('  ✗ Failed to install hook')

    click.echo()

    # Step 4: Auto-start (optional)
    click.echo('Step 4: Auto-start server (optional)')
    click.echo('─' * 40)

    if sys.platform == 'darwin':
        if click.confirm('  Start rclaude server on login?', default=False):
            if install_launchd():
                click.echo('  ✓ LaunchAgent installed')
            else:
                click.echo('  ✗ Failed to install LaunchAgent')
        else:
            click.echo('  Skipped. Run manually with: rclaude serve')
    else:
        click.echo('  Auto-start not yet supported on this platform.')
        click.echo('  Run manually with: rclaude serve')

    click.echo()

    # Save config
    save_config(config)
    click.echo(f'Config saved to: {CONFIG_FILE}')

    click.echo()
    click.echo('┌' + '─' * 50 + '┐')
    click.echo('│' + '           Setup Complete!'.center(50) + '│')
    click.echo('│' + ''.center(50) + '│')
    click.echo('│' + '  • Run: rclaude serve'.ljust(50) + '│')
    click.echo('│' + '  • Then: rclaude (to start Claude)'.ljust(50) + '│')
    click.echo('│' + '  • Use /tg to teleport to Telegram'.ljust(50) + '│')
    click.echo('└' + '─' * 50 + '┘')
    click.echo()


def validate_bot_token(token: str) -> bool:
    """Validate a Telegram bot token."""
    import urllib.request
    import json

    try:
        url = f'https://api.telegram.org/bot{token}/getMe'
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
            return data.get('ok', False)
    except Exception:
        return False


async def run_link_bot(token: str, expected_token: str) -> tuple[int, str] | None:
    """Run a temporary bot to receive the link command."""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    result: tuple[int, str] | None = None
    stop_event = asyncio.Event()

    async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        nonlocal result

        assert update.effective_user
        assert update.message

        args = context.args or []
        if not args:
            await update.message.reply_text('Usage: /link <token>')
            return

        provided_token = args[0].upper()
        if provided_token != expected_token:
            await update.message.reply_text('Invalid token. Please check and try again.')
            return

        user_id = update.effective_user.id
        username = update.effective_user.username or str(user_id)

        result = (user_id, username)
        await update.message.reply_text(f'✓ Linked! You can close this chat.\n\nUser ID: {user_id}')
        stop_event.set()

    async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message
        await update.message.reply_text('rclaude Setup\n\nUse /link <token> to complete setup.\nThe token was shown in your terminal.')

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler('link', handle_link))
    app.add_handler(CommandHandler('start', handle_start))

    async with app:
        await app.start()
        assert app.updater is not None
        await app.updater.start_polling()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass

        await app.updater.stop()
        await app.stop()

    return result


def install_launchd() -> bool:
    """Install a macOS LaunchAgent for auto-start."""
    import shutil

    # Find rclaude executable
    rclaude_path = shutil.which('rclaude')
    if not rclaude_path:
        # Fallback to python -m rclaude
        python_path = sys.executable
        rclaude_path = f'{python_path} -m rclaude'

    plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rclaude.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>{rclaude_path}</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/rclaude.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/rclaude.err</string>
</dict>
</plist>
"""

    launch_agents = Path.home() / 'Library' / 'LaunchAgents'
    launch_agents.mkdir(parents=True, exist_ok=True)

    plist_file = launch_agents / 'com.rclaude.server.plist'
    plist_file.write_text(plist_content)

    return True
