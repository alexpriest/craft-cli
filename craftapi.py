"""Self-contained low-level client for the Craft Docs REST API.

Owns auth (browser User-Agent + Bearer), cred resolution (.env in this dir, else
1Password `op`), and the request helper. Vendored here so `craft-cli` is a fully
independent tool — it does NOT depend on craft-mirror. (craft-mirror keeps its own
copy in craft_append.py; the two are deployed separately.)

Craft API landmines live in ~/Code/tools/craft-mirror/README.md — read them before
extending. base_url is a secret (embeds the link id): never put it on a command line.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_ENV_FILE = Path(__file__).resolve().parent / ".env"

# A divider. There is NO {"type":"divider"} — that 400s. A text block whose
# markdown is "---" comes back as {"type":"line"}.
DIVIDER = {"type": "text", "markdown": "---", "indentationLevel": 0}


# Gateway statuses worth replaying: the request never reached the app, so repeating a
# non-idempotent POST cannot duplicate a block. 500 is deliberately EXCLUDED — the app saw
# that one and may have partially applied it, so replaying is how you end up with two.
_RETRY_STATUS = {429, 502, 503, 504}
_REQ_ATTEMPTS = 4
_REQ_BACKOFF_S = 2.0


class CraftError(Exception):
    status: int | None = None  # HTTP status when the failure came back as one


def _load_env_file() -> None:
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[len("export "):] if line.startswith("export ") else line
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def creds() -> tuple[str, str]:
    """(base_url, credential) from env / .env, falling back to 1Password.

    Never let `op` run under launchd — it stalls waiting on the desktop app.
    """
    _load_env_file()
    out = []
    for field, key in (("base_url", "CRAFT_BASE_URL"), ("credential", "CRAFT_CREDENTIAL")):
        if os.environ.get(key):
            out.append(os.environ[key].strip())
            continue
        args = ["op", "item", "get", "Craft API", "--vault", "Claude", "--fields", f"label={field}"]
        if field == "credential":
            args.append("--reveal")
        out.append(subprocess.run(args, capture_output=True, text=True,
                                  check=True, timeout=20).stdout.strip())
    base, cred = out[0].rstrip("/"), out[1]
    if not base or not cred:
        raise CraftError("missing Craft credentials (CRAFT_BASE_URL / CRAFT_CREDENTIAL)")
    return base, cred


def req(method: str, path_or_url: str, cred: str, *, json_body=None):
    """Issue a request. `path_or_url` may be a full URL or a `/…` path (needs base)."""
    url = path_or_url
    headers = {"Authorization": f"Bearer {cred}", "User-Agent": _UA}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Craft's edge returns transient 502/503/504 (hit 2026-07-30). Retry gateway-level
    # failures and network errors only — see _RETRY_STATUS for why 500 is not among them.
    last: CraftError | None = None
    for attempt in range(1, _REQ_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as r:
                body = r.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            last = CraftError(f"{method} {url.split('?')[0]} -> {e.code}: {detail}")
            last.status = e.code
            if e.code not in _RETRY_STATUS:
                raise last
        except (urllib.error.URLError, TimeoutError) as e:
            last = CraftError(f"{method} {url.split('?')[0]} -> network error: {e}")
        if attempt < _REQ_ATTEMPTS:
            time.sleep(_REQ_BACKOFF_S * (2 ** (attempt - 1)))
    raise last


def needs_separator(base: str, cred: str, date: str) -> bool:
    """Should this append lay down a divider?

    ONE divider per note, not one per call (2026-07-29). Look at what the append
    will actually land against — the last meaningful top-level block:

      * nothing there (empty note, or a 404 because the write will create it):
        no rule. A divider with nothing above it separates nothing.
      * already a divider: no rule. That one is the boundary.
      * already a task: no rule. Continuing an existing agent section — a second
        rule between two checkbox lists is the noise Alex flagged.
      * anything else (prose, a toggle, a table): yes. The case the divider is for.

    Deliberately NOT "does the note contain a divider anywhere": Alex separates his
    own timestamped journal entries with `***`, so that test would suppress the one
    rule that's genuinely needed on a heavily-journaled day.

    Any non-404 read failure falls back to yes — a stray rule beats a collision.
    """
    try:
        doc = req("GET", f"{base}/blocks?date={date}", cred)
    except CraftError as e:
        return "NOT_FOUND_ERROR" not in str(e)
    for block in reversed(doc.get("content") or []):
        if not (block.get("markdown") or "").strip():
            continue  # trailing blank block — Craft leaves these behind
        if block.get("type") == "line":
            return False
        return block.get("listStyle") != "task"
    return False


def append_tasks(base: str, cred: str, date: str, tasks: list[str], *,
                 separator: bool | None = None) -> list[str]:
    """Append checkbox tasks to a daily note (auto-creates the note), under one divider.

    separator=None (default) decides via `needs_separator`; True/False force it.
    """
    if separator is None:
        separator = needs_separator(base, cred, date)
    blocks = ([DIVIDER] if separator else []) + [
        {"type": "text", "markdown": t, "listStyle": "task", "indentationLevel": 0} for t in tasks
    ]
    if len(blocks) > 20:
        raise CraftError(f"{len(blocks)} blocks; Craft caps a /blocks call at 20")
    created = req("POST", f"{base}/blocks", cred,
                  json_body={"blocks": blocks, "position": {"date": date, "position": "end"}})
    return [b["id"] for b in created.get("items", [])]


def update_block(base: str, cred: str, block_id: str, markdown: str) -> dict:
    """Edit a block's text in place. PUT /blocks — PATCH 404s."""
    return req("PUT", f"{base}/blocks", cred, json_body={"blocks": [{"id": block_id, "markdown": markdown}]})


