"""T-01 / S1: the fixture provider works without network or environment access.

The oracle is planning/plan.md F-01, read against IT-001's message and policy.
These provider tests assert no extraction or routing implementation.
"""

import json
import os
import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REPLY = ROOT / "tests/mock_responses/it-001-complete.json"

# Hand-authored from F-01, before any provider implementation or measured output.
EXPECTED_FACTS = {
    "request_id": "IT-001",
    "request_date": "2026-10-22",
    "kind": "new",
    "requester": "Maya Chen",
    "team": "Analytics",
    "system": "Atlas",
    "environment": "staging",
    "permission": "read",
    "business_purpose": (
        "I'm checking the weekly conversion dashboard with the new event "
        "definition changes (before next reporting cycle)."
    ),
    "start_date": "2026-10-23",
    "end_date": "2026-11-06",
    "manager": "Priya Shah",
    "supporting_quotes": [
        "Maya Chen from Analytics here, with request IT-001 dated 2026-10-22.",
        "Can I get new read access on Atlas in staging from 2026-10-23 to 2026-11-06?",
        "My manager is Priya Shah.",
        "I'm checking the weekly conversion dashboard with the new event "
        "definition changes (before next reporting cycle).",
    ],
    "issues": [],
}


class ProviderUnavailable(RuntimeError):
    """Named injected failure from the T-01 plan; never a real service error."""


class ObservedEvent(threading.Event):
    """Tell the test when a provider reaches the release barrier."""

    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


class MockProviderT01(unittest.TestCase):
    def setUp(self):
        self.message = json.loads(
            (ROOT / "materials/requests/IT-001.json").read_text(encoding="utf-8")
        )["message"]
        # Block the boundaries before importing the future implementation, too.
        for target in (
            "socket.socket",
            "socket.create_connection",
            "socket.getaddrinfo",
            "os.getenv",
            "os._Environ.__getitem__",
            "os._Environ.get",
            "os._Environ.__contains__",
        ):
            guard = patch(target, side_effect=AssertionError(
                f"T-01 forbids network/environment access: {target}"
            ))
            guard.start()
            self.addCleanup(guard.stop)

    def provider(self, **kwargs):
        from access_intake.providers.mock import MockProvider

        return MockProvider(**kwargs)

    def test_t01_saved_reply_and_call_recording_are_deterministic(self):
        expected_reply = json.loads(REPLY.read_text(encoding="utf-8"))
        expected_text = expected_reply["output"][0]["content"][0]["text"]
        self.assertEqual(json.loads(expected_text), EXPECTED_FACTS)
        for quote in EXPECTED_FACTS["supporting_quotes"]:
            self.assertIn(quote, self.message)

        provider = self.provider(responses={"IT-001.json": REPLY})
        for _ in range(2):
            self.assertEqual(
                provider.extract("IT-001.json", self.message), expected_reply
            )
        self.assertEqual(provider.calls, [
            ("IT-001.json", self.message), ("IT-001.json", self.message)
        ])

    def test_t01_selected_error_is_raised_and_recorded(self):
        failure = ProviderUnavailable("T-01 injected provider failure")
        provider = self.provider(responses={"IT-001.json": failure})
        with self.assertRaises(ProviderUnavailable) as caught:
            provider.extract("IT-001.json", self.message)
        self.assertIs(caught.exception, failure)
        self.assertEqual(provider.calls, [("IT-001.json", self.message)])

    def test_t01_delayed_reply_waits_without_blocking_another_call(self):
        release = ObservedEvent()
        provider = self.provider(
            responses={"delayed": REPLY, "ready": REPLY},
            release_events={"delayed": release},
        )
        expected_reply = json.loads(REPLY.read_text(encoding="utf-8"))
        # Aliases select the same source fixture; they are not document-ID claims.
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(provider.extract, "delayed", self.message)
            try:
                self.assertTrue(release.waiting.wait(timeout=2), "no delay barrier")
                self.assertFalse(pending.done())
                self.assertEqual(
                    provider.extract("ready", self.message), expected_reply
                )
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2), expected_reply)
        self.assertEqual(provider.calls, [
            ("delayed", self.message), ("ready", self.message)
        ])

    def test_t01_offline_guards_are_active(self):
        # Verify guards themselves without inspecting the host environment.
        with self.assertRaisesRegex(AssertionError, "forbids network"):
            socket.create_connection(("example.invalid", 443))
        with self.assertRaisesRegex(AssertionError, "forbids network/environment"):
            os.environ.get("T01_FORBIDDEN_ENVIRONMENT_LOOKUP")
        provider = self.provider(responses={"IT-001.json": REPLY})
        self.assertEqual(
            provider.extract("IT-001.json", self.message),
            json.loads(REPLY.read_text(encoding="utf-8")),
        )
