"""Entry point for running rclaude as a module."""

import sys


def run() -> None:
    """Run rclaude CLI, handling -- separator for Claude args."""
    from rclaude import cli

    # Extract and remove args after -- before Click sees them
    if '--' in sys.argv:
        sep_idx = sys.argv.index('--')
        cli._claude_args = sys.argv[sep_idx + 1 :]
        sys.argv = sys.argv[:sep_idx]
    else:
        cli._claude_args = []

    cli.main()


if __name__ == '__main__':
    run()
