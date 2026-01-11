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

from rclaude.settings import Config


def get_signal_file() -> Path:
    """Get the signal file path for this wrapper instance."""
    return Path(f'/tmp/rclaude-{os.getpid()}.signal')


def is_server_running(config: Config) -> bool:
    """Check if the rclaude server is running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex((config.server.host, config.server.port))
    sock.close()
    return result == 0


def start_server_background(config: Config, reload: bool = False, verbose: bool = False) -> subprocess.Popen:
    """Start the rclaude server in the background."""
    # Find the rclaude executable
    import shutil

    rclaude_path = shutil.which('rclaude')
    if rclaude_path:
        cmd = [rclaude_path, 'serve']
    else:
        cmd = [sys.executable, '-m', 'rclaude', 'serve']

    if reload:
        cmd.append('--reload')
    if verbose:
        cmd.append('--verbose')

    # Start in background
    # In verbose/reload mode, keep stderr visible for debugging
    show_output = reload or verbose
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=None if show_output else subprocess.DEVNULL,
        start_new_session=True,
    )

    # Save watcher PID so server can kill it on shutdown (for reload mode)
    wrapper_pid = os.environ.get('RCLAUDE_WRAPPER_PID')
    if wrapper_pid and reload:
        pid_file = Path(f'/tmp/rclaude-watcher-{wrapper_pid}.pid')
        pid_file.write_text(str(proc.pid))

    # Wait for server to be ready (longer timeout for reload mode due to watchfiles startup)
    timeout = 100 if reload else 50  # 10s vs 5s
    for _ in range(timeout):
        if is_server_running(config):
            return proc
        time.sleep(0.1)

    raise RuntimeError('Failed to start rclaude server')


def stream_session_updates(config: Config, session_id: str) -> tuple[str | None, bool]:
    """Stream session updates from server via SSE.

    Returns (session_id, should_exit):
    - (session_id, False) if user ran /cc - resume in terminal
    - (None, True) if user pressed Ctrl+C - exit wrapper
    - (None, False) if connection error - might be restarting
    """
    import urllib.request
    import urllib.error

    print('\n📱 Session on Telegram. Showing live updates...')
    print(f'   Session: {session_id[:8]}...')
    print('   Press Ctrl+C to stop.\n')
    print('─' * 60)

    url = f'http://{config.server.host}:{config.server.port}/stream'

    while True:  # Reconnect loop
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as response:
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
                                return content, False  # Resume, don't stop server
                        except json.JSONDecodeError:
                            pass

        except urllib.error.URLError as e:
            # Connection dropped - server might be restarting (hot reload)
            print(f'Connection lost ({e.reason}), reconnecting...')
            time.sleep(2)  # Wait before reconnect
            continue
        except KeyboardInterrupt:
            print('\n\nStopping...')
            return None, True  # User wants to stop


def run_claude_wrapper(config: Config, args: list[str], reload: bool = False, verbose: bool = False) -> int:
    """Run Claude Code with teleportation support.

    This spawns Claude as a subprocess and monitors for teleport signals.
    Server is started on-demand by the /tg hook, not here.
    """
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

    # Set env vars so hook can find us and know our settings
    env = os.environ.copy()
    env['RCLAUDE_WRAPPER_PID'] = str(os.getpid())
    if reload:
        env['RCLAUDE_RELOAD'] = '1'
    if verbose:
        env['RCLAUDE_VERBOSE'] = '1'

    # Track state for teleport cycles
    teleport_data: dict | None = None
    current_cwd = os.getcwd()
    resume_proc: subprocess.Popen | None = None  # Track resumed subprocess for signal handler

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
            # Terminate claude (pexpect child or resumed subprocess)
            if resume_proc and resume_proc.poll() is None:
                resume_proc.terminate()
            else:
                child.terminate(force=False)

        signal.signal(signal.SIGWINCH, handle_resize)
        signal.signal(signal.SIGUSR1, handle_teleport)
        handle_resize(None, None)

        # Interactive mode - pass through I/O
        child.interact()

        # Teleport loop: supports terminal -> TG -> terminal -> TG -> ...
        while teleport_data:
            session_id = teleport_data.get('session_id', '')
            current_cwd = teleport_data.get('cwd', current_cwd)
            signal_file.unlink(missing_ok=True)
            teleport_data = None  # Reset for next cycle

            # Stream updates from server, handle return-to-terminal
            resume_session, should_stop = stream_session_updates(config, session_id)

            if should_stop:
                # User pressed Ctrl+C in tail mode
                return 0

            if resume_session:
                # User ran /cc - resume Claude with session using Popen
                print(f'\nResuming session {resume_session[:8]}...')
                print('─' * 60)
                resume_cmd = ['claude', '--resume', resume_session]
                resume_proc = subprocess.Popen(resume_cmd, cwd=current_cwd, env=env)
                exit_code = resume_proc.wait()
                print(f'[DEBUG] Resumed Claude exited with code {exit_code}')
                resume_proc = None
                # After resume exits, check if we teleported again (loop continues if teleport_data set)
            else:
                print('[DEBUG] No resume session, exiting loop')

        print(f'[DEBUG] Exiting wrapper, child.exitstatus={child.exitstatus}')
        return child.exitstatus or 0

    except pexpect.exceptions.ExceptionPexpect as e:
        print(f'Error running Claude: {e}')
        return 1
    finally:
        signal_file.unlink(missing_ok=True)
