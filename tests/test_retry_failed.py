import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
os.environ["PROMPTX_SKIP_DOTENV"] = "1"
spec = importlib.util.spec_from_file_location("listener_retry", PROJECT / "prompt-listener.py")
listener = importlib.util.module_from_spec(spec)
spec.loader.exec_module(listener)

URLS = {
    "111": "https://x.com/one/status/111",
    "222": "https://x.com/two/status/222",
    "333": "https://x.com/three/status/333",
}


def job(tweet, chat_id, message_id, **extra):
    body = {
        "url": URLS[tweet],
        "chat_id": chat_id,
        "message_id": message_id,
        "received": 1000 + int(tweet),
        "rounds": 3,
        "retry_at": 9_999_999_999,
        "last_error": "hit your limit",
    }
    body.update(extra)
    return body


class RetryFailedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.queue = root / "queue"
        self.failed = root / "failed"
        self.audit = root / "audit"
        self.failed.mkdir()
        self.processed = root / "processed.json"
        self.lock = root / "pipeline.lock"
        for target, value in (
            ("QUEUE_DIR", self.queue),
            ("FAILED_DIR", self.failed),
            ("AUDIT_DIR", self.audit),
        ):
            patcher = patch.object(listener, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (
            ("PROCESSED_INDEX", self.processed),
            ("LOCK_FILE", self.lock),
        ):
            patcher = patch.object(listener.pipeline, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, owner in (
            ("TeleBot", listener.telebot),
            ("process", listener.pipeline),
        ):
            patcher = patch.object(owner, target, side_effect=AssertionError(target))
            patcher.start()
            self.addCleanup(patcher.stop)
        live = patch.object(listener.pipeline.urllib.request, "urlopen", side_effect=AssertionError("ollama"))
        live.start()
        self.addCleanup(live.stop)
        claude = patch.object(listener.subprocess, "run", side_effect=AssertionError("claude"))
        claude.start()
        self.addCleanup(claude.stop)

    def write_failed(self, tweet, chat_id, message_id, **extra):
        path = self.failed / f"{tweet}.json"
        path.write_text(json.dumps(job(tweet, chat_id, message_id, **extra)))
        return path

    def counts(self, execute=False):
        return listener.retry_failed(execute=execute)["counts"]

    def test_dry_run_lists_three_and_writes_nothing(self):
        paths = [self.write_failed(tweet, 4242, index) for index, tweet in enumerate(("111", "222", "333"), start=7)]
        before = [path.read_bytes() for path in paths]
        report = listener.retry_failed(execute=False)
        text = listener.format_retry_report(report)
        self.assertTrue(text.startswith("mode=dry-run\n"))
        counts = report["counts"]
        self.assertEqual(counts["queue"], 0)
        self.assertEqual(counts["failed"], 3)
        self.assertEqual(counts["audit"], 0)
        self.assertEqual(counts["eligible"], 3)
        self.assertEqual(counts["accepted"], 0)
        self.assertIn("eligible 111 chat=4242 message=7", report["lines"])
        self.assertIn("eligible 333 chat=4242 message=9", report["lines"])
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.audit.exists())
        self.assertFalse(self.lock.exists())

    def test_execute_accepts_three_original_routes_once(self):
        extraction = {"topic": "Note", "prompts": [{"title_en": "t", "prompt_en": "p", "title_it": "t", "prompt_it": "p"}]}
        original = self.write_failed("111", 4242, 7, extraction=extraction, author="someone")
        self.write_failed("222", 4242, 8)
        self.write_failed("333", 5151, 9)
        original_bytes = original.read_bytes()
        first = self.counts(execute=True)
        self.assertEqual(first["eligible"], 3)
        self.assertEqual(first["accepted"], 3)
        self.assertEqual(first["queue"], 3)
        self.assertEqual(first["queue_ready"], 3)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(first["audit"], 3)
        self.assertEqual((self.audit / "111.json").read_bytes(), original_bytes)
        queued = json.loads((self.queue / "111.json").read_text())
        self.assertEqual(queued["chat_id"], 4242)
        self.assertEqual(queued["message_id"], 7)
        self.assertEqual(queued["url"], URLS["111"])
        self.assertEqual(queued["received"], 1111)
        self.assertEqual(queued["rounds"], 0)
        self.assertEqual(queued["retry_at"], 0)
        self.assertEqual(queued["author"], "someone")
        self.assertEqual(queued["extraction"], extraction)
        self.assertEqual(queued["last_error"], "hit your limit")
        queue_bytes = sorted(path.read_bytes() for path in self.queue.glob("*.json"))
        second = self.counts(execute=True)
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(second["eligible"], 0)
        self.assertEqual(second["queue"], 3)
        self.assertEqual(second["audit"], 3)
        self.assertEqual(second["failed"], 0)
        self.assertEqual(sorted(path.read_bytes() for path in self.queue.glob("*.json")), queue_bytes)

    def test_already_queued_job_is_not_rewritten(self):
        self.write_failed("111", 4242, 7)
        self.queue.mkdir()
        queued = job("111", 4242, 7)
        queued["rounds"] = 2
        queued["extraction"] = {"topic": "Kept", "prompts": []}
        (self.queue / "111.json").write_text(json.dumps(queued))
        before = (self.queue / "111.json").read_bytes()
        counts = self.counts(execute=True)
        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(counts["audit_completed"], 1)
        self.assertEqual((self.queue / "111.json").read_bytes(), before)
        self.assertFalse((self.failed / "111.json").exists())
        self.assertTrue((self.audit / "111.json").is_file())

    def test_processed_and_conflicting_records_stay_in_failed(self):
        processed_path = self.write_failed("111", 4242, 7)
        conflict = self.write_failed("222", 4242, 8)
        processed_before = processed_path.read_bytes()
        conflict_before = conflict.read_bytes()
        self.processed.write_text(json.dumps({"111": "Fatto. 1 prompt in 'T'."}))
        self.queue.mkdir()
        (self.queue / "222.json").write_text(json.dumps(job("222", 9999, 8)))
        counts = self.counts(execute=True)
        self.assertEqual(counts["skipped_processed"], 1)
        self.assertEqual(counts["skipped_conflict"], 1)
        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(processed_path.read_bytes(), processed_before)
        self.assertEqual(conflict.read_bytes(), conflict_before)
        self.assertFalse(self.audit.exists())
        self.assertEqual(json.loads((self.queue / "222.json").read_text())["chat_id"], 9999)

    def test_invalid_route_is_not_replaced_with_a_fake_chat(self):
        missing = self.failed / "111.json"
        body = job("111", 4242, 7)
        del body["chat_id"]
        missing.write_text(json.dumps(body))
        wrong_name = self.failed / "not-the-id.json"
        wrong_name.write_text(json.dumps(job("222", 4242, 8)))
        self.failed.joinpath("333.json").symlink_to(missing)
        before = [missing.read_bytes(), wrong_name.read_bytes()]
        report = listener.retry_failed(execute=True)
        self.assertEqual(report["counts"]["skipped_invalid"], 3)
        self.assertEqual(report["counts"]["accepted"], 0)
        self.assertFalse(self.queue.exists())
        self.assertEqual([missing.read_bytes(), wrong_name.read_bytes()], before)

    def test_failed_queue_write_leaves_the_original(self):
        path = self.write_failed("111", 4242, 7)
        before = path.read_bytes()
        with patch.object(listener.pipeline, "write_json", side_effect=OSError("disk")):
            counts = self.counts(execute=True)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.audit.exists())
        self.assertEqual(list(self.queue.glob("*.json")), [])

    def test_verify_failure_rolls_the_queue_copy_back(self):
        path = self.write_failed("111", 4242, 7)
        before = path.read_bytes()
        with patch.object(listener, "queue_matches", return_value=False):
            counts = self.counts(execute=True)
        self.assertEqual(counts["rolled_back"], 1)
        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.queue.glob("*.json")), [])
        self.assertFalse(self.audit.exists())

    def test_cli_defaults_to_read_only_and_rejects_mixed_flags(self):
        with patch.object(sys, "argv", ["prompt-listener.py", "--retry-failed"]), \
                patch.object(listener, "setup_logging"), \
                patch.object(listener, "retry_failed_cli", return_value=0) as cli:
            with self.assertRaises(SystemExit) as raised:
                listener.main()
        self.assertEqual(raised.exception.code, 0)
        cli.assert_called_once_with(execute=False)
        with patch.object(sys, "argv", ["prompt-listener.py", "--retry-failed", "--execute"]), \
                patch.object(listener, "setup_logging"), \
                patch.object(listener, "retry_failed_cli", return_value=0) as execute:
            with self.assertRaises(SystemExit) as raised:
                listener.main()
        execute.assert_called_once_with(execute=True)
        with patch.object(sys, "argv", ["prompt-listener.py", "--retry-failed", "--dry-run", "--execute"]), \
                patch.object(listener, "setup_logging", side_effect=AssertionError("logged")):
            with self.assertRaises(SystemExit) as raised:
                listener.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
