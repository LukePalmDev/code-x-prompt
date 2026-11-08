#!/usr/bin/env python3
"""
prompt-listener.py — Telegram bot that queues X post URLs and processes them.

Setup:
  1. Create a bot via @BotFather → get the token
  2. Copy .env.example to .env and set TELEGRAM_BOT_TOKEN
  3. pip install -r requirements-bot.txt
  4. python3 install-service.py --install

Daily use:
  iPhone: X post → Share → Telegram → [bot name]
  Mac:    the link is saved in a persistent queue, processed by pipeline.py,
          and the bot replies with the result. /stato checks every component.

Manual run (same pipeline, no Telegram):
  .venv/bin/python prompt-listener.py --process <url>

Local provider probe (no Telegram, no fetch, no output files):
  .venv/bin/python prompt-listener.py --smoke-provider

Read-only counts for jobs archived in .cache/failed (no writes, no Telegram):
  .venv/bin/python prompt-listener.py --retry-failed

Requeue those jobs for the running service, preserving chat and message ids:
  .venv/bin/python prompt-listener.py --retry-failed --execute
"""

import argparse
import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
# Tests set PROMPTX_SKIP_DOTENV=1 so a checkout that contains the service .env
# is not read. The LaunchAgent does not set this variable.
if load_dotenv is not None and os.environ.get("PROMPTX_SKIP_DOTENV") != "1":
    load_dotenv(Path(__file__).parent / ".env")

import telebot

import pipeline

log = logging.getLogger("promptx")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PROJECT_DIR = pipeline.PROJECT_DIR
QUEUE_DIR = pipeline.CACHE_DIR / "queue"
FAILED_DIR = pipeline.CACHE_DIR / "failed"
# Original failed records moved here only after the queue copy has been verified.
AUDIT_DIR = pipeline.CACHE_DIR / "failed-audit"
STATE_FILE = pipeline.CACHE_DIR / "state.json"
LOG_FILE = PROJECT_DIR / "logs" / "listener.log"

X_URL_RE = re.compile(r'https?://(?:twitter\.com|x\.com)/\S+/status/\d+\S*')

# Delays before retrying a job that failed for a transient reason.
RETRY_LATER = (15 * 60, 60 * 60, 3 * 60 * 60)
IDLE_POLL_SECONDS = 60
FXTWITTER_PROBE = "https://api.fxtwitter.com/2/status/20"

# A process cut off from Telegram with the network up is wedged: exit so launchd restarts it.
WATCHDOG_INTERVAL = 60
STALL_RESTART_SECONDS = 15 * 60
TELEGRAM_ADDRESS = ("api.telegram.org", 443)

# macOS flags files evicted by iCloud; a launchd job cannot read them (EDEADLK).
SF_DATALESS = 0x40000000
SYNCED_FOLDERS = ("Documents", "Desktop", "Library/Mobile Documents")

state_lock = threading.Lock()


class SuppressRepeats(logging.Filter):
    """During an outage telebot logs a traceback every few seconds: keep one per interval."""

    def __init__(self, interval: int = 600):
        super().__init__()
        self.interval = interval
        self.last_logged = {}

    def filter(self, record: logging.LogRecord) -> bool:
        message, now = record.getMessage(), time.time()
        if now - self.last_logged.get(message, 0) < self.interval:
            return False
        self.last_logged[message] = now
        return True


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    # Telegram long-polling timeouts are routine and recover by themselves.
    file_handler.addFilter(lambda record: "Read timed out" not in record.getMessage())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), file_handler],
    )
    logging.getLogger("TeleBot").addFilter(SuppressRepeats())


def redact(text: str) -> str:
    """Request errors quote the URL, which contains the bot token."""
    return text.replace(BOT_TOKEN, "***") if BOT_TOKEN else text


# ─── persistent state ─────────────────────────────────────────────────────────

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def update_state(**changes) -> dict:
    with state_lock:
        state = read_json(STATE_FILE, {})
        state.update(changes)
        pipeline.write_json(STATE_FILE, state)
        return state


# ─── queue ────────────────────────────────────────────────────────────────────

def job_path(url: str) -> Path:
    return QUEUE_DIR / f"{pipeline.tweet_id(url)}.json"


