"""Permission logic for tool approval."""

import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query as claude_query

from .session import ContextUsage

logger = logging.getLogger('rclaude')

# Tools that require interactive approval in default mode
APPROVAL_REQUIRED_TOOLS = {'Edit', 'Write', 'Bash', 'NotebookEdit'}

# Edit tools that are auto-allowed in acceptEdits mode
EDIT_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}

# Valid permission modes
VALID_MODES = ('default', 'acceptEdits', 'plan', 'bypassPermissions')

# Permission mode display names
MODE_DISPLAY = {
    'default': '🔒 Default (ask for permissions)',
    'acceptEdits': '📝 Accept Edits (auto-allow file changes)',
    'plan': '📋 Plan Mode (read-only)',
    'bypassPermissions': '⚠️ Dangerous (skip all permissions)',
}


def format_mode_display(mode: str) -> str:
    """Format permission mode for display."""
    return MODE_DISPLAY.get(mode, f'🔒 {mode}')


def validate_permission_mode(mode: str) -> str:
    """Validate and return a permission mode, defaulting to 'default' if invalid."""
    if mode in VALID_MODES:
        return mode
    return 'default'


def format_mode_short(mode: str) -> str:
    """Format permission mode as short label."""
    short = {
        'default': '🔒',
        'acceptEdits': '📝',
        'plan': '📋',
        'bypassPermissions': '⚠️',
    }
    return short.get(mode, '🔒')


def format_model_short(model: str | None) -> str:
    """Format model name as short label."""
    if not model:
        return '⚡ sonnet'
    m = model.lower()
    if 'opus' in m:
        return '🧠 opus'
    if 'haiku' in m:
        return '🚀 haiku'
    return f'⚡ {model}'


def parse_context_output(text: str) -> ContextUsage | None:
    """Parse /context command output to extract token usage.

    Expected formats:
        **Tokens:** 21.8k / 200.0k (11%)  (markdown from SDK)
        Tokens: 24.4k / 200.0k (12%)      (plain text from CLI)
    """
    match = re.search(r'\*?\*?Tokens:\*?\*?\s*([\d.]+)k\s*/\s*([\d.]+)k\s*\((\d+)%\)', text)
    if not match:
        return None

    used_str, max_str, percent_str = match.groups()

    return ContextUsage(
        tokens_used=int(float(used_str) * 1000),
        tokens_max=int(float(max_str) * 1000),
        percent_used=int(percent_str),
    )


def generate_permission_rule(tool_name: str, input_data: dict[str, Any]) -> str:
    """Generate CC-compatible permission rule pattern."""
    if tool_name == 'Bash':
        command = input_data.get('command', '')
        base_cmd = command.split()[0] if command else ''
        return f'Bash({base_cmd}:*)'
    elif tool_name == 'Edit':
        file_path = input_data.get('file_path', '')
        return f'Edit(//{file_path})'
    elif tool_name == 'Write':
        file_path = input_data.get('file_path', '')
        return f'Write(//{file_path})'
    elif tool_name == 'NotebookEdit':
        notebook_path = input_data.get('notebook_path', '')
        return f'NotebookEdit(//{notebook_path})'
    else:
        return f'{tool_name}(*)'


def load_permission_rules(cwd: str) -> list[str]:
    """Load allow rules from .claude/settings.local.json."""
    settings_path = Path(cwd) / '.claude' / 'settings.local.json'
    if not settings_path.exists():
        return []
    try:
        settings = json.loads(settings_path.read_text())
        return settings.get('permissions', {}).get('allow', [])
    except (json.JSONDecodeError, KeyError):
        return []


def check_permission_rule(tool_name: str, input_data: dict[str, Any], rules: list[str]) -> bool:
    """Check if a tool call matches any allow rule."""
    generated_rule = generate_permission_rule(tool_name, input_data)

    if generated_rule in rules:
        return True

    if tool_name == 'Bash':
        command = input_data.get('command', '')
        base_cmd = command.split()[0] if command else ''
        if f'Bash({base_cmd}:*)' in rules:
            return True
        if 'Bash(*)' in rules:
            return True

    return False


def add_permission_rule(cwd: str, rule: str) -> None:
    """Add a permission rule to .claude/settings.local.json in the project."""
    settings_path = Path(cwd) / '.claude' / 'settings.local.json'
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    if 'permissions' not in settings:
        settings['permissions'] = {'allow': [], 'deny': [], 'ask': []}

    if rule not in settings['permissions']['allow']:
        settings['permissions']['allow'].append(rule)
        settings_path.write_text(json.dumps(settings, indent=2))
        logger.info(f'Added permission rule: {rule}')


class PermissionChecker:
    """Handles permission mode logic and rule matching."""

    def should_auto_allow(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        mode: str,
        cwd: str,
    ) -> bool:
        """Check if tool should be auto-allowed based on mode and rules."""
        if mode == 'bypassPermissions':
            return True
        if mode == 'acceptEdits' and tool_name in EDIT_TOOLS:
            return True
        if tool_name not in APPROVAL_REQUIRED_TOOLS:
            return True
        rules = load_permission_rules(cwd)
        return check_permission_rule(tool_name, input_data, rules)


