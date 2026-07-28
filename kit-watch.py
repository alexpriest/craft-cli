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

Do the work, then RESPOND INSIDE THE BLOCK. Guidance:
- PRIMARY output: write your answer/result as children of the tagged block with \
`craft append {block_id} --stdin` (pipe markdown; it nests inside the block's page, where Alex \
opens it). This is the default — Alex reviews there, no text needed.
- ⚠ IF THE TAGGED BLOCK CONTAINS SEVERAL DISTINCT ASKS as its own child blocks (a "few things I'd \
love help on" list, a checklist, etc.), do NOT dump everything onto the parent. `craft get <docId> \
--json` to find each child's block id, then `craft append <thatChildId>` so each answer nests under \
the ask it answers. Appending it all to the parent is technically "inside the tagged block" and \
still WRONG: the answers pile up after his list instead of beneath the item each one addresses, and \
he cannot tell which answer belongs to which ask. Observed 2026-07-28 on the Halide note — 45 blocks \
landed in one heap under a 4-item list. Answer the parent directly only when it is a single ask.
- FORMATTING: write REAL markdown — `craft append` hands it to Craft whole and Craft parses it into \
native blocks. A wall of **bold**-lead paragraphs is the failure mode; use structure:
  • `| a | b |` GFM tables for ANY comparison, price list, spec sheet or option set. This is the \
single biggest readability win — reach for it the moment you have 2+ things with 2+ attributes. \
Numbers belong in a table, not in prose.
  • `<callout>…</callout>` for the ONE finding that matters most. One per answer, not five.
  • `+ Toggle line` with 2-space-indented children to fold sources, caveats and raw detail out of \
the way — keeps the answer short without dropping the receipts.
  • `###` for section breaks. Never `#`/`##` — they render as huge headlines.
  • `<caption>…</caption>` for provenance/footnotes, `==text==` to highlight a single key number.
  • `[label](date://YYYY-MM-DD)` for dates, `[label](block://blockId)` to cross-reference a block.
  • Blank line between paragraphs — a single newline continues the same block.
- Then SWAP the tag: remove #kit and put `#review` in its place, on the block's own line \
(`craft edit {block_id} "<line> #review"`). Removing #kit stops it re-triggering; adding #review is \
how Alex FINDS the finished work — previously a completed item just lost its tag and became \
invisible, so he had no way to know you'd answered. Every item you finish gets #review, without \
exception, including ones you only acknowledged or judged to need no action. He clears the tag \
himself once he has read it. Keep the line's text EXACTLY as Alex wrote it apart from that tag swap — \
do NOT add a `#` heading prefix or otherwise reformat it (that turns his question into an H1). When \
your appended answer must *mention* either tag, wrap it in backticks (`#kit`) — a bare one re-triggers \
this watcher and loops.
- Be PROPORTIONATE: match effort to the task. A quick question gets a quick answer — don't spin up \
calendar + web research for something simple. Reach for tools only when the task actually needs them.
- Tools: `craft` (Craft), `gws` (Gmail/Calendar/Drive/Sheets/Docs), git, plus MCPs (Linear, Gmail, \
Calendar, Slack, Granola, Duckbill, etc. — most work headless). Browser automation (claude-in-chrome) \
and any server needing interactive auth are unavailable; if the task truly needs one, say so in your \
in-block response.
- ACT vs ASK (you are unattended — no live human to check with):
  • JUST DO (reversible/contained): answer in-block; read anything; create NEW content (a new note/doc, \
a draft email left UNSENT); add a SINGLE event to any of Alex's calendars incl. family/shared; edit an \
existing calendar event that has NO guests.
  • PROPOSE-ONLY — write the plan into the block + text Alex, and DO NOT act (unless this #kit note \
explicitly authorizes it, e.g. "go ahead"): bulk calendar changes (more than ~2–3 events, or rewriting \
a series); editing an existing event that HAS guests; editing/overwriting/deleting anything that already \
exists (vault files, Craft docs, calendar events); sending anything outward (email or message to third \
parties, comments others see, a Duckbill call); bookings, purchases, anything touching money; \
system/config/permission changes; reaching out to people.
  • NEVER unattended — bounce to Alex and stop: financial transactions beyond trivial, anything \
legal/medical, mass sends, mass deletion.
- Text Alex via `kit-notify "<plain text>"` ONLY if blocked, time-sensitive, needs a decision, or \
you took an outward/irreversible action — otherwise stay in-block. Keep any text plain, no markdown."""


def dispatch(block_id: str, text: str) -> bool:
    """Fire the headless Kit worker DETACHED and return immediately.

    Non-blocking so the poll never stalls and multiple #kit items run in parallel.
    start_new_session detaches it from kit-watch's process group; output goes to a
    worker log for debugging. Returns True if the process launched.
    """
    try:
        log = open(os.path.join(HERE, "worker.log"), "a")
        subprocess.Popen(
            ["claude", "-p", worker_prompt(block_id, text), "--permission-mode", "auto"],
            cwd=KIT_CWD, stdout=log, stderr=log, start_new_session=True,
        )
        return True
    except OSError:
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
