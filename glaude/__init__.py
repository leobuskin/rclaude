"""Glaude - Telegram bot for remote Claude Code control."""

try:
    from importlib.metadata import version, PackageNotFoundError

    try:
        __version__ = version('glaude')
    except PackageNotFoundError:
        __version__ = '0.0.0+unknown'
except ImportError:
    __version__ = '0.0.0+unknown'