def enqueue(url: str, chat_id: int, message_id: int) -> bool:
    """Persist the job before acknowledging it; False if already queued."""
    path = job_path(url)
    if path.exists():
        return False
    pipeline.write_json(path, {
        "url": url, "chat_id": chat_id, "message_id": message_id,
        "received": time.time(), "rounds": 0, "retry_at": 0,
    })
    return True


def queued_jobs() -> list:
    jobs = [(read_json(path, None), path) for path in QUEUE_DIR.glob("*.json")]
    return sorted(((job, path) for job, path in jobs if job), key=lambda item: item[0]["received"])


def next_job():
    now = time.time()
    return next(((job, path) for job, path in queued_jobs() if job["retry_at"] <= now), (None, None))


def archive_failed(path: Path) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    os.replace(path, FAILED_DIR / path.name)


def _telegram_int(value, allow_negative: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if value == 0 or (value < 0 and not allow_negative):
        return False
    return True


def failed_identity(path: Path, job):
    """Return the tweet id when the archived job can be retried on its original route.

    chat_id and message_id must already be in the file. Nothing here invents a chat.
    """
    if path.is_symlink() or not path.is_file() or not isinstance(job, dict):
        return None
    url = job.get("url")
    if not isinstance(url, str) or X_URL_RE.search(url) is None:
        return None
    try:
        tweet = pipeline.tweet_id(url)
    except pipeline.PipelineError:
        return None
    if path.name != f"{tweet}.json":
        return None
    if not _telegram_int(job.get("chat_id"), allow_negative=True):
        return None
    if not _telegram_int(job.get("message_id"), allow_negative=False):
        return None
    received = job.get("received")
    if isinstance(received, bool) or not isinstance(received, (int, float)):
        return None
    return tweet


def queue_matches(path: Path, payload: dict) -> bool:
    loaded = read_json(path, None)
    if not isinstance(loaded, dict):
        return False
    return (
        loaded.get("url") == payload["url"]
        and loaded.get("chat_id") == payload["chat_id"]
        and loaded.get("message_id") == payload["message_id"]
        and loaded.get("rounds") == 0
        and loaded.get("retry_at") == 0
    )


def same_route(existing: dict, job: dict) -> bool:
    return (
        existing.get("url") == job.get("url")
        and existing.get("chat_id") == job.get("chat_id")
        and existing.get("message_id") == job.get("message_id")
    )


def _json_files(directory: Path) -> list:
    if not directory.is_dir():
        return []
    files = [path for path in directory.glob("*.json") if path.is_symlink() or path.is_file()]
    return sorted(files, key=lambda path: path.name)


def _audit_destination(failed_path: Path) -> Path:
    """A new path. Existing audit files are never replaced."""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    primary = AUDIT_DIR / failed_path.name
    if not primary.exists():
        return primary
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    candidate = AUDIT_DIR / f"{failed_path.stem}.{stamp}.json"
    number = 2
    while candidate.exists():
        candidate = AUDIT_DIR / f"{failed_path.stem}.{stamp}.{number}.json"
        number += 1
    return candidate


def _park_original(failed_path: Path) -> None:
    os.replace(failed_path, _audit_destination(failed_path))


def _empty_retry_counts() -> dict:
    return {key: 0 for key in (
        "queue", "queue_ready", "queue_delayed", "failed", "audit",
        "eligible", "accepted", "audit_completed",
        "skipped_queued", "skipped_processed", "skipped_invalid", "skipped_conflict",
        "rolled_back", "errors",
    )}


def _snapshot_counts(counts: dict) -> None:
    now = time.time()
    jobs = queued_jobs()
    ready = sum(1 for job, _path in jobs if job.get("retry_at", 0) <= now)
    counts["queue"] = len(jobs)
    counts["queue_ready"] = ready
    counts["queue_delayed"] = len(jobs) - ready
    counts["failed"] = len(_json_files(FAILED_DIR))
    counts["audit"] = len(_json_files(AUDIT_DIR))


def _retry_record(path: Path, processed: dict, execute: bool, counts: dict, lines: list) -> None:
    if path.is_symlink() or not path.is_file():
        counts["skipped_invalid"] += 1
        lines.append(f"skip-invalid {path.name}")
        return
    job = read_json(path, None)
    tweet = failed_identity(path, job)
    if tweet is None:
        counts["skipped_invalid"] += 1
        lines.append(f"skip-invalid {path.name}")
        return
    if tweet in processed:
        counts["skipped_processed"] += 1
        lines.append(f"skip-processed {tweet}")
        return
    queue_path = QUEUE_DIR / path.name
    if queue_path.exists():
        existing = read_json(queue_path, None)
        if not (isinstance(existing, dict) and same_route(existing, job)):
            counts["skipped_conflict"] += 1
            lines.append(f"skip-conflict {tweet}")
            return
        if not execute:
            counts["skipped_queued"] += 1
            lines.append(f"skip-queued {tweet} chat={job['chat_id']} message={job['message_id']}")
            return
        try:
            _park_original(path)
        except OSError as exc:
            counts["errors"] += 1
            lines.append(f"error {tweet} {exc}")
            return
        counts["audit_completed"] += 1
        lines.append(f"audit-completed {tweet} chat={job['chat_id']} message={job['message_id']}")
        return
    counts["eligible"] += 1
    if not execute:
        lines.append(f"eligible {tweet} chat={job['chat_id']} message={job['message_id']}")
        return
    payload = dict(job)
    payload["rounds"] = 0
    payload["retry_at"] = 0
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        pipeline.write_json(queue_path, payload)
    except OSError as exc:
        counts["errors"] += 1
        lines.append(f"error {tweet} {exc}")
        return
    if not queue_matches(queue_path, payload):
        try:
            queue_path.unlink()
        except OSError as exc:
            counts["errors"] += 1
            lines.append(f"error {tweet} {exc}")
            return
        counts["rolled_back"] += 1
        lines.append(f"rollback {tweet}")
        return
    try:
        _park_original(path)
    except OSError as exc:
        # The queue file is already valid. Leave the failed record for a later
        # execute, which only parks it (audit_completed) and does not rewrite the queue.
        counts["errors"] += 1
        lines.append(f"error {tweet} {exc}")
        return
    counts["accepted"] += 1
    lines.append(f"accepted {tweet} chat={job['chat_id']} message={job['message_id']}")


def retry_failed(execute: bool = False) -> dict:
    """Count or requeue `.cache/failed` jobs.

    The default is read-only. `execute=True` copies a job into the queue and
    only then moves the original file to `.cache/failed-audit`. The copy keeps
    url, chat_id, message_id, received and any saved extraction. rounds and
    retry_at restart at 0. Already queued, already processed, or invalid
    records are not rewritten. This function does not send Telegram or call
    the model; the running service delivers the original chat.
    """
    def run():
        counts = _empty_retry_counts()
        lines = []
        processed = pipeline.load_processed()
        for path in _json_files(FAILED_DIR):
            _retry_record(path, processed, execute, counts, lines)
        _snapshot_counts(counts)
        return {"mode": "execute" if execute else "dry-run", "counts": counts, "lines": lines}

    if not execute:
        return run()
    with pipeline.exclusive_lock():
        return run()


def format_retry_report(report: dict) -> str:
    order = (
        "queue", "queue_ready", "queue_delayed", "failed", "audit",
        "eligible", "accepted", "audit_completed",
        "skipped_queued", "skipped_processed", "skipped_invalid", "skipped_conflict",
        "rolled_back", "errors",
    )
    lines = [f"mode={report['mode']}"]
    lines.extend(f"{key}={report['counts'][key]}" for key in order)
    lines.extend(report["lines"])
    return "\n".join(lines)


def retry_failed_cli(execute: bool) -> int:
    report = retry_failed(execute=execute)
    print(format_retry_report(report), flush=True)
    if report["counts"]["errors"] or report["counts"]["rolled_back"]:
        return 1
    return 0


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send(bot, chat_id, text: str, reply_to=None) -> bool:
    """A Telegram hiccup must neither drop the job nor hide its result."""
    reply = telebot.types.ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None
    for delay in (0, 5, 15, 60):
        time.sleep(delay)
        try:
            bot.send_message(chat_id, text[:4000], reply_parameters=reply)
            return True
        except Exception as exc:
            log.warning("Telegram send failed: %s", redact(str(exc)))
    return False


def notify_owner(bot, text: str) -> None:
    owner = read_json(STATE_FILE, {}).get("owner_chat_id")
    if owner:
        send(bot, owner, text)


@contextlib.contextmanager
def system_awake():
    """Prevent idle sleep from aborting Claude mid-run (a closed lid still sleeps)."""
    try:
        keeper = subprocess.Popen(["/usr/bin/caffeinate", "-i"])
    except OSError:
        keeper = None
    try:
        yield
    finally:
        if keeper:
            keeper.terminate()
            keeper.wait()


def handle_job(bot, job: dict, path: Path) -> None:
    url = job["url"]
    log.info("Processing %s (round %d)", url, job["rounds"] + 1)
    try:
        with system_awake():
            result = pipeline.process(job, save=lambda current: pipeline.write_json(path, current))
    except pipeline.PipelineError as exc:
        if exc.transient and job["rounds"] < len(RETRY_LATER):
            delay = RETRY_LATER[job["rounds"]]
            job.update(rounds=job["rounds"] + 1, retry_at=time.time() + delay, last_error=str(exc))
            pipeline.write_json(path, job)
            retry_time = time.strftime("%H:%M", time.localtime(job["retry_at"]))
            log.warning("Job %s postponed to %s: %s", url, retry_time, exc)
            send(bot, job["chat_id"], f"⚠️ Non riuscito per ora:\n{exc}\n\nRiprovo da solo alle {retry_time}.", job["message_id"])
        else:
            log.error("Job %s failed: %s", url, exc)
            archive_failed(path)
            update_state(last_failure={"time": time.time(), "url": url, "error": str(exc)})
            send(bot, job["chat_id"], f"❌ Elaborazione non riuscita:\n{exc}", job["message_id"])
    except Exception as exc:
        # A bug: keep the job for inspection and say so instead of going silent.
        log.exception("Unexpected error on %s", url)
        archive_failed(path)
        update_state(last_failure={"time": time.time(), "url": url, "error": repr(exc)})
        send(bot, job["chat_id"], f"❌ Errore interno: {exc!r}\nDettagli in {LOG_FILE}", job["message_id"])
    else:
        path.unlink()
        update_state(last_success={"time": time.time(), "url": url, "result": result})
        send(bot, job["chat_id"], f"✅ {result}", job["message_id"])


def worker(bot, wake: threading.Event) -> None:
    while True:
        try:
            job, path = next_job()
            if job is None:
                wake.wait(IDLE_POLL_SECONDS)
                wake.clear()
                continue
            handle_job(bot, job, path)
        except Exception:
            log.exception("Worker loop error")
            time.sleep(IDLE_POLL_SECONDS)


# ─── health checks ────────────────────────────────────────────────────────────

def provider_label() -> str:
    try:
        if pipeline.provider_settings()["provider"] == "ollama":
            return "Ollama"
    except pipeline.PipelineError:
        return "Provider"
    return "Claude"


def check_provider():
    """Check the configured provider only. Ollama never falls through to Claude."""
    try:
        settings = pipeline.provider_settings()
    except pipeline.PipelineError as exc:
        raise ValueError(str(exc)) from None
    if settings["provider"] == "ollama":
        try:
            return pipeline.check_ollama(settings)
        except pipeline.PipelineError as exc:
            raise ValueError(str(exc)) from None
    return check_claude()


def check_claude():
    try:
        result = subprocess.run(
            [pipeline.CLAUDE_CMD, "auth", "status"], capture_output=True, text=True,
            timeout=30, env=pipeline.ENV,
        )
    except FileNotFoundError:
        raise ValueError(
            "Claude CLI non trovato. Installa con: npm install -g @anthropic-ai/claude-code"
        ) from None
    try:
        status = json.loads(result.stdout or "{}")
    except ValueError:
        detail = (result.stderr or result.stdout or "risposta vuota").strip()
        raise ValueError(f"claude auth status non ha restituito JSON: {detail[:300]}") from None
    if result.returncode or not status.get("loggedIn"):
        raise ValueError("non autenticato: esegui claude auth login")
    return "autenticato"


def check_output():
    directory = pipeline.output_directory()
    if not directory.is_dir():
        raise ValueError(f"cartella assente: {directory}")
    if not os.access(directory, os.W_OK):
        raise ValueError(f"cartella non scrivibile: {directory}")
    return f"{sum(path.is_dir() for path in directory.iterdir())} cartelle"


def check_fxtwitter():
    request = urllib.request.Request(FXTWITTER_PROBE, headers={"User-Agent": "code-x-prompt/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        if json.load(response).get("code") != 200:
            raise ValueError("risposta inattesa")
    return "raggiungibile"


def check_runtime():
    """The service reads code, venv and config at any time: none of it may live in iCloud."""
    home = Path.home()
    synced = next((home / name for name in SYNCED_FOLDERS if home / name in PROJECT_DIR.parents), None)
    if synced:
        raise ValueError(f"il progetto è in {synced}, sincronizzata con iCloud: fai girare il servizio da un clone fuori, per esempio ~/Developer/code-x-prompt")
    evicted = 0
    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [name for name in dirs if name != ".git"]
        evicted += sum(
            bool(getattr(os.lstat(os.path.join(root, name)), "st_flags", 0) & SF_DATALESS)
            for name in files
        )
    if evicted:
        raise ValueError(f"{evicted} file del progetto sono solo su iCloud e il servizio non può leggerli")
    return "tutti i file sono locali"


def run_checks(checks) -> list:
    results = []
    for label, check in checks:
        try:
            results.append((True, label, check()))
        except Exception as exc:
            results.append((False, label, str(exc) or type(exc).__name__))
    return results


def format_time(timestamp) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(timestamp))


def status_report() -> str:
    lines = [
        f"{'✅' if ok else '❌'} {label}: {detail}"
        for ok, label, detail in run_checks([
            (provider_label(), check_provider), ("Output", check_output), ("X/FxTwitter", check_fxtwitter),
            ("Runtime", check_runtime),
        ])
    ]
    state = read_json(STATE_FILE, {})
    jobs = queued_jobs()
    lines.append(f"🕐 Attivo dal {format_time(state.get('started', time.time()))}")
    lines.append(f"📥 Coda: {len(jobs)} link")
    for job, _ in jobs:
        if job["retry_at"] > time.time():
            lines.append(f"   ↻ {job['url']} alle {format_time(job['retry_at'])}")
    if state.get("last_success"):
        lines.append(f"✔️ Ultimo successo {format_time(state['last_success']['time'])}: {state['last_success']['result']}")
    if state.get("last_failure"):
        failure = state["last_failure"]
        lines.append(f"✖️ Ultimo errore {format_time(failure['time'])}: {failure['error'][:300]}")
    lines.append(f"📄 Log: {LOG_FILE}")
    return "\n".join(lines)


def check_token():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN non configurato")
    return "configurato"


def check_setup() -> bool:
    """Read-only startup diagnostics; never print the bot token."""
    results = run_checks([
        ("Telegram", check_token), (provider_label(), check_provider), ("Output", check_output), ("Runtime", check_runtime),
    ])
    for ok, label, detail in results:
        print(f"{'OK ' if ok else 'ERR'} {label}: {detail}", file=sys.stdout if ok else sys.stderr, flush=True)
    return all(ok for ok, _, _ in results)


# ─── watchdog ─────────────────────────────────────────────────────────────────

def network_up() -> bool:
    try:
        socket.create_connection(TELEGRAM_ADDRESS, timeout=10).close()
    except OSError:
        return False
    return True


def watchdog(bot) -> None:
    """Report Telegram outages once and restart a process that stays cut off with the network up."""
    failing_since = error = None
    connected = False
    while True:
        try:
            bot.get_me()
        except Exception as exc:
            failing_since = failing_since or time.time()
            description = redact(f"{type(exc).__name__}: {exc}")
            if description != error:
                error = description
                log.warning("Telegram unreachable: %s", error)
            if time.time() - failing_since > STALL_RESTART_SECONDS and network_up():
                log.critical("Telegram unusable for %d min with the network up: restarting", STALL_RESTART_SECONDS // 60)
                os._exit(1)
        else:
            if failing_since or not connected:
                log.info("Telegram reachable")
            failing_since = error = None
            connected = True
        time.sleep(WATCHDOG_INTERVAL)


# ─── entry points ─────────────────────────────────────────────────────────────

def smoke_provider() -> int:
    """Probe local structured output. Never constructs the Telegram bot."""
    try:
        settings = pipeline.provider_settings()
    except pipeline.PipelineError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    if settings["provider"] != "ollama":
        print(
            "Provider claude: smoke locale non eseguito. Nessuna chiamata a Claude o Telegram.",
            file=sys.stderr,
        )
        return 2
    try:
        extraction = pipeline.smoke_local_extraction()
    except pipeline.PipelineError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    print(
        f"Smoke Ollama ok: {len(extraction['prompts'])} prompt, topic {extraction['topic']!r}, "
        f"modello {settings['model']}, num_ctx {settings['num_ctx']}, think {str(settings['think']).lower()}.",
        flush=True,
    )
    return 0


def process_once(url: str) -> int:
    try:
        print(pipeline.process({"url": url}))
    except pipeline.PipelineError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    return 0


def run_bot() -> None:
    bot = telebot.TeleBot(BOT_TOKEN)
    wake = threading.Event()

    previous = read_json(STATE_FILE, {})
    crashed = previous.get("running", False)
    update_state(running=True, started=time.time())

    def stop(signum, frame):
        update_state(running=False)
        log.info("Stopped by signal %d", signum)
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def remember_owner(msg):
        if not read_json(STATE_FILE, {}).get("owner_chat_id"):
            update_state(owner_chat_id=msg.chat.id)

    @bot.message_handler(commands=["start", "help"])
    def cmd_help(msg):
        remember_owner(msg)
        send(bot, msg.chat.id,
             "Mandami un link X/Twitter e salvo i prompt in inglese e italiano.\n\n"
             "Esempio:\nhttps://x.com/username/status/123456789\n\n"
             "/stato controlla che tutto funzioni.", msg.message_id)

    @bot.message_handler(commands=["stato", "status"])
    def cmd_status(msg):
        remember_owner(msg)
        send(bot, msg.chat.id, status_report(), msg.message_id)

    @bot.message_handler(func=lambda msg: True)
    def handle_message(msg):
        remember_owner(msg)
        urls = X_URL_RE.findall((msg.text or "").strip())
        if not urls:
            send(bot, msg.chat.id, "Nessun link X valido. Esempio:\nhttps://x.com/.../status/...", msg.message_id)
            return
        url = urls[0]
        if enqueue(url, msg.chat.id, msg.message_id):
            log.info("Queued %s", url)
            send(bot, msg.chat.id, f"⏳ Ricevuto, in coda ({len(queued_jobs())}).", msg.message_id)
            wake.set()
        else:
            send(bot, msg.chat.id, "⏳ Questo post è già in coda.", msg.message_id)

    pending = len(queued_jobs())
    log.info("Bot started. Project: %s. Queue: %d. Provider: %s", PROJECT_DIR, pending, provider_label())
    for ok, label, detail in run_checks([("Runtime", check_runtime)]):
        if not ok:
            log.error("%s: %s", label, detail)
    if crashed:
        log.warning("Previous run ended unexpectedly")
        notify_owner(bot, f"🔄 Il bot è ripartito dopo un arresto imprevisto.\nLink in coda: {pending}.\nLog: {LOG_FILE}")
    elif pending:
        notify_owner(bot, f"🔄 Bot avviato: riprendo {pending} link in coda.")

    threading.Thread(target=worker, args=(bot, wake), daemon=True, name="worker").start()
    threading.Thread(target=watchdog, args=(bot,), daemon=True, name="watchdog").start()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Verifica configurazione senza avviare il bot")
    parser.add_argument("--process", metavar="URL", help="Elabora un link senza Telegram")
    parser.add_argument(
        "--smoke-provider",
        action="store_true",
        help="Prova il provider locale senza Telegram, senza fetch e senza scrivere file",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Conta i job in .cache/failed. Senza --execute non scrive nulla",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Con --retry-failed, elenco e conteggi senza scrivere",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Con --retry-failed, accetta i job originali in coda",
    )
    args = parser.parse_args()
    selected = [args.check, bool(args.process), args.smoke_provider, args.retry_failed]
    if sum(selected) > 1:
        parser.error("Scegli un solo comando tra --check, --process, --smoke-provider e --retry-failed")
    if args.dry_run and not args.retry_failed:
        parser.error("--dry-run richiede --retry-failed")
    if args.execute and not args.retry_failed:
        parser.error("--execute richiede --retry-failed")
    if args.dry_run and args.execute:
        parser.error("Scegli --dry-run oppure --execute")
    setup_logging()
    if args.check:
        sys.exit(0 if check_setup() else 1)
    if args.smoke_provider:
        sys.exit(smoke_provider())
    if args.retry_failed:
        sys.exit(retry_failed_cli(execute=args.execute))
    if args.process:
        sys.exit(process_once(args.process))
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set: create a bot via @BotFather and put the token in .env")
        sys.exit(1)
    try:
        run_bot()
    except Exception:
        log.exception("Bot crashed")
        raise


if __name__ == "__main__":
    main()
