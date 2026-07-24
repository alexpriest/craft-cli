# Kit ↔ Craft: current system + the plan to fold it into the imsg daemon

**Status (2026-07-24):** The Craft `#kit` system runs **standalone** (launchd poller →
headless `claude -p` worker). It works. The **end state** is to fold Craft into the Kit
iMessage **daemon** as an input/output adapter, once Kit moves off Blooio onto self-hosted
`imsg`. This doc is the handoff for that fold-in.

Read this alongside:
- `~/Code/tools/craft-cli/README.md` — how the standalone system works today.
- `~/Obsidian/alexpriest/Claude/Output/Ad Hoc/2026-07-21 iMessage Backend — Replacing Blooio.md`
  — the plan to kill Blooio and self-host on `imsg` (Kit + Paloma, dedicated macOS users).
- `~/Code/system/imessage/src/tools/delegate.ts` — the daemon's existing headless-delegate tool
  (the pattern the Craft worker mirrors).

---

## 1. The big picture (why the fold-in is small)

The Kit iMessage daemon (`~/Code/system/imessage/`) is a **persona runtime** (Kit + Paloma):

- **Fast API brain** — `@anthropic-ai/sdk`, `messages.create`, model `claude-sonnet-5`, ~2–3s
  replies. Handles conversational turns (`src/claude.ts`).
- **`delegate` tool** (`src/tools/delegate.ts`) — when a task "needs real reasoning or a
  specialist," the brain spawns **headless Claude Code** (`claude --print --permission-mode
  acceptEdits --allowedTools …`), fire-and-forget, running from `~/Obsidian/alexpriest/Claude`
  so it sees the vault's `.claude/skills` + CLAUDE.md. On Alex's Max plan (not the daemon's API
  key). The delegate texts Alex the result; the daemon does NOT wait.

**The Craft `#kit` worker I built is a standalone re-implementation of `delegate`, triggered by a
Craft tag instead of an iMessage.** Same shape: fast trigger → headless Claude Code does the work.

So folding Craft into the daemon = adding a **Craft input adapter** that calls the existing
`delegate` machinery, plus a **pending-approval store** the daemon can resolve from inbound texts.
One runtime then owns both surfaces → the approval collision (below) disappears.

### The approval collision this fixes
Alex wants: for high-blast-radius actions, the worker texts him and acts only on his reply.
Standalone, that's impossible cleanly — his reply goes to the **daemon** (it owns his iMessage
inbox), not to kit-watch. Two systems can't both consume his texts. **Unified, there's one
inbox-reader:** the daemon holds the pending approval AND receives the reply.

---

## 2. What exists today (standalone) — inventory

All in `~/Code/tools/craft-cli/` (its own git repo; **separate from `craft-mirror`** on purpose —
craft-mirror is the one-way Craft→Obsidian launchd sync job):

| File | Role |
|---|---|
| `craft` | CLI over the Craft REST API. `ls / get / search / new / edit / append / rm / tasks / tag`. Compact output, `--json` for raw. |
| `craftapi.py` | Vendored low-level client: `creds()` (reads `.env` then `op`), `req()`, browser UA, `append_tasks`, `update_block`. Self-contained (no dep on craft-mirror). |
| `kit-watch.py` | launchd poller (every 5 min). Detects **new** live-`#kit` blocks; dispatches a headless worker **detached** (non-blocking). Seeds silently on first run. `--dry-run` to inspect. |
| `kit-notify` | **The single Blooio touchpoint.** Sends Alex a plain-text iMessage. Swap its `send()` body for the imsg local send later; nothing else changes. |
| `install.sh` | Per-machine: symlink `craft`+`kit-notify` onto PATH, write `.env` from `op`. |
| `com.alexpriest.kit-watch.plist` | launchd job, `StartInterval` 300, `RunAtLoad` false. Log → `~/Library/Logs/kit-watch.log`; worker output → `worker.log`. |
| `KIT-DAEMON-INTEGRATION.md` | this doc |

