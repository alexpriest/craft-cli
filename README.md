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
craft new "<title>" [--folder ID] [--stdin]   # --stdin: body markdown, one block per line
craft edit <blockId> "<markdown>"        # edit a block in place
craft rm <docId>...                      # soft-delete to trash (batches ≤40, verifies)
craft tasks <YYYY-MM-DD> "<text>"...     # append checkbox tasks to a daily note
craft tag <blockId> <name>...            # append #tags to a block (idempotent)
```

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
  search for `#kit` + a diff against `~/.local/state/craft-kit-watch/seen.json`. On a **new**
  `#kit` block it dispatches a headless Kit worker (`claude -p --permission-mode auto`, run from
  the Chief-of-Staff folder for the Kit persona). First run seeds silently; if the worker can't
  run, it falls back to texting via `kit-notify` so nothing is dropped.
- **The worker decides comms with judgment** — texts Alex (via `kit-notify`) only if the outcome
  is notable/blocked/time-sensitive/outward; otherwise it handles the item, annotates the Craft
  block, and removes the `#kit` tag for async review.
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
