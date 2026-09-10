# craft-cli

`craft` — a thin, token-frugal CLI over the **Craft Docs REST API**. The default way
any shell session (Claude Code, scripts) reads or mutates Craft. Compact output by
default; `--json` for raw.

This is **separate from `craft-mirror`** on purpose: the mirror is a one-way
Craft→Obsidian sync job (launchd), this is a general read/write client. They share
nothing at runtime — `craft-cli` vendors its own small API client (`craftapi.py`) so
it's fully independent. (The mirror keeps its own copy in `craft_append.py`. If the
Craft API auth ever changes, update both.)

## Setup (once per machine)

```sh
./install.sh
```

Symlinks `craft` onto PATH and writes `.env` from 1Password. `.env` is gitignored
*and* Syncthing-ignored, so it's populated per machine (needs `op` signed in).

## Usage

```sh
craft ls [--location unsorted|trash|templates|daily_notes] [--folder ID]   # id + title
craft get <docId>                        # rendered markdown
craft search <term>                      # docId | snippet
craft new "<title>" [--folder ID] [--stdin]   # --stdin: body markdown from stdin
craft edit <blockId> "<markdown>"        # edit a block in place
craft append <blockId> [--stdin]         # write markdown INTO a block's page (nested children)
craft rm <docId>...                      # soft-delete to trash (batches ≤40, verifies)
craft tasks <YYYY-MM-DD> "<text>"...     # append checkbox tasks to a daily note
      [--divider auto|always|never]      # auto = ONE divider per note (see below)
craft tag <blockId> <name>...            # append #tags to a block (idempotent)
craft rm-block <blockId>...               # DELETE blocks  [--doc ID | --date D] [--dry-run] [-y]
craft undo [<stamp>] [--list] [--to ID]   # restore what the last rm-block removed
```

## Deleting blocks — real, but genuinely one-way

