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


def start_server_background(config: Config) -> subprocess.Popen:
    """Start the glaude server in the background."""
    # Find the glaude executable
    import shutil

    glaude_path = shutil.which('glaude')
    if glaude_path:
        cmd = [glaude_path, 'serve']
    else:
        cmd = [sys.executable, '-m', 'glaude', 'serve']

    # Start in background
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for server to be ready
    for _ in range(50):  # 5 seconds timeout
        if is_server_running(config):
            return proc
        time.sleep(0.1)

    raise RuntimeError('Failed to start glaude server')


def tail_session_log(session_id: str, cwd: str) -> None:
    """Tail the session log file to show updates."""
    # Find the session log file
    project_path = cwd.replace('/', '-').replace(':', '')
    if project_path.startswith('-'):
        project_path = project_path[1:]

    log_dir = Path.home() / '.claude' / 'projects' / f'-{project_path}'
    log_file = log_dir / f'{session_id}.jsonl'

    if not log_file.exists():
        print(f'Session log not found: {log_file}')
        return

    print('\n📱 Session on Telegram. Showing live updates...')
    print(f'   Log: {log_file}')
    print('   Press Ctrl+C to exit.\n')
    print('─' * 60)

    # Tail the file
    try:
        with subprocess.Popen(
            ['tail', '-f', str(log_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as proc:
            assert proc.stdout
            for line in proc.stdout:
                # Parse JSONL and show relevant parts
                try:
                    import json

                    data = json.loads(line)
                    msg_type = data.get('type')

                    if msg_type == 'assistant':
                        content = data.get('message', {}).get('content', [])
                        for block in content:
                            if block.get('type') == 'text':
                                print(f'Claude: {block.get("text", "")[:200]}')
                            elif block.get('type') == 'tool_use':
                                print(f'  → {block.get("name")}')
                    elif msg_type == 'user':
                        content = data.get('message', {}).get('content', '')
                        if isinstance(content, str):
                            print(f'You (TG): {content[:100]}')
                except json.JSONDecodeError:
                    pass
    except KeyboardInterrupt:
        print('\n\nExiting tail mode.')


def run_claude_wrapper(config: Config, args: list[str]) -> int:
    """Run Claude Code with teleportation support.

    This spawns Claude as a subprocess and monitors for teleport signals.
    """
    # Ensure server is running
    if not is_server_running(config):
        print('Starting glaude server...')
        try:
            start_server_background(config)
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
            tail_session_log(session_id, cwd)
            return 0

        return child.exitstatus or 0

    except pexpect.exceptions.ExceptionPexpect as e:
        print(f'Error running Claude: {e}')
        return 1
    finally:
        signal_file.unlink(missing_ok=True)


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
