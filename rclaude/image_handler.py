"""Image handling for Telegram photos - download, encode, and prepare for Claude."""

import asyncio
import base64
import logging
from pathlib import Path
from typing import Optional

from telegram import PhotoSize, Bot

logger = logging.getLogger('rclaude')


async def download_telegram_photo(bot: Bot, photo_sizes: tuple[PhotoSize, ...] | list[PhotoSize], user_id: int) -> Optional[Path]:
    """Download highest-resolution photo from Telegram and save locally.

    Args:
        bot: Telegram bot instance
        photo_sizes: List of PhotoSize objects from message
        user_id: User ID for organizing temp files

    Returns:
        Path to downloaded file, or None if download failed
    """
    if not photo_sizes:
        logger.warning('[IMAGE] No photo sizes provided')
        return None

    # Get highest resolution photo (last one is typically largest)
    largest_photo = photo_sizes[-1]

    # Create temp directory for images
    temp_dir = Path('/tmp/rclaude-images') / str(user_id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Download file
    try:
        file_info = await bot.get_file(largest_photo.file_id)
        file_bytes = await file_info.download_as_bytearray()

        # Telegram photos are always JPEG
        ext = '.jpg'

        # Save with timestamp
        import time
        timestamp = int(time.time() * 1000)
        file_path = temp_dir / f'{timestamp}{ext}'

        file_path.write_bytes(file_bytes)
        logger.info(f'[IMAGE] Downloaded photo: {file_path} ({len(file_bytes)} bytes)')
        return file_path

    except Exception as e:
        logger.error(f'[IMAGE] Failed to download photo: {e}')
        return None


def _get_extension_from_mime(mime_type: str) -> str:
    """Get file extension from MIME type."""
    mime_to_ext = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'image/bmp': '.bmp',
        'image/tiff': '.tiff',
    }
    return mime_to_ext.get(mime_type, '.jpg')


def get_image_mime_type(file_path: Path) -> str:
    """Detect MIME type from file extension."""
    ext_to_mime = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.tiff': 'image/tiff',
    }
    ext = file_path.suffix.lower()
    return ext_to_mime.get(ext, 'image/jpeg')


async def encode_image_base64(file_path: Path) -> Optional[str]:
    """Convert image file to base64 string.

    Args:
        file_path: Path to image file

    Returns:
        Base64-encoded image string, or None if encoding failed
    """
    try:
        # Read file asynchronously
        loop = asyncio.get_event_loop()
        file_bytes = await loop.run_in_executor(None, file_path.read_bytes)

        # Encode to base64
        base64_str = base64.standard_b64encode(file_bytes).decode('utf-8')
        logger.info(f'[IMAGE] Encoded image to base64 ({len(base64_str)} chars)')
        return base64_str

    except Exception as e:
        logger.error(f'[IMAGE] Failed to encode image: {e}')
        return None


async def prepare_image_for_claude(file_path: Path) -> Optional[tuple[str, str]]:
    """Prepare image for Claude SDK by encoding to base64.

    Args:
        file_path: Path to downloaded image file

    Returns:
        Tuple of (base64_data, mime_type), or None if preparation failed
    """
    if not file_path.exists():
        logger.error(f'[IMAGE] File does not exist: {file_path}')
        return None

    # Get MIME type
    mime_type = get_image_mime_type(file_path)

    # Encode to base64
    base64_data = await encode_image_base64(file_path)
    if not base64_data:
        return None

    logger.info(f'[IMAGE] Image prepared for Claude: {mime_type}')
    return (base64_data, mime_type)


def cleanup_image_file(file_path: Path) -> None:
    """Delete image file after use."""
    try:
        if file_path.exists():
            file_path.unlink()
            logger.info(f'[IMAGE] Cleaned up image: {file_path}')
    except Exception as e:
        logger.warning(f'[IMAGE] Failed to cleanup image: {e}')


async def cleanup_old_images(user_id: int, max_age_hours: int = 1) -> None:
    """Clean up old image files for a user.

    Args:
        user_id: User ID
        max_age_hours: Delete images older than this many hours
    """
    import time

    temp_dir = Path('/tmp/rclaude-images') / str(user_id)
    if not temp_dir.exists():
        return

    current_time = time.time()
    max_age_seconds = max_age_hours * 3600

    try:
        for file_path in temp_dir.glob('*'):
            if file_path.is_file():
                age_seconds = current_time - file_path.stat().st_mtime
                if age_seconds > max_age_seconds:
                    cleanup_image_file(file_path)
    except Exception as e:
        logger.warning(f'[IMAGE] Failed to cleanup old images: {e}')
