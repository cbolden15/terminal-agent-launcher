from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from terminal_agent_launcher.feedback import (
    FeedbackBusyError,
    FeedbackError,
    FeedbackStore,
    ReceiptStore,
    advisory_lock,
    make_route_receipt,
    normalize_task_v1,
)


class FeedbackTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.feedback_path = self.base / "data" / "terminal-agent-launcher" / "routing" / "feedback.jsonl"
        self.receipts_path = self.base / "state" / "terminal-agent-launcher" / "routing" / "receipts.jsonl"
        self.project = {"id": "project-billing", "name": "Billing", "path": "/tmp/billing"}
        self.now = datetime(2026, 9, 18, 21, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def receipt(self, receipt_id: str, *, terminal: str = "/dev/ttys001", age: timedelta = timedelta(0)):
        return make_route_receipt(
            "fix billing",
            self.project,
            {"score": 12.4, "margin": 2.0, "matched_terms": ["billing"]},
            receipt_id=receipt_id,
            terminal_id=terminal,
            created_at=self.now - age,
        )

    def test_normalize_task_v1_preserves_punctuation_and_order(self) -> None:
        self.assertEqual(
            normalize_task_v1("  Fix\u00a0BILLING: API   v2!  "),
            "fix billing: api v2!",
        )
        with self.assertRaisesRegex(FeedbackError, "must be a string"):
            normalize_task_v1(None)  # type: ignore[arg-type]

    def test_reducer_tracks_teach_then_revoke_without_rewriting_history(self) -> None:
        store = FeedbackStore(self.feedback_path)
        correction = store.append_teach("Fix billing", self.project, event_id="teach-1", created_at=self.now)
        self.assertEqual(store.active_corrections(), {"teach-1": correction})

        store.append_revoke("teach-1", event_id="revoke-1", created_at=self.now + timedelta(seconds=1))

        self.assertEqual(store.active_corrections(), {})
        events = store.events()
        self.assertEqual([event["event"] for event in events], ["teach", "revoke"])
        self.assertEqual(events[0]["task"], "Fix billing")

    def test_failed_atomic_feedback_write_preserves_existing_history(self) -> None:
        store = FeedbackStore(self.feedback_path)
        store.append_teach("Fix billing", self.project, event_id="teach-1", created_at=self.now)
        before = self.feedback_path.read_bytes()

        with patch(
            "terminal_agent_launcher.feedback.os.replace",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaisesRegex(FeedbackError, "atomically write"):
                store.append_teach(
                    "Fix invoices",
                    self.project,
                    event_id="teach-2",
                    created_at=self.now + timedelta(seconds=1),
                )

        self.assertEqual(self.feedback_path.read_bytes(), before)
        self.assertEqual([event["event_id"] for event in store.events()], ["teach-1"])

    def test_reducer_rejects_duplicate_unknown_and_invalid_revoke_transitions(self) -> None:
        store = FeedbackStore(self.feedback_path)
        store.append_teach("Fix billing", self.project, event_id="teach-1", created_at=self.now)
        with self.assertRaisesRegex(FeedbackError, "Duplicate feedback event ID"):
            store.append_teach("Fix notes", self.project, event_id="teach-1", created_at=self.now)
        with self.assertRaisesRegex(FeedbackError, "unknown feedback ID"):
            store.append_revoke("missing", event_id="revoke-missing", created_at=self.now)
        store.append_revoke("teach-1", event_id="revoke-1", created_at=self.now)
        with self.assertRaisesRegex(FeedbackError, "not active"):
            store.append_revoke("teach-1", event_id="revoke-2", created_at=self.now)

    def test_malformed_and_unsupported_feedback_data_fails_closed(self) -> None:
        self.feedback_path.parent.mkdir(parents=True)
        self.feedback_path.write_text("not json\n", encoding="utf-8")
        with self.assertRaisesRegex(FeedbackError, "Malformed JSONL"):
            FeedbackStore(self.feedback_path).events()

        self.feedback_path.write_text(
            json.dumps({"schema_version": 2, "event": "teach"}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(FeedbackError, "Unsupported feedback schema version"):
            FeedbackStore(self.feedback_path).events()

    def test_store_directories_files_and_locks_are_owner_only(self) -> None:
        FeedbackStore(self.feedback_path).append_teach("Fix billing", self.project)
        ReceiptStore(self.receipts_path).record(self.receipt("receipt-1"))

        for directory in (self.feedback_path.parent.parent, self.feedback_path.parent, self.receipts_path.parent.parent, self.receipts_path.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for file_path in (
            self.feedback_path,
            self.feedback_path.with_name("feedback.jsonl.lock"),
            self.receipts_path,
            self.receipts_path.with_name("receipts.jsonl.lock"),
        ):
            self.assertEqual(stat.S_IMODE(file_path.stat().st_mode), 0o600)

    def test_store_rejects_symlinked_data_file_and_directory(self) -> None:
        self.feedback_path.parent.mkdir(parents=True)
        target = self.base / "outside.jsonl"
        target.write_text("[]", encoding="utf-8")
        self.feedback_path.symlink_to(target)
        with self.assertRaisesRegex(FeedbackError, "must not be a symlink"):
            FeedbackStore(self.feedback_path).events()

        routing_directory = self.receipts_path.parent
        routing_directory.parent.mkdir(parents=True)
        routing_directory.symlink_to(self.base / "outside-routing", target_is_directory=True)
        with self.assertRaisesRegex(FeedbackError, "must not be a symlink"):
            ReceiptStore(self.receipts_path).receipts()

    def test_advisory_lock_fails_after_its_bounded_wait(self) -> None:
        lock_path = self.feedback_path.with_name("feedback.jsonl.lock")
        lock_path.parent.mkdir(parents=True)
        lock_path.touch(mode=0o600)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, pathlib, sys, time; "
                "f = pathlib.Path(sys.argv[1]).open('r+'); "
                "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(2)",
                str(lock_path),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaises(FeedbackBusyError):
                with advisory_lock(self.feedback_path, timeout_seconds=0):
                    pass
        finally:
            holder.terminate()
            holder.wait(timeout=5)
            holder.stdout.close()

    def test_receipt_store_keeps_only_its_bounded_newest_records(self) -> None:
        store = ReceiptStore(self.receipts_path, max_receipts=2)
        store.record(self.receipt("one", age=timedelta(minutes=3)))
        store.record(self.receipt("two", age=timedelta(minutes=2)))
        store.record(self.receipt("three", age=timedelta(minutes=1)))

        self.assertEqual([receipt.receipt_id for receipt in store.receipts()], ["two", "three"])

    def test_last_receipt_requires_matching_terminal_and_age_under_fifteen_minutes(self) -> None:
        store = ReceiptStore(self.receipts_path)
        store.record(self.receipt("other", terminal="/dev/ttys002", age=timedelta(seconds=10)))
        store.record(self.receipt("expired", age=timedelta(minutes=15)))
        store.record(self.receipt("current", age=timedelta(minutes=14, seconds=59)))

        self.assertEqual(store.last_for_terminal("/dev/ttys001", now=self.now).receipt_id, "current")
        self.assertIsNone(store.last_for_terminal("/dev/ttys999", now=self.now))


if __name__ == "__main__":
    unittest.main()
