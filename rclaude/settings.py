"""Configuration management for rclaude.

Config is stored in ~/.config/rclaude/config.toml
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w


CONFIG_DIR = Path.home() / '.config' / 'rclaude'
CONFIG_FILE = CONFIG_DIR / 'config.toml'
HOOK_DIR = Path.home() / '.claude' / 'commands'
CLAUDE_SETTINGS_FILE = Path.home() / '.claude' / 'settings.json'


@dataclass
class TelegramConfig:
    bot_token: str = ''
    user_id: int = 0
    username: str = ''


@dataclass
class ServerConfig:
    host: str = '127.0.0.1'
    port: int = 7680


@dataclass
class ClaudeConfig:
    hook_installed: bool = False


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)

    def is_configured(self) -> bool:
        """Check if rclaude is fully configured."""
        return bool(self.telegram.bot_token and self.telegram.user_id)

    def to_dict(self) -> dict:
        """Convert config to dict for TOML serialization."""
        return {
            'telegram': {
                'bot_token': self.telegram.bot_token,
                'user_id': self.telegram.user_id,
                'username': self.telegram.username,
            },
            'server': {
                'host': self.server.host,
                'port': self.server.port,
            },
            'claude': {
                'hook_installed': self.claude.hook_installed,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Config':
        """Create config from dict."""
        config = cls()

        if 'telegram' in data:
            tg = data['telegram']
            config.telegram.bot_token = tg.get('bot_token', '')
            config.telegram.user_id = tg.get('user_id', 0)
            config.telegram.username = tg.get('username', '')

        if 'server' in data:
            srv = data['server']
            config.server.host = srv.get('host', '127.0.0.1')
            config.server.port = srv.get('port', 7680)

        if 'claude' in data:
            cc = data['claude']
            config.claude.hook_installed = cc.get('hook_installed', False)

        return config


def load_config() -> Config:
    """Load config from file, or return defaults if not exists."""
    if not CONFIG_FILE.exists():
        return Config()

    with open(CONFIG_FILE, 'rb') as f:
        data = tomllib.load(f)

    return Config.from_dict(data)


def save_config(config: Config) -> None:
    """Save config to file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_FILE, 'wb') as f:
        tomli_w.dump(config.to_dict(), f)


def get_server_url(config: Config | None = None) -> str:
    """Get the server URL."""
    if config is None:
        config = load_config()
    return f'http://{config.server.host}:{config.server.port}'