**State:** `~/.local/state/craft-kit-watch/seen.json` — JSON list of block IDs already dispatched.
**Creds:** 1Password item `Craft API` (vault Claude) → `op://Claude/Craft API/{base_url,credential}`.
The `base_url` embeds a secret link ID — treat the whole URL as a token, never on a command line.

### Worker behavior (encoded in `kit-watch.py::worker_prompt`)
1. **Respond INSIDE the block** — `craft append {block_id} --stdin` nests the answer as children of
   the tagged block (Alex opens the block to read it). Default output; no text needed.
2. Remove the `#kit` tag from the block's own line (verbatim otherwise — see gotcha #4).
3. **Formatting:** plain paragraphs + `**bold**` lead-ins. NO `#/##/###` headings.
4. **Proportionality:** match effort to the task; don't over-research a simple question.
5. **Approval tiers (propose-only interim gate):**
   - **Just do** (reversible/contained): answer in-block; read anything; create NEW content
     (new note/doc, draft-unsent); add a SINGLE event to any calendar incl. family/shared; edit an
     existing event with **no guests**.
   - **Propose-only** (write plan into block + `kit-notify`, do NOT act, unless the tag says
     "go ahead"): bulk calendar edits (>~2–3 / rewriting a series); editing an event **with
     guests**; editing/overwriting/deleting anything existing (vault files, Craft docs, events);
     outward sends (email/msg to third parties, comments others see, Duckbill calls); bookings,
     purchases, money; system/config/permission changes; reaching out to people.
   - **Never unattended:** financial beyond trivial, legal/medical, mass sends, mass deletion.
6. Text via `kit-notify` only if blocked / time-sensitive / needs a decision / took an
   outward-irreversible action.

### Tag conventions (also in global `~/.claude/CLAUDE.md`)
- `#kit` — for Kit to action. `#macbook` — tasks that must run on Alex's MacBook (not the Mini).
- **How to drive a tag via API:** Craft has no tag primitive; the app renders a literal `#name` in
  a block's markdown as a live tag. `craft tag <blockId> <name>` just appends `#name`. `tag://`,
  `##`, `#[]`, `<tag>` do NOT work.

---

## 3. Target architecture (after Kit is on imsg)

```
                 ┌────────────────────── Kit daemon (persona runtime, imsg) ──────────────────────┐
 iMessage  ◄────►│  imsg-rpc transport ──► fast API brain (Sonnet 5, ~2–3s)                        │
                 │                              │                                                  │
 Craft #kit ────►│  Craft adapter (poll) ──────┼──► delegate() ──► headless Claude Code (worker)   │
   (poll)        │                              │        (responds in-block via `craft append`)    │
                 │  pending-approval store ◄────┴──► inbound-text handler resolves approvals        │
                 └───────────────────────────────────────────────────────────────────────────────┘
```

- **Craft adapter** = the current `kit-watch.py` logic, moved inside the daemon: poll
  `documents/search?include=%23kit`, re-fetch each candidate's real markdown, diff vs seen state.
- On a new `#kit` block → call **`delegate`** (not a bespoke `claude -p` spawn) with the worker
  prompt. Delegate already runs headless Claude Code from the vault with a bounded tool list.
- **Approvals** live in the daemon: worker hits a propose-only action → records a pending approval
  + texts Alex → Alex replies on iMessage → the daemon's inbound handler checks pending approvals
  first and, if resolved, **re-delegates to execute the plan**; otherwise normal chat.
- **Transport-agnostic:** the Craft adapter never touches iMessage transport, so the Blooio→imsg
  swap doesn't affect it. `kit-notify` collapses into the daemon's send.

---

## 4. Fold-in steps (do these when wiring Kit on imsg)

