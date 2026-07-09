#!/usr/bin/env python3
"""
prompt-listener.py — Telegram bot that receives X post URLs and runs the full pipeline.

Setup:
  1. Create a bot via @BotFather → get the token
  2. Copy .env.example to .env and set TELEGRAM_BOT_TOKEN
  3. pip install -r requirements.txt
  4. python3 prompt-listener.py

Daily use:
  iPhone: X post → Share → Telegram → [bot name]
  Mac:    runs the workflow in CLAUDE.md and replies with the created files
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import telebot

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PROJECT_DIR = Path(__file__).parent

CLAUDE_CMD = shutil.which("claude") or "claude"

X_URL_RE = re.compile(r'https?://(?:twitter\.com|x\.com)/\S+/status/\d+\S*')

MAX_PIPELINE_ATTEMPTS = 3
RETRY_DELAYS = (5, 15)
TRANSIENT_ERROR_MARKERS = (
    "connection closed",
    "connection reset",
    "network error",
    "overloaded",
    "rate limit",
    "request timed out",
    "service unavailable",
    "socket hang up",
    "status code 429",
    "status code 500",
    "status code 502",
    "status code 503",
    "status code 529",
)

ENV = {
    **os.environ,
    "PATH": os.environ.get("PATH", "") + ":/usr/local/bin:/usr/bin:/bin",
}


def is_transient_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in TRANSIENT_ERROR_MARKERS)


def run_pipeline(url: str) -> str:
    """Run the project workflow via Claude CLI."""
    prompt = (
        "Segui rigorosamente le istruzioni nel file CLAUDE.md di questo progetto. "
        "Elabora il seguente link X ed esegui l'intero workflow, inclusa la "
        "creazione dei file nella output_dir configurata. Se un tentativo precedente "
        "ha già creato una cartella o alcuni file per lo stesso contenuto, riprendi e "
        "completa quella cartella senza crearne una duplicata e senza sovrascrivere "
        f"file già completi:\n{url}"
    )

    try:
        print(f"Processing URL: {url}", flush=True)
        last_error = "Errore sconosciuto"

        for attempt in range(1, MAX_PIPELINE_ATTEMPTS + 1):
            command = [CLAUDE_CMD, "--dangerously-skip-permissions"]
            if attempt > 1:
                command.extend(["--model", "sonnet"])
            command.extend(["-p", prompt])

            try:
                result = subprocess.run(
                    command,
                    cwd=PROJECT_DIR,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=ENV,
                )
            except subprocess.TimeoutExpired:
                last_error = "Il tentativo ha superato 10 minuti."
                transient = True
            else:
                if result.returncode == 0:
                    output = (result.stdout or "").strip()
                    print(f"Pipeline completed on attempt {attempt}.", flush=True)
                    return output[:4000] if output else "Pipeline completata."

                last_error = (
                    result.stderr or result.stdout or "Errore sconosciuto"
                ).strip()
                transient = is_transient_error(last_error)

            print(
                f"Pipeline attempt {attempt}/{MAX_PIPELINE_ATTEMPTS} failed: "
                f"{last_error}",
                flush=True,
            )

            if not transient or attempt == MAX_PIPELINE_ATTEMPTS:
                break

            delay = RETRY_DELAYS[attempt - 1]
            print(f"Retrying in {delay} seconds...", flush=True)
            time.sleep(delay)

        attempt_label = "tentativo" if attempt == 1 else "tentativi"
        return (
            f"❌ Pipeline non riuscita dopo {attempt} {attempt_label}:\n"
            f"{last_error[:3700]}"
        )
    except FileNotFoundError:
        return "❌ Claude CLI non trovato. Installa con: npm install -g @anthropic-ai/claude-code"
    except Exception as e:
        return f"❌ Errore: {e}"


def main():
    if not BOT_TOKEN:
        print(
            "Error: TELEGRAM_BOT_TOKEN not set.\n"
            "Create a bot via @BotFather, then:\n"
            "  export TELEGRAM_BOT_TOKEN='your-token'\n"
            "or put it in a .env file in the same directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    bot = telebot.TeleBot(BOT_TOKEN)

    @bot.message_handler(commands=["start", "help"])
    def cmd_help(msg):
        bot.reply_to(
            msg,
            "Hi! Send me an X/Twitter link and I'll extract the prompts automatically.\n\n"
            "Example:\nhttps://x.com/username/status/123456789",
        )

    @bot.message_handler(func=lambda msg: True)
    def handle_message(msg):
        text = (msg.text or "").strip()
        urls = X_URL_RE.findall(text)

        if not urls:
            bot.reply_to(
                msg,
                "No valid X link found. Send a URL like:\nhttps://x.com/.../status/...",
            )
            return

        url = urls[0]
        bot.reply_to(msg, f"⏳ Processing post...\n{url}")

        output = run_pipeline(url)
        if output.startswith(("❌", "⏱")):
            bot.reply_to(msg, output)
        else:
            bot.reply_to(msg, f"✅ Fatto!\n\n{output}")

    print(f"Bot started. Project: {PROJECT_DIR}")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    main()
