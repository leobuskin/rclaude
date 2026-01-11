# Smart Permission Rule Generation - Implementation Changes

This document contains all changes needed to implement smart Bash permission rules using Haiku.

## Overview

When user clicks "Always" on a Bash command permission, instead of generating a simple `Bash(git:*)` rule, we use Haiku to intelligently extract the command intent (tool + subcommand + flags) while wildcarding values.

Example: `git tag -a v0.2.1 -m "v0.2.1"` becomes `Bash(git tag -a -m *)` instead of `Bash(git:*)`

---

## Change 1: Add imports

**Location:** Top of `rclaude/server.py`, after line 16 (after `from telegram.constants import ChatAction`)

**Add:**
```python
import shlex
```

**Location:** In the `claude_agent_sdk` import block (around line 18-30)

**Change from:**
```python
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
```

**Change to:**
```python
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    query as claude_query,
)
```

---

## Change 2: Add smart rule generation functions

**Location:** After `generate_permission_rule()` function (around line 208), before `load_permission_rules()`

**Add this entire block:**

```python
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
    """Check if pattern is dangerously broad.

    Currently only rejects bare wildcard patterns.
    """
    pattern_prefix = pattern.rstrip(' *').strip()

    # Reject bare "*" - matches everything
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

    # Normalize: ensure ends with " *"
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
    # Extract base command for fallback
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
```

---

## Change 3: Rename and simplify `add_permission_rule`

**Location:** The `add_permission_rule` function (around line 243)

**Change from:**
```python
async def add_permission_rule(cwd: str, tool_name: str, input_data: dict[str, Any]) -> None:
    """Add a permission rule to .claude/settings.local.json in the project."""
    settings_path = Path(cwd) / '.claude' / 'settings.local.json'
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    if 'permissions' not in settings:
        settings['permissions'] = {'allow': [], 'deny': [], 'ask': []}

    # Generate rule pattern
    rule = generate_permission_rule(tool_name, input_data)

    # Add if not already present
    if rule not in settings['permissions']['allow']:
        settings['permissions']['allow'].append(rule)
        settings_path.write_text(json.dumps(settings, indent=2))
        logger.info(f'Added permission rule: {rule}')
```

**Change to:**
```python
def add_permission_rule_to_file(cwd: str, rule: str) -> None:
    """Add a permission rule to .claude/settings.local.json in the project."""
    settings_path = Path(cwd) / '.claude' / 'settings.local.json'
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    if 'permissions' not in settings:
        settings['permissions'] = {'allow': [], 'deny': [], 'ask': []}

    # Add if not already present
    if rule not in settings['permissions']['allow']:
        settings['permissions']['allow'].append(rule)
        settings_path.write_text(json.dumps(settings, indent=2))
        logger.info(f'Added permission rule: {rule}')
```

Note: Changed from `async def` to `def` (no longer async), renamed to `add_permission_rule_to_file`, and now accepts a pre-generated `rule` string instead of generating it internally.

---

## Change 4: Update the 'always' callback handler

**Location:** In `_handle_permission_callback()` function, the `elif action == 'always':` block (around line 1022-1029)

**Change from:**
```python
    elif action == 'always':
        # Add to CC permission file, then allow
        await add_permission_rule(session.cwd, pending.tool_name, pending.input_data)
        pending.result = PermissionResultAllow(updated_input=pending.input_data)
        rule = generate_permission_rule(pending.tool_name, pending.input_data)
        await query.edit_message_text(f'✓ Allowed (always)\n<code>{rule}</code>', parse_mode='HTML')
        logger.info('[PERM_CALLBACK] Setting event for always-allow')
        pending.event.set()
```

**Change to:**
```python
    elif action == 'always':
        # Generate smart rule for Bash, simple rule for others
        if pending.tool_name == 'Bash':
            command = pending.input_data.get('command', '')
            await query.edit_message_text('⏳ Generating smart rule...', parse_mode='HTML')
            rule = await generate_smart_bash_rule(command)
        else:
            rule = generate_permission_rule(pending.tool_name, pending.input_data)

        # Add to CC permission file, then allow
        add_permission_rule_to_file(session.cwd, rule)
        pending.result = PermissionResultAllow(updated_input=pending.input_data)
        await query.edit_message_text(f'✓ Allowed (always)\n<code>{rule}</code>', parse_mode='HTML')
        logger.info('[PERM_CALLBACK] Setting event for always-allow')
        pending.event.set()
```

---

## Summary of Changes

1. **Import `shlex`** - for command tokenization
2. **Import `query as claude_query`** - for one-shot Haiku calls
3. **Add smart rule functions:**
   - `_SMART_RULE_SYSTEM_PROMPT` - system prompt for Haiku
   - `_pattern_matches_command()` - validates pattern matches original command
   - `_is_pattern_too_broad()` - rejects bare `*` patterns
   - `_generate_pattern_once()` - single Haiku call
   - `generate_smart_bash_rule()` - orchestrator with retry and fallback
4. **Rename `add_permission_rule` to `add_permission_rule_to_file`** - now accepts pre-generated rule string
5. **Update 'always' handler** - use smart rule for Bash, show "Generating..." message

## Expected Behavior

- User runs a Bash command like `git tag -a v0.2.1 -m "v0.2.1"`
- Permission prompt appears in Telegram
- User clicks "Always"
- Message updates to "⏳ Generating smart rule..."
- Haiku generates pattern: `git tag -a -m *`
- Pattern is validated against original command
- If valid, rule `Bash(git tag -a -m *)` is added to settings
- Message updates to "✓ Allowed (always)" with the rule shown
- Future `git tag -a ... -m ...` commands auto-allowed

## Fallback Behavior

If Haiku fails or returns invalid pattern after 3 attempts:
- Falls back to simple `Bash(git:*)` rule
- Logs warning about fallback
