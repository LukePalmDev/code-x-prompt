import contextlib
import importlib.util
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
spec = importlib.util.spec_from_file_location("listener", PROJECT / "prompt-listener.py")
listener = importlib.util.module_from_spec(spec)
spec.loader.exec_module(listener)

URL = "https://x.com/author/status/123"


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value in (
            ("QUEUE_DIR", root / "queue"),
            ("FAILED_DIR", root / "failed"),
            ("STATE_FILE", root / "state.json"),
            ("system_awake", contextlib.nullcontext),
        ):
            patcher = patch.object(listener, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch.object(listener.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.bot = Mock()

    def queued(self):
        listener.enqueue(URL, 42, 7)
        return listener.next_job()

    def handle(self, outcome):
        job, path = self.queued()
        with patch.object(listener.pipeline, "process", side_effect=[outcome]):
            listener.handle_job(self.bot, job, path)
        return path, self.bot.send_message.call_args.args[1]

    def test_duplicate_link_is_queued_once(self):
        self.assertTrue(listener.enqueue(URL, 42, 7))
        self.assertFalse(listener.enqueue(URL.replace("x.com", "twitter.com"), 42, 8))
        self.assertEqual(len(listener.queued_jobs()), 1)

    def test_success_removes_job_and_replies(self):
        path, reply = self.handle("Fatto. 2 prompt in 'Scrittura'.")
        self.assertFalse(path.exists())
        self.assertEqual(reply, "✅ Fatto. 2 prompt in 'Scrittura'.")
        self.assertEqual(json.loads(listener.STATE_FILE.read_text())["last_success"]["url"], URL)

    def test_transient_failure_is_retried_later(self):
        path, reply = self.handle(listener.pipeline.PipelineError("Connection error", transient=True))
        job = json.loads(path.read_text())
        self.assertEqual(job["rounds"], 1)
        self.assertGreater(job["retry_at"], time.time())
        self.assertIn("Riprovo da solo", reply)
        self.assertEqual(listener.next_job(), (None, None))

    def test_transient_failure_gives_up_after_all_rounds(self):
        listener.enqueue(URL, 42, 7)
        path = listener.job_path(URL)
        job = json.loads(path.read_text())
        job["rounds"] = len(listener.RETRY_LATER)
        with patch.object(listener.pipeline, "process", side_effect=listener.pipeline.PipelineError("down", transient=True)):
            listener.handle_job(self.bot, job, path)
        self.assertFalse(path.exists())
        self.assertTrue((listener.FAILED_DIR / path.name).exists())
        self.assertTrue(self.bot.send_message.call_args.args[1].startswith("❌"))

    def test_permanent_failure_is_archived_and_reported(self):
        path, reply = self.handle(listener.pipeline.PipelineError("Nessun prompt trovato nel post."))
        self.assertFalse(path.exists())
        self.assertTrue((listener.FAILED_DIR / path.name).exists())
        self.assertIn("Nessun prompt", reply)

    def test_unexpected_error_is_reported_not_silent(self):
        _, reply = self.handle(KeyError("boom"))
        self.assertIn("Errore interno", reply)

    def test_progress_saved_by_pipeline_survives_restart(self):
        job, path = self.queued()

        def interrupted(current, save):
            current["extraction"] = {"topic": "T", "prompts": []}
            save(current)
            raise listener.pipeline.PipelineError("Connection lost", transient=True)

        with patch.object(listener.pipeline, "process", side_effect=interrupted):
            listener.handle_job(self.bot, job, path)
        self.assertIn("extraction", json.loads(path.read_text()))

    def test_send_retries_after_telegram_error(self):
        self.bot.send_message.side_effect = [ConnectionError("Read timed out"), None]
        self.assertTrue(listener.send(self.bot, 42, "ok", 7))
        self.assertEqual(self.bot.send_message.call_count, 2)

    def test_setup_check_reports_logged_out_claude(self):
        result = subprocess.CompletedProcess([], 1, '{"loggedIn":false}', "")
        with patch.object(listener, "BOT_TOKEN", "test"), patch.object(listener.subprocess, "run", return_value=result), \
                patch.object(listener, "check_output", return_value="ok"):
            self.assertFalse(listener.check_setup())


class RuntimeTests(unittest.TestCase):
    def test_project_in_icloud_folder_is_rejected(self):
        project = Path.home() / "Documents" / "code-x-prompt"
        with patch.object(listener, "PROJECT_DIR", project):
            with self.assertRaisesRegex(ValueError, "iCloud"):
                listener.check_runtime()

    def test_local_project_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "prompt-listener.py").touch()
            with patch.object(listener, "PROJECT_DIR", Path(directory).resolve()):
                self.assertTrue(listener.check_runtime())

    def test_evicted_files_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "cacert.pem").touch()
            evicted = Mock(st_flags=listener.SF_DATALESS)
            with patch.object(listener, "PROJECT_DIR", Path(directory).resolve()), \
                    patch.object(listener.os, "lstat", return_value=evicted):
                with self.assertRaisesRegex(ValueError, "1 file .* solo su iCloud"):
                    listener.check_runtime()

    def test_missing_claude_explains_how_to_install(self):
        with patch.object(listener.subprocess, "run", side_effect=FileNotFoundError(2, "No such file", "claude")):
            with self.assertRaisesRegex(ValueError, "Claude CLI non trovato"):
                listener.check_claude()

    def test_non_json_auth_status_is_reported(self):
        result = subprocess.CompletedProcess([], 0, "not-json", "")
        with patch.object(listener.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "non ha restituito JSON"):
                listener.check_claude()

    def test_token_is_redacted_from_errors(self):
        with patch.object(listener, "BOT_TOKEN", "123:secret"):
            self.assertEqual(listener.redact("url /bot123:secret/getMe"), "url /bot***/getMe")

    def test_repeated_telebot_failure_is_logged_once(self):
        suppress = listener.SuppressRepeats()
        record = logging.LogRecord("TeleBot", logging.ERROR, __file__, 0, "Infinity polling exception", None, None)
        self.assertTrue(suppress.filter(record))
        self.assertFalse(suppress.filter(record))
        suppress.last_logged.clear()
        self.assertTrue(suppress.filter(record))


class WatchdogTests(unittest.TestCase):
    class StopLoop(Exception):
        pass

    def setUp(self):
        self.bot = Mock()
        self.bot.get_me.side_effect = ConnectionError("down")
        for target, value in (
            ("STALL_RESTART_SECONDS", -1),
            ("network_up", Mock(return_value=True)),
        ):
            patcher = patch.object(listener, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        exit_patch = patch.object(listener.os, "_exit", side_effect=SystemExit)
        self.exit = exit_patch.start()
        self.addCleanup(exit_patch.stop)

    def run_watchdog(self, sleeps):
        with patch.object(listener.time, "sleep", side_effect=sleeps):
            listener.watchdog(self.bot)

    def test_restarts_when_telegram_stays_down_with_network_up(self):
        with self.assertRaises(SystemExit):
            self.run_watchdog([None, None])
        self.exit.assert_called_once_with(1)

    def test_waits_while_network_is_down(self):
        listener.network_up.return_value = False
        with self.assertRaises(self.StopLoop):
            self.run_watchdog([None, self.StopLoop()])
        self.exit.assert_not_called()

    def test_recovery_cancels_the_restart(self):
        self.bot.get_me.side_effect = [ConnectionError("down"), Mock(), ConnectionError("down"), Mock()]
        with patch.object(listener, "STALL_RESTART_SECONDS", 3600), self.assertRaises(self.StopLoop):
            self.run_watchdog([None, None, None, self.StopLoop()])
        self.exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
