"""Retry behaviour for craftapi.req.

Craft's edge returns transient 502/503/504 (hit 2026-07-30, stranding a memo that had
already cost a full transcribe + enrich). Those mean the request never reached the app,
so replaying even a non-idempotent POST cannot duplicate a block.

A bare 500 is the opposite: the app saw it and may have partially applied. Retrying that
is how you get two of something. The 500 case below is the load-bearing test — it is easy
to "fix" this by retrying every 5xx, and that fix is wrong.

Run: python3 -m unittest discover -s tests -v   (from ~/Code/tools/craft-cli)
"""

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import craftapi  # noqa: E402


def _http_error(code):
    return urllib.error.HTTPError(
        url="https://example.invalid/blocks", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(b'{"error":"boom"}'),
    )


class _OKResponse:
    def __init__(self, payload=b'{"ok":true}'):
        self._p = payload

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RetryTest(unittest.TestCase):
    def setUp(self):
        # never actually sleep during tests
        patcher = mock.patch.object(craftapi.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, side_effect):
        with mock.patch.object(craftapi.urllib.request, "urlopen") as u:
            u.side_effect = side_effect
            try:
                result = craftapi.req("POST", "https://example.invalid/blocks", "cred",
                                      json_body={"a": 1})
                return result, u.call_count, None
            except craftapi.CraftError as e:
                return None, u.call_count, e

    def test_gateway_502_is_retried_then_succeeds(self):
        result, calls, err = self._run([_http_error(502), _http_error(502), _OKResponse()])
        self.assertIsNone(err, f"should have recovered, got {err}")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, 3, "expected two retries then success")

    def test_503_and_504_and_429_are_retried(self):
        for code in (503, 504, 429):
            with self.subTest(code=code):
                result, calls, err = self._run([_http_error(code), _OKResponse()])
                self.assertIsNone(err)
                self.assertEqual(calls, 2)

    def test_500_is_NOT_retried(self):
        """A bare 500 may have partially applied — replaying it can duplicate a block."""
        _, calls, err = self._run([_http_error(500), _OKResponse()])
        self.assertIsNotNone(err, "500 must raise, not silently retry into success")
        self.assertEqual(calls, 1, "500 must not be replayed")

    def test_4xx_is_not_retried(self):
        _, calls, err = self._run([_http_error(400), _OKResponse()])
        self.assertIsNotNone(err)
        self.assertEqual(calls, 1)

    def test_error_carries_status(self):
        _, _, err = self._run([_http_error(404)])
        self.assertEqual(getattr(err, "status", None), 404,
                         "CraftError must expose .status so callers can branch on it")

    def test_network_error_is_retried(self):
        result, calls, err = self._run([urllib.error.URLError("connection reset"), _OKResponse()])
        self.assertIsNone(err)
        self.assertEqual(calls, 2)

    def test_gives_up_after_the_attempt_cap(self):
        _, calls, err = self._run([_http_error(502)] * 10)
        self.assertIsNotNone(err)
        self.assertEqual(calls, craftapi._REQ_ATTEMPTS)
        self.assertEqual(getattr(err, "status", None), 502)


if __name__ == "__main__":
    unittest.main()
