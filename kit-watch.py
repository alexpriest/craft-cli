#!/usr/bin/env python3
"""kit-watch — event-driven #kit pickup, modeled on the Kit iMessage flow.

A zero-token poll (launchd, every 5 min) is the *receiver*: one Craft search + a
diff against a state file. When a NEW #kit block appears, it fires a headless Kit
agent (`claude -p`, auto permission mode) as the *worker* — so the LLM runs only on
a real event, never on an empty poll. The worker decides for itself whether to text
Alex or just handle it and annotate the block in Craft for async review.

  ./kit-watch.py            one poll (what launchd runs)
  ./kit-watch.py --dry-run  print what it would dispatch; no agent, no state write

Creds: craftapi reads craft-cli/.env (so `op` never stalls under launchd). Texting
goes through `kit-notify` (the single Blooio swap point).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import craftapi as api  # noqa: E402

STATE = Path.home() / ".local" / "state" / "craft-kit-watch" / "seen.json"
KIT_NOTIFY = os.path.join(HERE, "kit-notify")
# Running claude from the Chief-of-Staff folder loads the Kit persona + operating manual.
KIT_CWD = str(Path.home() / "Obsidian" / "alexpriest" / "Claude" / "Chief of Staff")
# #kit as a whole tag: not followed by a word char or hyphen (so #kitchen / #kit-x don't match).
KIT_RE = re.compile(r"#kit(?![\w-])", re.IGNORECASE)
# Inline-code spans (`...`) are references, not live tags — strip them before matching so a
# doc that merely *mentions* `#kit` (e.g. a conventions note) never triggers the worker.
CODE_SPAN_RE = re.compile(r"`[^`]*`")


def _has_kit_tag(md: str) -> bool:
    return bool(KIT_RE.search(CODE_SPAN_RE.sub("", md)))
AGENT_TIMEOUT = 900  # seconds per item


def _block_markdown(base: str, cred: str, block_id: str) -> str:
    """Authoritative markdown for one block."""
    try:
        r = api.req("GET", f"{base}/blocks?id={block_id}&maxDepth=1", cred)
    except api.CraftError:
        return ""
    return (r.get("markdown") or "").strip()


def find_kit_blocks(base: str, cred: str) -> dict[str, str]:
    """{blockId: full-markdown} for every block carrying a live #kit tag.

    Search only nominates candidates: its snippets are truncated, lowercased, and
    render inline code as **bold**, so `#kit` in a reference note looks identical to
    a live tag there. Re-fetch each block for the real markdown before matching.
    """
    r = api.req("GET", f"{base}/documents/search?include=" + urllib.parse.quote("#kit"), cred)
    out = {}
    for m in r.get("items", []):
        for bid in m.get("blockIds", []):
            md = _block_markdown(base, cred, bid)
            if md and _has_kit_tag(md):
                out[bid] = md
    return out


def worker_prompt(block_id: str, text: str) -> str:
    return f"""You are Kit, handling an item Alex tagged #kit in Craft for you to action. \
This is an UNATTENDED headless run (launched by kit-watch), not a live chat.

The tagged block (id `{block_id}`):
\"\"\"
{text}
\"\"\"

Do the work. Guidance:
- Prefer CLIs — `craft` (Craft read/write), `gws` (Gmail/Calendar/Drive/Sheets/Docs), git. \
Some MCPs are ABSENT in this headless context: Linear, Duckbill, Slack, Granola, browser \
(claude-in-chrome). If the task needs one of those, do NOT fake it — text Alex to handle it.
- Safe/reversible actions: just do them. Outward or irreversible actions (emailing third \
parties, bookings, purchases, deletions): do them ONLY if this #kit item explicitly authorizes \
it; otherwise prepare and text Alex to confirm. You are unattended — no live human to check with.
- COMMS (decide with judgment): text Alex via `kit-notify "<plain text>"` ONLY if the outcome is \
notable, blocked, time-sensitive, needs his input, or you took an outward/irreversible action. \
Otherwise stay quiet and record it in Craft: mark the #kit block done and/or append a short note \
of what you did (use `craft edit {block_id} "<updated markdown>"`), so he can review async. \
Remove the #kit tag from the block once handled so it doesn't re-trigger. When anything you write \
back into Craft needs to *mention* the tag, wrap it in backticks (`#kit`) — a bare one in your own \
note re-triggers this watcher on the next poll and loops.
- Keep any text plain (no markdown). Be concise. End by stating what you did and how you reported it."""


def dispatch(block_id: str, text: str) -> bool:
    """Fire the headless Kit worker. Returns True on clean exit."""
    try:
        r = subprocess.run(
            ["claude", "-p", worker_prompt(block_id, text), "--permission-mode", "auto"],
            cwd=KIT_CWD, capture_output=True, text=True, timeout=AGENT_TIMEOUT,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def notify(text: str) -> None:
    try:
        subprocess.run([KIT_NOTIFY, text], check=False, timeout=30)
    except OSError:
        pass


def main(argv) -> int:
    dry = "--dry-run" in argv
    base, cred = api.creds()
    current = find_kit_blocks(base, cred)

    first_run = not STATE.exists()
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    new = [(bid, md) for bid, md in current.items() if bid not in seen]

    if first_run and not dry:
        # Seed silently so pre-existing #kit items don't all fire at once on first run.
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(list(current)))
        print(f"seeded {len(current)} existing #kit block(s), no dispatch")
        return 0

    # Mark seen BEFORE dispatching so a long/again-firing poll never double-handles.
    if not dry:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(list(current)))

    for bid, md in new:
        if dry:
            print("WOULD DISPATCH:", bid, "|", re.sub(r"\s+", " ", md)[:90])
            continue
        if not dispatch(bid, md):
            # Never silently drop: if the worker couldn't run, ping Alex to handle it.
            notify(f"⚠️ Couldn't auto-handle a #kit item in Craft:\n{re.sub(chr(10),' ',md)[:200]}")

    if not new:
        print("no new #kit items")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
