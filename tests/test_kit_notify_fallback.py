"""kit-notify must still reach Alex when the iMessage bridge is down.

After a Mini reboot the kit account has no GUI session, so kit-imsg-send fails
and every alert routed through it vanished (9/19-10/16: 7 reboot/freeze events,
5+ lost texts). The fallback is Kit's own mailbox, which runs as alex.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "kit-notify"


def load():
    loader = importlib.machinery.SourceFileLoader("kit_notify", str(SCRIPT))
    spec = importlib.util.spec_from_loader("kit_notify", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def kn(tmp_path, monkeypatch):
    module = load()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"transport": "imsg"}))
    monkeypatch.setattr(module, "KIT_CONFIG", cfg)
    return module


def test_email_fallback_when_imessage_fails(kn, monkeypatch):
    emailed: list[str] = []

    def broken(text: str) -> None:
        raise RuntimeError("kit-imsg-send exited 1: connect ENOENT")

    monkeypatch.setattr(kn, "_send_imsg", broken)
    monkeypatch.setattr(kn, "_send_email", lambda text, reason: emailed.append(text))
    kn.send("Daily Briefing failed")
    assert emailed == ["Daily Briefing failed"]


def test_no_email_when_imessage_works(kn, monkeypatch):
    emailed: list[str] = []
    monkeypatch.setattr(kn, "_send_imsg", lambda text: None)
    monkeypatch.setattr(kn, "_send_email", lambda text, reason: emailed.append(text))
    kn.send("hello")
    assert emailed == []


def test_raises_when_both_channels_fail(kn, monkeypatch):
    def broken(*args) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(kn, "_send_imsg", broken)
    monkeypatch.setattr(kn, "_send_email", broken)
    with pytest.raises(RuntimeError):
        kn.send("hello")
