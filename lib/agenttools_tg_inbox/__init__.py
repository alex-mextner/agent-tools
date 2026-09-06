"""agenttools_tg_inbox — the tg-ctl Stop-hook inbox reader (agent-tools#526, tg-cli#306).

Accessed via: the harness Stop bridges (``cc_hook_bridge``, ``codex_hook_bridge``) call
:func:`consume_pending` at every Stop and, when it returns entries, block the stop with
the messages as the reason — Claude Code / Codex then feed that text to the agent as its
next instruction. This is the tmux-free delivery channel for an agent that tg-ctl cannot
reach with ``tmux send-keys`` (started from a plain terminal tab).

THE INBOX KEY CONTRACT — shared byte-for-byte with tg-cli ``features/tg-ctl/unreachable.ts``
(both READMEs document it; the test vectors in ``tests/test_tg_inbox.py`` and tg-cli's
``tests/ctl-unreachable.test.ts`` are identical on purpose):

    key = sanitized(--name value)          when the agent's argv carries ``--name X``
        = "cwd-" + sha256(cwd)[:16] (hex)   otherwise
    sanitized(x) = every char outside [A-Za-z0-9._-] -> "_", truncated to 64 chars;
                   an empty result counts as "no name".
    cwd = the agent process's working directory with trailing "/" stripped ("/" stays).
    inbox dir = <tg-cli config dir>/inbox/<key>/
                (config dir = $TG_CTL_CONFIG_DIR, else ~/.config/tg-cli)
    pending.jsonl   -- appended by the tg-ctl daemon, one JSON object per line
    delivered.jsonl -- appended by this reader after it handed the entries to the agent
    acked.jsonl     -- appended by the daemon after reacting on the Telegram message

Assumptions: the inbox is a local file written by the user's own tg-ctl daemon; its
content is DATA for the agent (rendered as the block reason), never executed here. A
missing, empty or malformed inbox never blocks (fail-open, logged to stderr). Delivery is
at-most-once: entries are archived to delivered.jsonl BEFORE the block reason is returned.
"""

from __future__ import annotations

from .core import (
    agent_key,
    agent_key_for_process,
    consume_pending,
    format_block_reason,
    inbox_dir,
    inbox_root,
    parse_agent_name,
    sanitize_agent_name,
)

__all__ = [
    "agent_key",
    "agent_key_for_process",
    "consume_pending",
    "format_block_reason",
    "inbox_dir",
    "inbox_root",
    "parse_agent_name",
    "sanitize_agent_name",
]
