"""Deterministic saved Responses envelopes. No environment or network access."""
import copy
import json
from pathlib import Path
import threading


class MockProvider:
    def __init__(self, responses=None, release_events=None, fixtures=None):
        if responses is None:
            responses = json.loads((Path(fixtures)/'corpus-completed.json').read_text(encoding='utf-8'))
        self.responses = dict(responses)
        self.release_events = dict(release_events or {})
        self.calls = []
        self._lock = threading.Lock()

    def extract(self, document_id, message):
        with self._lock:
            self.calls.append((document_id, message))
            reply = self.responses[document_id]
        if document_id in self.release_events:
            self.release_events[document_id].wait()
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, (str, Path)):
            return json.loads(Path(reply).read_text(encoding='utf-8'))
        return copy.deepcopy(reply)