`DELETE /blocks {"blockIds":[…]}` has always existed (it is in the OpenAPI spec, and
craft-mirror's README documented it on 2026-07-23). A 2026-07-30 session nonetheless
asserted "Craft has no block DELETE" and worked around it by rewriting a stale block
in place. That was wrong, and it is why clutter accumulated instead of being removed.

Verified live against the API:

| Behaviour | Result |
|---|---|
| Delete prose / task / page blocks | all work |
| Bad or already-deleted id | `404`, and the call is **atomic** — one bad id in a batch deletes **nothing** |
| Passing a `documentId` as a blockId | `400 Root block cannot be deleted` — a doc can't be lost this way |
| Deleting a parent | children are **promoted**, not cascaded — nothing is silently destroyed |
| Recovery | **none.** No trash, no 30-day window (`craft rm` on a *document* does have one) |

That last row is the whole design constraint. `rm-block` therefore snapshots every
block — text, type, and its preceding sibling — into `~/.local/state/craft-cli/undo/`
*before* issuing the delete, and `craft undo` replays the snapshot. Guards: a
non-TTY caller must pass `-y` (so nothing deletes by accident inside a pipeline),
`--dry-run` prints exactly what would go, and page blocks are flagged in the preview.

Pass `--doc <id>` or `--date <YYYY-MM-DD>` whenever you know it. That is what lets
the journal record placement, so undo restores each block to its exact original
position and in original document order (including the fiddly case where two
adjacent blocks were deleted together and the second one's anchor is the first).
Without it, undo needs `--to <docId>` and drops the block at the top of the doc.

**Known fidelity limit:** indentation is not recoverable. The API doesn't return
`indentationLevel` on read, so a restored child block comes back at top level.
Restores are faithful on text, order, position, and block type (tasks come back as
tasks — the journal stores rendered markdown like `- [ ] thing` and re-parses it,
which reproduces the type rather than double-prefixing it).

## Dividers on daily notes — one per note, not one per append

Agent-written tasks sit under a `---` rule so they don't run into Alex's handwriting. That rule is
laid down **once per note**. `--divider auto` (the default) GETs the note first and adds one only
when there's content to separate from *and* no top-level divider yet — an empty note gets none, and
a note that already has one gets none, because later appends belong under the existing boundary.

Until 2026-07-29 every append added its own, so a day with four appends came out with four `***`
rules stacked through it. Alex flagged that as noise; this is the fix. `--divider always|never`
overrides. The same logic lives in `craft_append.py` (craft-mirror) — change both or neither.

## Formatting — write real markdown

`new --stdin` and `append` POST the markdown **whole** to `/blocks` and let Craft parse it into
native blocks. (Until 2026-07-24 they split input into one plain text block per line, which
flattened every answer into a wall of bold-lead paragraphs and shredded tables into loose rows.
That's why long agent write-ups looked so bad.) Two consequences: a single newline now *continues*
a block — put a blank line between paragraphs — and this path has **no 20-block cap** (40+ blocks
in one call verified).

What Craft's ingest understands, all verified 2026-07-24:

| Syntax | Becomes |
|---|---|
| `\| a \| b \|` GFM table | native `type: "table"` block with a real cell grid |
| `<callout>…</callout>` | text block with `decorations: ["callout"]` |
| `<caption>…</caption>` | `textStyle: "caption"` (small, muted) |
| `==text==` / `<highlight color="red">` | inline highlight (yellow, green, mint, cyan, blue, purple, pink, red, gray, gradient-\*) |
| `+ line` + 2-space-indented children | collapsible toggle (`listStyle: "toggle"`) |
| `#` / `##` / `###` | `textStyle` h1/h2/h3 — **use `###`**, h1/h2 render as huge headlines |
| `-` / `1.` / `>` | bullet, numbered, `decorations: ["quote"]` |
| 2 leading spaces | one nesting level |
| `[t](date://YYYY-MM-DD)` / `[t](block://id)` | daily-note link / block cross-reference |
| `<page><pageTitle>T</pageTitle><content>…</content></page>` | nested page; `<card>` = card style |

Reach for a table whenever there are 2+ things with 2+ attributes — it is the single biggest
readability win over prose. Full token reference: the `info.description` of `$BASE/openapi.json`.

⚠️ **Table blocks have no `markdown` field on read** — the content comes back as a `rows` cell
grid. Any reader that only looks at `markdown` drops tables silently; `_table_to_md` in the CLI
re-renders them as GFM.

🚨 **Craft's table cells DO NOT KEEP LINKS. `[text](https://…)` inside a `| cell |` loses the URL
and lands as plain text.** Links in prose, bullets and headings survive normally — this is
specific to table cells. Verified 2026-08-09 on a research doc: two docs built as comparison
tables with a link per row came back with **5 of ~30 and 3 of ~18 URLs** intact — the only
survivors were the handful written outside tables. Nothing errors and the table still looks
right, so this is silent and easy to ship.

**The rule: never let a table cell be the only place a URL exists.** A comparison table is still
the right shape for options × attributes — just pair it with a plain link list (`- Name ($price,
stock) — https://…`) covering every row. Put the bare URL in the list, not another
`[markdown](link)`, so a future reader can see at a glance that it's really there. Same applies
to any `craft new`/`append` payload built from research output, which is exactly where
link-per-row tables get generated.

**Tags:** the API has no tag primitive, but Craft's app renders a literal `#name` in block
markdown as a live, tappable tag (verified 2026-07-24 — `#macbook`, `#MacBook`, `#macbook-pro`
all work; `tag://`, `##name`, `#[name]`, `<tag>` do NOT). `craft tag` just appends `#name` text.
Use it to group cross-cutting items (e.g. `#macbook` on machine-specific tasks) — tapping the tag
in Craft collects them in one place.

## Creds & security

`op://Claude/Craft API/{base_url,credential}`. The **base_url is itself a secret**
(embeds the link id) — the CLI resolves it internally so it never lands on a command
line. `craftapi.creds()` reads `.env` first, then falls back to `op`.

## Landmines

Handled by the CLI; mind them if you drop to the raw API (`$BASE/openapi.json`):
`GET /documents` with no `location` returns **everything incl. trash**; the doc list
is **cached** so verify deletes via `location=trash`; `DELETE` is soft (30-day
recover) and batches ≤40; `POST /blocks` caps 20/call; rate limit ~100/min; requests
need a **browser User-Agent** (Cloudflare 403s python-urllib otherwise). The full
hard-won API notes live in `../craft-mirror/README.md`.

A Craft **MCP** covers Claude **desktop/mobile** on the same space; `craft` is the
Claude Code path.

## `#kit` automation — event-driven pickup (kit-watch)

Modeled on the Kit iMessage flow: a cheap poll is the *receiver*, an agent is the *worker*,
and the LLM only runs on a real event.

- **`kit-watch.py`** — runs on a launchd StartInterval (every 5 min). Zero-token: one Craft
  search for `#kit` + a diff against `~/.local/state/craft-kit-watch/seen.json`. Candidate blocks
  are re-fetched for their real markdown (search snippets strip backticks, so a note that merely
  *mentions* `` `#kit` `` doesn't false-trigger). On a **new** live-tagged `#kit` block it
  dispatches a headless Kit worker (`claude -p --permission-mode auto`, from the Chief-of-Staff
  folder for the Kit persona), **detached** — the poll returns immediately and items run in
  parallel. First run seeds silently.
- **The worker responds inside the block** — writes its answer as nested children of the tagged
  block via `craft append` (Alex opens the block to read it), **swaps `#kit` for `#review`**, and
  stays quiet. It texts via `kit-notify` only when blocked, time-sensitive, needs a decision, or it
  took an outward/irreversible action.
- **`#review` is the completion signal** (added 2026-07-28). Dropping `#kit` stops the block
  re-triggering, but on its own it made finished work *invisible*: a completed item looked identical
  to one nobody had started, and Alex had no way to find what had been answered. `#review` is his
  inbox of done work — `craft search "#review"`. Every finished item gets it, including ones the
  worker only acknowledged or judged to need no action. **Alex clears it himself**; the worker never
  removes it. Safe by construction: `_has_kit_tag()` matches `#kit` as a whole tag only, so `#review`
  cannot re-trigger the watcher (a bare `#kit` in appended prose still can — backtick it).
- **`kit-notify`** — the single place Blooio is touched (sends Alex a plain-text iMessage). **Blooio
  is being retired for a local send approach (in progress); swap the body of `kit-notify`'s
  `send()` and nothing else changes.**

**Headless MCP availability:** the claude.ai connectors are remote HTTP/SSE servers with cached
OAuth (see `claude mcp list`), so a headless `claude` reaches them the same as an interactive
session — Linear, Duckbill, Slack, Granola, Gmail, Google Calendar, Google Drive, Strava, Oura,
Readwise, Typefully, MyMind — plus all the local stdio servers (Kit Tools, Kit/Alex Email, Resy,
Monarch, Cronometer, tally) and CLIs (`craft`, `gws`, git). The worker is **not** neutered.
Genuine exceptions: **claude-in-chrome** (browser automation needs a live Chrome — unavailable in
cron) and any server showing `! Needs authentication` in `claude mcp list` (currently Attio, Notion,
beeper — a pre-existing auth state, not headless-specific). If a task needs one of those, the worker
texts Alex rather than faking it.

**Enable (per machine):**
```sh
cp com.alexpriest.kit-watch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.alexpriest.kit-watch.plist
```
Log: `~/Library/Logs/kit-watch.log`.
