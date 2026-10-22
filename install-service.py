#!/usr/bin/env python3
"""Generate a launchd configuration; --install registers and starts it, --update deploys the code pushed to GitHub."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


def python_runtime(python: Path):
    """Use the framework app to which macOS grants the user's file access."""
    if sys.platform == "darwin":
        app = python.resolve().parent.parent / "Resources/Python.app/Contents/MacOS/Python"
        if app.is_file():
            # Preserve venv discovery when invoking its framework app directly.
            return app, {"__PYVENV_LAUNCHER__": str(python)}
    return python, {}


def bootstrap(domain: str, plist: Path):
    """Bootstrapping right after bootout can fail with an I/O error while launchd unloads the job."""
    for _ in range(5):
        if subprocess.run(["launchctl", "bootstrap", domain, str(plist)]).returncode == 0:
            return
        time.sleep(3)
    sys.exit("launchctl non riesce ad avviare il servizio: il vecchio servizio è stato fermato, riprova.")


def verify_service(target: str):
    """Detect startup failures instead of treating bootstrap as proof of health."""
    for _ in range(5):
        time.sleep(1)
        status = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
        if status.returncode or "\tstate = running\n" not in status.stdout:
            subprocess.run(["launchctl", "bootout", target], capture_output=True)
            sys.exit(
                "Il servizio non resta attivo ed è stato fermato. Controlla "
                "/tmp/promptx-listener.log. Se trovi 'Operation not permitted' "
                "su Documenti, macOS richiede un'autorizzazione privacy per Python."
            )


def run_check(project: Path, runtime: Path, environment: dict):
    subprocess.run(
        [str(runtime), str(project / "prompt-listener.py"), "--check"],
        env={**os.environ, **environment}, check=True,
    )


def update(project: Path, python: Path, runtime: Path, environment: dict, target: str):
    """Deploy the pushed code: pull, refresh dependencies, check, then restart."""
    subprocess.run(["git", "-C", str(project), "pull", "--ff-only"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", str(project / "requirements-bot.txt")], check=True)
    # A broken release must not replace the process that is still answering.
    run_check(project, runtime, environment)
    subprocess.run(["launchctl", "kickstart", "-k", target], check=True)
    verify_service(target)
    print("Servizio aggiornato e riavviato.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    python = project / ".venv/bin/python"
    if not python.exists():
        sys.exit("Crea prima .venv e installa requirements-bot.txt (vedi README.md).")
    runtime, environment = python_runtime(python)
    label = "com.promptx.listener"
    if args.update:
        update(project, python, runtime, environment, f"gui/{os.getuid()}/{label}")
        return
    config = {
        "Label": label,
        "ProgramArguments": [str(runtime), "-u", str(project / "prompt-listener.py")],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": "/tmp/promptx-listener.log",
        "StandardErrorPath": "/tmp/promptx-listener.log",
    }
    if environment:
        config["EnvironmentVariables"] = environment
    preview = project / ".cache" / f"{label}.plist"
    preview.parent.mkdir(exist_ok=True)
    preview.write_bytes(plistlib.dumps(config))
    print(f"Configurazione generata: {preview}", flush=True)
    if not args.install:
        return
    run_check(project, runtime, environment)
    target = Path.home() / "Library/LaunchAgents" / preview.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(preview.read_bytes())
    domain = f"gui/{os.getuid()}"
    status = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True)
    if status.returncode == 0:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=True)
    bootstrap(domain, target)
    verify_service(f"{domain}/{label}")
    print("Servizio installato e avviato. Log: /tmp/promptx-listener.log")


if __name__ == "__main__":
    main()