# ─────────────────────────────────────────────────────────────────────────────
# Smart Permission Rule Generation (using Haiku)
# ─────────────────────────────────────────────────────────────────────────────

_SMART_RULE_SYSTEM_PROMPT = """You convert bash commands to permission patterns. Output ONLY the pattern, nothing else.

Rules:
- Keep: command name, subcommand, all flags (starting with - or --)
- Remove: all values (paths, names, URLs, numbers like -20)
- End with single *

Examples:
Input: git push origin main --tags
Output: git push --tags *

Input: head -20 file.txt
Output: head *

Input: tail -f log.txt
Output: tail -f *

Input: docker run -it --rm -v /a:/b img
Output: docker run -it --rm -v *

Input: kubectl get pods -n ns -o wide
Output: kubectl get pods -n -o *

Input: python3 script.py --verbose
Output: python3 --verbose *

Respond with ONLY the pattern, no explanation."""


def _pattern_matches_command(pattern: str, command: str) -> bool:
    """Check if a permission pattern matches the original command."""
    if not pattern.endswith(' *') and pattern != '*':
        if not pattern.endswith('*'):
            return False

    pattern_prefix = pattern.rstrip(' *').strip()

    if not pattern_prefix:
        return True

    try:
        pattern_tokens = shlex.split(pattern_prefix)
        command_tokens = shlex.split(command)
    except ValueError:
        pattern_tokens = pattern_prefix.split()
        command_tokens = command.split()

    if not command_tokens:
        return False

    cmd_idx = 0
    for pat_token in pattern_tokens:
        found = False
        while cmd_idx < len(command_tokens):
            if command_tokens[cmd_idx] == pat_token:
                found = True
                cmd_idx += 1
                break
            cmd_idx += 1

        if not found:
            return False

    return True


def _is_pattern_too_broad(pattern: str) -> bool:
    """Check if pattern is dangerously broad."""
    pattern_prefix = pattern.rstrip(' *').strip()
    if not pattern_prefix:
        return True
    return False


async def _generate_pattern_once(command: str) -> str:
    """Single attempt to generate a pattern using Haiku."""
    options = ClaudeAgentOptions(
        tools=[],
        model='haiku',
        max_turns=1,
        system_prompt=_SMART_RULE_SYSTEM_PROMPT,
    )

    result = ''
    async for message in claude_query(prompt=command, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result += block.text

    pattern = result.strip()

    if not pattern.endswith(' *'):
        if pattern.endswith('*'):
            pattern = pattern[:-1].rstrip() + ' *'
        else:
            pattern = pattern + ' *'

    return pattern


async def generate_smart_bash_rule(command: str, max_retries: int = 2) -> str:
    """Generate a smart permission rule pattern for a Bash command.

    Uses Haiku to intelligently extract the command intent (tool + subcommand + flags)
    while wildcarding values (paths, names, URLs, etc).

    Returns a pattern like "Bash(git push --tags *)" or falls back to simple
    "Bash(git:*)" if generation fails.
    """
    try:
        tokens = shlex.split(command)
        base_cmd = tokens[0] if tokens else command.split()[0]
    except (ValueError, IndexError):
        base_cmd = command.split()[0] if command else 'unknown'

    fallback = f'Bash({base_cmd}:*)'

    for attempt in range(max_retries + 1):
        try:
            pattern = await _generate_pattern_once(command)

            if not _pattern_matches_command(pattern, command):
                logger.warning(f'[SMART_RULE] Attempt {attempt + 1}: pattern {pattern!r} does not match command')
                continue

            if _is_pattern_too_broad(pattern):
                logger.warning(f'[SMART_RULE] Attempt {attempt + 1}: pattern {pattern!r} is too broad')
                continue

            logger.info(f'[SMART_RULE] Generated: {pattern}')
            return f'Bash({pattern})'

        except Exception as e:
            logger.warning(f'[SMART_RULE] Attempt {attempt + 1} error: {e}')

    logger.warning(f'[SMART_RULE] All attempts failed, using fallback: {fallback}')
    return fallback


def can_resume_session(session_id: str, cwd: str) -> bool:
    """Check if a session can be resumed (exists and has actual conversation content)."""
    project_path = cwd.replace('/', '-').replace(':', '')
    if project_path.startswith('-'):
        project_path = project_path[1:]

    log_dir = Path.home() / '.claude' / 'projects' / f'-{project_path}'
    log_file = log_dir / f'{session_id}.jsonl'

    if not log_file.exists():
        logger.info(f'[SESSION] can_resume_session: {session_id[:8]}... -> False (file not found)')
        return False

    try:
        with open(log_file) as f:
            for line in f:
                if '"type":"user"' in line or '"type":"assistant"' in line:
                    logger.info(f'[SESSION] can_resume_session: {session_id[:8]}... -> True (has messages)')
                    return True
        logger.info(f'[SESSION] can_resume_session: {session_id[:8]}... -> False (no messages, only metadata)')
        return False
    except Exception as e:
        logger.warning(f'[SESSION] can_resume_session: {session_id[:8]}... -> False (error: {e})')
        return False
