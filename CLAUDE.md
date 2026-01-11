# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

```bash
# Always activate venv first
source .venv/bin/activate

# Install in editable mode
pip install -e .

# Lint and format
ruff check rclaude/
ruff check rclaude/ --fix
ruff format rclaude/

# Type check
ty check rclaude/

# Run the CLI
rclaude --help
rclaude setup      # Interactive setup wizard
rclaude serve      # Start HTTP + Telegram server
rclaude status     # Show configuration status
rclaude            # Run Claude Code with teleport support
rclaude -- -c      # Pass args to Claude (e.g., continue last session)
```

## Architecture

rclaude enables remote Claude Code control via Telegram with session teleportation between terminal and mobile.

### Package Structure

```
rclaude/
  cli.py, __main__.py     # CLI layer
  wrapper.py              # Claude CLI wrapper
  settings.py, config.py  # Configuration
  setup_wizard.py         # Interactive setup

  core/                   # Frontend-agnostic components
    events.py             # Event types (TextEvent, ToolCallEvent, etc.)
    session.py            # Session, SessionManager
    permissions.py        # Permission logic, smart rule generation
    claude_client.py      # Claude SDK wrapper

  server/                 # HTTP server
    app.py                # aiohttp routes and server

  frontends/              # Frontend implementations
    base.py               # Frontend protocol/ABC
    telegram/             # Telegram frontend
      frontend.py         # TelegramFrontend class
      formatting.py       # Telegram-specific formatting
      keyboards.py        # Inline keyboard builders
```

### Core Components

**CLI Layer** (`cli.py`, `__main__.py`)
- Click-based CLI with commands: `setup`, `serve`, `status`, `uninstall`
- Default action (no subcommand) runs Claude with teleport support
- Use `--` to pass arguments to Claude: `rclaude -- --resume <id>`

**Configuration** (`settings.py`, `config.py`)
- `settings.py`: TOML-based config at `~/.config/rclaude/config.toml`
- `config.py`: Backward-compat layer that loads TOML or falls back to `.env`

**Core** (`core/`)
- `session.py`: Frontend-agnostic Session with event queue, SessionManager with frontend mapping
- `events.py`: Typed event classes for the message bus between core and frontends
- `permissions.py`: Permission checking, rule management, smart Bash rule generation via Haiku
- `claude_client.py`: SDK wrapper, permission handler factory, response processing

**Server** (`server/app.py`)
- aiohttp HTTP server with routes: `/teleport`, `/health`, `/stream`, `/api/setup-link`
- SSE endpoint streams session events to terminal

**Frontends** (`frontends/`)
- `base.py`: Frontend protocol defining `send_text`, `request_permission`, etc.
- `telegram/`: TelegramFrontend with handlers for `/start`, `/new`, `/cc`, `/mode`, `/model`, `/status`, `/stop`, `/cancel`

**Claude Wrapper** (`wrapper.py`)
- Spawns `claude` CLI via pexpect with PTY
- Auto-starts server in background if not running

### Teleportation Flow

1. User runs `rclaude` (wrapper starts server if needed, launches Claude)
2. In Claude, user runs `/tg` slash command
3. Hook POSTs `{session_id, cwd}` to `localhost:7680/teleport`
4. Server notifies user on Telegram
5. User continues session on mobile via Claude Agent SDK with `resume=session_id`
6. `/cc` in Telegram emits `ReturnToTerminalEvent`, shows resume command

## Code Style

- Python 3.14+
- Single quotes for strings (ruff format)
- Line length 140
- Use `ty` for type checking (not mypy)
- Type narrow with `assert` for Telegram's Optional types