Prereq: Kit runs on `imsg` in a **dedicated `kit` macOS user** (per the Kill-Blooio plan — bot must
not read Alex's real message history).

1. **Re-home the Craft tooling into the `kit` user.** `craft`/`craftapi.py` on PATH, `craft-cli/.env`
   populated (or `op` reachable) as `kit`, symlinks in `~kit/.local/bin`. Run `install.sh` as `kit`.
2. **Add a Craft input adapter to the daemon.** Port `kit-watch.py::find_kit_blocks` (search →
   re-fetch real markdown → `_has_kit_tag` strips code spans) + the seen-state diff. Poll on an
   interval (Craft has no webhook). Keep the state file per-user.
3. **Route new `#kit` blocks through `delegate`** instead of a standalone dispatch. Pass the worker
   prompt (§2 behavior). Decide the delegate's **tool allowlist** deliberately (see decisions §5):
   the current `delegate` allowlist is bounded and does NOT include Calendar/Monarch/Notion MCPs —
   that bounded posture is the guardrail. Add MCPs only as Alex wants those capabilities.
4. **Pending-approval store + request helper.** Worker calls a helper (e.g. `kit-approve-request
   <blockId> <summary>`) that writes a pending record and texts Alex "reply to approve." Store lives
   where the daemon can read it.
5. **Approval reader in the inbound handler.** Before normal chat, check pending approvals: if the
   inbound message resolves one (natural language interpreted by the brain, with a short code
   fallback when >1 pending), re-delegate to execute the plan the worker wrote into the block; else
   fall through to chat. This is the **non-negotiable** text-reply loop.
6. **Collapse `kit-notify` into the daemon's send** (imsg). Single swap point.
7. **Retire the standalone kit-watch** launchd job (`launchctl unload …`; delete the plist copy).

---

## 5. Decisions already made (don't re-litigate)

- **CLI over MCP for Craft** — every Craft-writing surface is a shell session; a CLI is directly
  callable, no server. (A Craft MCP exists for Alex's desktop/mobile claude.ai; that's a different
  surface.)
- **Headless Claude Code over the API loop for `#kit` work** — tasks are open-ended and need the
  full toolbox; mirrors `delegate.ts`. The API brain stays for conversational chat.
- **Respond-in-block** is the primary output (not texting) — Alex reviews answers where the question
  lives.
- **Non-blocking dispatch** — poll never stalls; items run in parallel.
- **Propose-only approval tiers** (§2.5) with a **"go ahead" escape hatch** in the tag.
- **Unify into the daemon** rather than run two systems that fight over Alex's inbox.
- **Blooio is being replaced by imsg** — `kit-notify` is the single Blooio touchpoint today.

## 6. Open items for the fold-in
- [ ] The text-reply approval loop (step 5) — the whole point; not yet built.
- [ ] Finalize the Craft-delegate **tool allowlist** / permission posture (bounded like delegate.ts,
      plus whatever MCPs Alex wants Kit to use unattended).
- [ ] Re-home to the `kit` user (paths, `.env`, `op`, symlinks).
- [ ] `kit-notify` → imsg send.
- [ ] Retire standalone kit-watch.

## 7. Gotchas / lessons banked (all cost real debugging)
- **Craft search snippets ≠ real content** — they lowercase, truncate, strip inline code, and bold
  matches. For any tag/text precision, **re-fetch the block** (`GET /blocks?id=`). This is why
  `find_kit_blocks` re-fetches candidates.
- **Backticked `` `#kit` `` is NOT a live tag** (Craft tag pane ignores it) but the API text search
  still matches the raw characters → must re-fetch + strip code spans before deciding it's a task.
- **Don't put a literal `#kit` in a reference/conventions note** — Craft turns it into a live tag.
  The "Kit & Craft — Tag Conventions" note describes tags in words for that reason.
- **Nesting children into a plain TEXT block converts it to a `page`** (a task block stays a task).
  The worker must NOT add a `#` prefix when editing the block line — that makes the question an ugly
  H1. Keep the line verbatim minus the tag.
- **`craft append` uses `position:{pageId:<block>}`**, not `parentId` (that 400s). `POST /blocks`
  caps 20/call; `DELETE /documents` caps ~40 and is soft (30-day trash); block deletes are harder to
  recover — capture text first.
- **Can't test a nested-agent dispatch from inside a Claude Code auto-mode session** — the classifier
  blocks spawning an unattended `claude -p`. Test the launchd path as Alex, or as the `kit` user.
- **`op` stalls under launchd** if it has to hit the desktop app — that's why `.env` is populated per
  machine so `creds()` doesn't shell to `op` in the background.
