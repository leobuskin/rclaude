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

### Core Components

**CLI Layer** (`cli.py`, `__main__.py`)
- Click-based CLI with commands: `setup`, `serve`, `status`, `uninstall`
- Default action (no subcommand) runs Claude with teleport support
- Use `--` to pass arguments to Claude: `rclaude -- --resume <id>`
- Entry point at `rclaude.__main__:main`

**Configuration** (`settings.py`, `config.py`)
- `settings.py`: New TOML-based config at `~/.config/rclaude/config.toml`
- `config.py`: Backward-compat layer that loads TOML or falls back to `.env`

**Server** (`server.py`)
- Combined aiohttp HTTP server + python-telegram-bot in single async loop
- HTTP endpoint `POST /teleport` receives session handoffs from `/tg` hook
- Telegram handlers for `/start`, `/new`, `/cc`, `/status`, `/stop`, `/cancel`

**Claude Wrapper** (`wrapper.py`)
- Spawns `claude` CLI via pexpect with PTY
- Auto-starts server in background if not running
- Handles terminal resize signals

**Setup Wizard** (`setup_wizard.py`)
- Interactive setup: bot token validation, Telegram account linking via `/link` command
- Installs `/tg` hook to `~/.claude/commands/tg.md`
- Optional macOS LaunchAgent for auto-start

### Teleportation Flow

1. User runs `rclaude` (wrapper starts server if needed, launches Claude)
2. In Claude, user runs `/tg` slash command
3. Hook POSTs `{session_id, cwd}` to `localhost:7680/teleport`
4. Server notifies user on Telegram
5. User continues session on mobile via Claude Agent SDK with `resume=session_id`
6. `/cc` in Telegram shows command to resume in terminal

### Session Management (`session.py`)

Sessions keyed by Telegram user ID. `UserSession` dataclass holds:
- `ClaudeSDKClient` instance
- Processing state
- Pending `AskUserQuestion` interactions
- Working directory

## Code Style

- Python 3.14+
- Single quotes for strings (ruff format)
- Line length 140
- Use `ty` for type checking (not mypy)
- Type narrow with `assert` for Telegram's Optional types
