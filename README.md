# rclaude

[![PyPI](https://img.shields.io/pypi/v/rclaude)](https://pypi.org/project/rclaude/)

Remote control for [Claude Code](https://claude.ai/code) via Telegram. Seamlessly teleport your coding sessions between terminal and phone.

## Features

- **Session Teleportation** - Switch between terminal and Telegram mid-conversation with `/tg`
- **Interactive Permissions** - Approve file edits and shell commands from your phone
- **Live Updates** - See Claude's activity streamed to your terminal while on Telegram
- **On-Demand Server** - Server starts automatically when needed, shuts down when idle
- **Hot Reload** - Development mode with automatic restart on code changes

## Requirements

- Python 3.14+
- [Claude Code](https://claude.ai/code) CLI installed
- Telegram account

## Installation

```bash
pip install rclaude
```

Or install from source:

```bash
git clone https://github.com/leobuskin/rclaude.git
cd rclaude
pip install -e .
```

## Setup

Run the interactive setup wizard:

```bash
rclaude setup
```

The wizard will guide you through:

1. **Create a Telegram bot** - Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token
2. **Link your account** - Send `/link <token>` to your new bot
3. **Install the /tg hook** - Adds the teleport command to Claude Code
4. **Auto-start (optional)** - Configure server to start on login (macOS)

## Usage

### Start a session

```bash
rclaude
```

This launches Claude Code with teleportation support. Work normally in your terminal.

### Teleport to Telegram

When you want to continue on your phone, type in Claude Code:

```
/tg
```

The session transfers to Telegram. Your terminal shows live updates of the conversation.

### Return to terminal

In Telegram, send `/cc` to get the command for resuming in terminal. The session seamlessly continues where you left off.

### Telegram commands

| Command | Description |
|---------|-------------|
| `/start` | Show help |
| `/new` | Start fresh session |
| `/cc` | Return to terminal |
| `/status` | Show session info |
| `/mode` | Change permission mode |
| `/model` | Change AI model |
| `/cost` | Show usage costs |
| `/stop` | Interrupt current task |
| `/cancel` | Cancel pending teleport |

## How It Works

```
Terminal                    Server                      Telegram
────────                    ──────                      ────────
rclaude ──────────────────────────────────────────────────────────
    │                          │                            │
    │  Claude Code running     │                            │
    │         │                │                            │
    │  user: /tg               │                            │
    │         │                │                            │
    │         └───── POST /teleport ─────► notify user      │
    │                          │                 │          │
    │  (shows live updates) ◄──┼── SSE stream    │          │
    │                          │                 ▼          │
    │                          │◄──────── user messages ────┤
    │                          │                            │
    │                          │  Claude Agent SDK          │
    │                          │         │                  │
    │                          │         ▼                  │
    │                          │──────► responses ─────────►│
    │                          │                            │
    │                          │◄──────── /cc ──────────────┤
    │                          │                            │
    │  resume ◄────────────────┤                            │
    │                          │                            │
```

1. **Wrapper** (`rclaude`) spawns Claude Code and monitors for `/tg`
2. **Hook** intercepts `/tg`, POSTs session info to local server
3. **Server** notifies via Telegram frontend, streams events back to terminal via SSE
4. **SDK** continues the conversation via Telegram messages with interactive permissions
5. **Return** with `/cc` emits event to terminal, shows resume command

## CLI Reference

```bash
rclaude              # Run Claude Code with teleport support
rclaude setup        # Interactive setup wizard
rclaude serve        # Start server manually (usually auto-started)
rclaude status       # Show configuration and server status
rclaude uninstall    # Remove configuration and hooks
```

### Options

```bash
rclaude --reload     # Development mode with hot-reload
rclaude --verbose    # Enable debug logging
rclaude --version    # Show version
```

## Configuration

Config is stored in `~/.config/rclaude/config.toml`:

```toml
[telegram]
bot_token = "123456:ABC..."
user_id = 123456789
username = "you"

[server]
host = "127.0.0.1"
port = 7680

[claude]
hook_installed = true
```

## Development

```bash
# Clone and install in dev mode
git clone https://github.com/leobuskin/rclaude.git
cd rclaude
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Run with hot-reload
rclaude --reload

# Lint
ruff check rclaude/
ruff format rclaude/

# Type check
ty check rclaude/
```

## License

MIT
