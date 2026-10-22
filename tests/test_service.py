import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("service", Path(__file__).resolve().parents[1] / "install-service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ServiceTests(unittest.TestCase):
    @patch.object(service.sys, "platform", "darwin")
    def test_framework_app_preserves_virtual_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "Python3.framework/Versions/3.9/bin/python3.9"
            binary.parent.mkdir(parents=True)
            binary.touch()
            app = binary.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
            app.parent.mkdir(parents=True)
            app.touch()
            python = root / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(binary)
            self.assertEqual(service.python_runtime(python), (
                app.resolve(), {"__PYVENV_LAUNCHER__": str(python)},
            ))

    @patch.object(service.sys, "platform", "darwin")
    def test_python_without_app_keeps_original_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "bin/python"
            self.assertEqual(service.python_runtime(python), (python, {}))

    @patch.object(service.time, "sleep")
    def test_bootstrap_success_is_insufficient(self, sleep):
        status = subprocess.CompletedProcess([], 0, "\tstate = spawn scheduled\n\tlast exit code = 1\n", "")
        with patch.object(service.subprocess, "run", return_value=status) as run:
            with self.assertRaisesRegex(SystemExit, "non resta attivo"):
                service.verify_service("gui/501/com.promptx.listener")
        self.assertEqual(run.call_args.args[0][1], "bootout")

    @patch.object(service.time, "sleep")
    def test_bootstrap_is_retried_while_launchd_unloads_the_old_job(self, sleep):
        results = [subprocess.CompletedProcess([], 5), subprocess.CompletedProcess([], 0)]
        with patch.object(service.subprocess, "run", side_effect=results) as run:
            service.bootstrap("gui/501", Path("job.plist"))
        self.assertEqual(run.call_count, 2)

    @patch.object(service.time, "sleep")
    def test_bootstrap_gives_up_with_a_message(self, sleep):
        with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 5)):
            with self.assertRaisesRegex(SystemExit, "non riesce ad avviare"):
                service.bootstrap("gui/501", Path("job.plist"))

    def test_update_pulls_installs_checks_then_restarts(self):
        project, python = Path("/p"), Path("/p/.venv/bin/python")
        with patch.object(service.subprocess, "run") as run, patch.object(service, "verify_service") as verify:
            service.update(project, python, python, {}, "gui/501/job")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[0:3] for command in commands], [
            ["git", "-C", "/p"], ["/p/.venv/bin/python", "-m", "pip"],
            ["/p/.venv/bin/python", "/p/prompt-listener.py", "--check"], ["launchctl", "kickstart", "-k"],
        ])
        verify.assert_called_once_with("gui/501/job")

    def test_failed_check_keeps_the_running_service(self):
        project, python = Path("/p"), Path("/p/.venv/bin/python")
        failure = subprocess.CalledProcessError(1, "check")
        with patch.object(service.subprocess, "run", side_effect=[None, None, failure]) as run, \
                patch.object(service, "verify_service"):
            with self.assertRaises(subprocess.CalledProcessError):
                service.update(project, python, python, {}, "gui/501/job")
        self.assertEqual(run.call_count, 3)

    @patch.object(service.time, "sleep")
    def test_running_service_is_checked_over_time(self, sleep):
        status = subprocess.CompletedProcess([], 0, "\tstate = running\n", "")
        with patch.object(service.subprocess, "run", return_value=status) as run:
            service.verify_service("gui/501/com.promptx.listener")
        self.assertEqual(run.call_count, 5)


if __name__ == "__main__":
    unittest.main()
