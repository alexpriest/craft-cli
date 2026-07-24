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
import urllib.error
import urllib.request
from pathlib import Path

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_ENV_FILE = Path(__file__).resolve().parent / ".env"

# A divider. There is NO {"type":"divider"} — that 400s. A text block whose
# markdown is "---" comes back as {"type":"line"}.
DIVIDER = {"type": "text", "markdown": "---", "indentationLevel": 0}


class CraftError(Exception):
    pass


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
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise CraftError(f"{method} {url.split('?')[0]} -> {e.code}: {detail}")


def append_tasks(base: str, cred: str, date: str, tasks: list[str], *, separator: bool = True) -> list[str]:
    """Append checkbox tasks to a daily note (auto-creates the note), divider first."""
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
