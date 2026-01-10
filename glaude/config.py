"""Configuration and environment loading."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / '.env')

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger('glaude')

# Telegram settings
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '')
MAX_MESSAGE_LENGTH = 4000

# Authorization
_allowed_str = os.getenv('ALLOWED_USERS', '')
ALLOWED_USERS: set[int] = set()
if _allowed_str.strip():
    ALLOWED_USERS = {int(uid.strip()) for uid in _allowed_str.split(',') if uid.strip()}
