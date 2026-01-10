"""Claude Code wrapper with teleportation support.

Spawns Claude Code as a subprocess and handles /tg teleportation.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pexpect

from glaude.settings import Config


def get_signal_file() -> Path:
    """Get the signal file path for this wrapper instance."""
    return Path(f'/tmp/glaude-{os.getpid()}.signal')


def is_server_running(config: Config) -> bool:
    """Check if the glaude server is running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex((config.server.host, config.server.port))
    sock.close()
    return result == 0


def start_server_background(config: Config, reload: bool = False) -> subprocess.Popen:
    """Start the glaude server in the background."""
    # Find the glaude executable
    import shutil

    glaude_path = shutil.which('glaude')
    if glaude_path:
        cmd = [glaude_path, 'serve']
    else:
        cmd = [sys.executable, '-m', 'glaude', 'serve']

    if reload:
        cmd.append('--reload')

    # Start in background
    # In reload mode, keep stderr visible for debugging
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=None if reload else subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for server to be ready (longer timeout for reload mode due to watchfiles startup)
    timeout = 100 if reload else 50  # 10s vs 5s
    for _ in range(timeout):
        if is_server_running(config):
            return proc
        time.sleep(0.1)

    raise RuntimeError('Failed to start glaude server')


def stream_session_updates(config: Config, session_id: str) -> str | None:
    """Stream session updates from server via SSE.

    Returns session_id if we should resume in terminal, None otherwise.
    """
    import urllib.request
    import urllib.error

    print('\n📱 Session on Telegram. Showing live updates...')
    print(f'   Session: {session_id[:8]}...')
    print('   Press Ctrl+C to return to terminal.\n')
    print('─' * 60)

    url = f'http://{config.server.host}:{config.server.port}/stream'

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            for line in response:
                line = line.decode('utf-8').strip()

                if line.startswith('event:'):
                    # Skip event type lines, we handle data directly
                    continue

                if line.startswith('data:'):
                    data_str = line[5:].strip()
                    if not data_str or data_str == '{}':
                        continue

                    try:
                        data = json.loads(data_str)
                        update_type = data.get('type', '')
                        content = data.get('content', '')

                        if update_type == 'user':
                            print(f'You (TG): {content[:100]}')
                        elif update_type == 'text':
                            # Truncate long text
                            display = content[:200] + '...' if len(content) > 200 else content
                            print(f'Claude: {display}')
                        elif update_type == 'tool_call':
                            print(f'  → {content}')
                        elif update_type == 'question':
                            print(f'  ? {content}')
                        elif update_type == 'return_to_terminal':
                            print('\n💻 Returning to terminal...')
                            return content  # Return session_id to resume
                    except json.JSONDecodeError:
                        pass

    except urllib.error.URLError as e:
        print(f'Connection error: {e.reason}')
    except KeyboardInterrupt:
        print('\n\nExiting stream mode.')

    return None


def _stop_server(proc: subprocess.Popen) -> None:
    """Stop the server process gracefully."""
    print('\nStopping glaude server...')
    try:
        # First try graceful termination
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Force kill if still running
            proc.kill()
            proc.wait()
        print('✓ Server stopped')
    except Exception as e:
        print(f'Warning: Could not stop server: {e}')


def run_claude_wrapper(config: Config, args: list[str], reload: bool = False) -> int:
    """Run Claude Code with teleportation support.

    This spawns Claude as a subprocess and monitors for teleport signals.
    """
    # Track server process if we start it
    server_proc: subprocess.Popen | None = None

    # Ensure server is running
    if not is_server_running(config):
        msg = 'Starting glaude server'
        if reload:
            msg += ' (reload mode)'
        print(f'{msg}...')
        try:
            server_proc = start_server_background(config, reload=reload)
            print('✓ Server started')
        except RuntimeError as e:
            print(f'Warning: {e}')

    signal_file = get_signal_file()

    # Clear any old signal file
    if signal_file.exists():
        signal_file.unlink()

    # Build claude command
    claude_cmd = 'claude'
    if args:
        claude_cmd += ' ' + ' '.join(args)

    print('Starting Claude Code... (use /tg to teleport to Telegram)')
    print('─' * 60)

    # Set env var so hook can find us
    env = os.environ.copy()
    env['GLAUDE_WRAPPER_PID'] = str(os.getpid())

    # Track teleport state
    teleport_data: dict | None = None

    # Use pexpect to spawn claude with PTY
    try:
        child = pexpect.spawn(claude_cmd, encoding='utf-8', timeout=None, env=env)
        child.setwinsize(24, 80)

        # Handle window resize
        def handle_resize(signum, frame):
            try:
                import struct
                import fcntl
                import termios

                s = struct.pack('HHHH', 0, 0, 0, 0)
                result = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, s)
                rows, cols, _, _ = struct.unpack('HHHH', result)
                child.setwinsize(rows, cols)
            except Exception:
                pass

        # Handle teleport signal
        def handle_teleport(signum, frame):
            nonlocal teleport_data
            if signal_file.exists():
                try:
                    teleport_data = json.loads(signal_file.read_text())
                except Exception:
                    pass
            # Terminate claude to exit interact()
            child.terminate(force=False)

        signal.signal(signal.SIGWINCH, handle_resize)
        signal.signal(signal.SIGUSR1, handle_teleport)
        handle_resize(None, None)

        # Interactive mode - pass through I/O
        child.interact()

        # Check if we teleported
        if teleport_data:
            session_id = teleport_data.get('session_id', '')
            cwd = teleport_data.get('cwd', '.')
            signal_file.unlink(missing_ok=True)

            # Stream updates from server, handle return-to-terminal
            resume_session = stream_session_updates(config, session_id)

            if resume_session:
                # User ran /cc - resume Claude with session
                print(f'\nResuming session {resume_session[:8]}...')
                print('─' * 60)
                resume_cmd = ['claude', '--resume', resume_session]
                subprocess.run(resume_cmd, cwd=cwd)

            # Clean up server if we started it
            if server_proc:
                _stop_server(server_proc)
            return 0

        return child.exitstatus or 0

    except pexpect.exceptions.ExceptionPexpect as e:
        print(f'Error running Claude: {e}')
        return 1
    finally:
        signal_file.unlink(missing_ok=True)
        # Note: We don't stop server here on normal exit
        # Only stop when exiting tail mode (teleport completed)


def run_claude_simple(config: Config, args: list[str]) -> int:
    """Simple wrapper that just runs claude (fallback)."""
    # Ensure server is running
    if not is_server_running(config):
        print('Starting glaude server...', end=' ', flush=True)
        try:
            start_server_background(config)
            print('✓')
        except RuntimeError as e:
            print(f'Warning: {e}')

    # Just run claude directly
    cmd = ['claude'] + args

    try:
        result = subprocess.run(cmd)
        return result.returncode
    except FileNotFoundError:
        print('Error: claude command not found. Is Claude Code installed?')
        return 1
    except KeyboardInterrupt:
        return 130
