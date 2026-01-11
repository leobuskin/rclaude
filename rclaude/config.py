"""Configuration and environment loading.

This module provides backward compatibility by loading from:
1. ~/.config/rclaude/config.toml (new setup)
2. .env file (legacy)
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from rclaude.settings import load_config as load_toml_config, CONFIG_FILE

# Load .env from project root (legacy support)
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / '.env')

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger('rclaude')

# Telegram message limit
MAX_MESSAGE_LENGTH = 4000

# Load config - prefer TOML, fall back to .env
_toml_config = load_toml_config() if CONFIG_FILE.exists() else None

if _toml_config and _toml_config.is_configured():
    # Use new TOML config
    TG_BOT_TOKEN = _toml_config.telegram.bot_token
    ALLOWED_USERS: set[int] = {_toml_config.telegram.user_id} if _toml_config.telegram.user_id else set()
else:
    # Fall back to .env (legacy)
    TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '')
    _allowed_str = os.getenv('ALLOWED_USERS', '')
    ALLOWED_USERS = set()
    if _allowed_str.strip():
        ALLOWED_USERS = {int(uid.strip()) for uid in _allowed_str.split(',') if uid.strip()}
