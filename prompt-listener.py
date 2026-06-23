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
  Mac:    runs /x-prompt automatically and replies with the created files
"""

import json
import os
import re
import shutil
import subprocess
import sys
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

ENV = {
    **os.environ,
    "PATH": os.environ.get("PATH", "") + ":/usr/local/bin:/usr/bin:/bin",
}


def run_pipeline(url: str) -> str:
    """Run optimized extraction pipeline (66% cheaper than /x-prompt)."""
    try:
        result = subprocess.run(
            ["python3", str(PROJECT_DIR / "optimize-pipeline.py"), url],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min (first run of easyocr downloads models, slow)
            env=ENV,
        )
        if result.returncode != 0:
            return f"❌ Error: {result.stderr[:200]}"

        # Parse JSON output and extract summary
        try:
            output = json.loads(result.stdout)
            topic = output.get("topic", "?")
            count = output.get("prompts_extracted", 0)
            return f"✅ Fatto. {count} prompt in `{topic}`"
        except:
            # Fallback if JSON parsing fails
            return "✅ Fatto"
    except subprocess.TimeoutExpired:
        return "⏱ Timeout (10 min). Se è il primo post, i modelli OCR stavano scaricando. Riprova."
    except FileNotFoundError:
        return "❌ optimize-pipeline.py not found"
    except Exception as e:
        return f"❌ Error: {str(e)[:100]}"


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
        bot.reply_to(msg, f"✅ Done!\n\n{output}")

    print(f"Bot started. Project: {PROJECT_DIR}")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    main()