# --- block deletion -------------------------------------------------------
#
# `DELETE /blocks {"blockIds":[...]}` is real and has been all along (it is in
# the OpenAPI spec and was documented in craft-mirror's README on 2026-07-23).
# A 2026-07-30 session asserted "Craft has no block DELETE" and worked around it
# by rewriting a stale block instead; that claim was wrong. Verified live:
#
#   * Works on prose, task, and page blocks alike.
#   * A bad/already-deleted id 404s, and the call is ATOMIC — one bad id in a
#     batch deletes NOTHING. Passing a documentId 400s ("Root block cannot be
#     deleted"), so a whole doc can't be lost through this path.
#   * Deleting a parent PROMOTES its children to the parent's level; it does
#     not cascade. Nothing is silently destroyed, but nesting is lost.
#   * ⚠ THERE IS NO TRASH FOR BLOCKS. Unlike DELETE /documents (soft, 30-day
#     Recently Deleted), a deleted block is gone server-side. The local undo
#     journal below is the ONLY undo that exists — always capture before deleting.

UNDO_DIR = Path.home() / ".local" / "state" / "craft-cli" / "undo"


def doc_blocks(base: str, cred: str, *, doc_id: str | None = None,
               date: str | None = None) -> list[dict]:
    """Flat list of a document's (or daily note's) blocks. One of doc_id/date."""
    if not (doc_id or date):
        raise CraftError("doc_blocks needs doc_id or date")
    key = f"id={doc_id}" if doc_id else f"date={date}"
    return req("GET", f"{base}/blocks?{key}", cred).get("content") or []


def capture_blocks(base: str, cred: str, block_ids: list[str], *,
                   doc_id: str | None = None, date: str | None = None) -> list[dict]:
    """Snapshot blocks so a delete can be undone. Call BEFORE deleting.

    With doc/date context we also record each block's preceding sibling, which
    lets `undo` put it back exactly where it was. Without context we can still
    capture the text (via a per-block GET), but placement is unknown and a
    restore has to be told which document to land in.
    """
    context = doc_blocks(base, cred, doc_id=doc_id, date=date) if (doc_id or date) else []
    by_id = {b["id"]: i for i, b in enumerate(context)}
    entries = []
    for bid in block_ids:
        if bid in by_id:
            i = by_id[bid]
            block = context[i]
            entries.append({
                "id": bid,
                "markdown": block.get("markdown") or "",
                "type": block.get("type"),
                "prev_sibling": context[i - 1]["id"] if i > 0 else None,
                "index": i,
                "doc_id": doc_id,
                "date": date,
            })
        else:
            block = req("GET", f"{base}/blocks?id={bid}", cred)
            entries.append({
                "id": bid,
                "markdown": block.get("markdown") or "",
                "type": block.get("type"),
                "prev_sibling": None,
                "doc_id": doc_id,
                "date": date,
                "placement_unknown": True,
            })
    return entries


def write_journal(entries: list[dict], stamp: str) -> Path:
    """Persist a capture so `craft undo` can restore it. Returns the journal path."""
    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    path = UNDO_DIR / f"{stamp}.json"
    path.write_text(json.dumps(entries, indent=1))
    return path


def delete_blocks(base: str, cred: str, block_ids: list[str]) -> list[str]:
    """Delete blocks. Atomic per call — any unknown id aborts the whole batch."""
    deleted = []
    for i in range(0, len(block_ids), 40):
        chunk = block_ids[i:i + 40]
        r = req("DELETE", f"{base}/blocks", cred, json_body={"blockIds": chunk})
        deleted += [x["id"] for x in r.get("items", [])]
    return deleted


def restore_blocks(base: str, cred: str, entries: list[dict],
                   to_doc: str | None = None) -> list[str]:
    """Put journaled blocks back, in original order, at their original spots.

    Restores via the markdown form of POST /blocks: the captured markdown is the
    RENDERED text ('- [ ] thing' for a task), and re-parsing it reproduces the
    original block type rather than double-prefixing it.

    Two ordering details that are easy to get wrong:
      * Restore in ORIGINAL DOCUMENT ORDER, not the order the ids were typed on
        the command line, or a multi-block undo comes back shuffled.
      * When two adjacent blocks were deleted together, the second one's anchor
        is the first one — an id that no longer exists. Remap each anchor to the
        block's freshly restored id, else the second insert 404s.
    """
    ordered = sorted(entries, key=lambda e: e.get("index") if e.get("index") is not None else 1 << 30)
    remap: dict[str, str] = {}
    new_ids = []
    for e in ordered:
        md = (e.get("markdown") or "").strip()
        if not md:
            continue
        anchor = e.get("prev_sibling")
        anchor = remap.get(anchor, anchor)
        if anchor:
            position = {"position": "after", "siblingId": anchor}
        else:
            target = to_doc or e.get("doc_id")
            if target:
                position = {"position": "start", "pageId": target}
            elif e.get("date"):
                position = {"position": "end", "date": e["date"]}
            else:
                raise CraftError(f"nowhere to restore {e['id']} — pass --to <docId>")
        r = req("POST", f"{base}/blocks", cred, json_body={"markdown": md, "position": position})
        made = [x["id"] for x in r.get("items", [])]
        new_ids += made
        if made:
            e["_restored_as"] = made[-1]
            remap[e["id"]] = made[-1]
    return new_ids
