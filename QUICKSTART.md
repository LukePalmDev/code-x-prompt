# Quick start

Extract prompts from an X post into bilingual Markdown files (English and Italian).

## Install

```bash
git clone https://github.com/LukePalmDev/code-x-prompt.git
cd code-x-prompt
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-bot.txt
cp config.yaml.example config.yaml
```

Edit `config.yaml` and set `output_dir` to a folder that already exists.

Log in to Claude Code:

```bash
npm install -g @anthropic-ai/claude-code
claude auth login
```

## Run one post

```bash
.venv/bin/python prompt-listener.py --process "https://x.com/someone/status/123456789"
```

`optimize-pipeline.py` runs the same pipeline.

## Run the Telegram bot

```bash
cp .env.example .env
```

Set `TELEGRAM_BOT_TOKEN` in `.env` (from [@BotFather](https://t.me/BotFather)), then:

```bash
.venv/bin/python prompt-listener.py --check
.venv/bin/python prompt-listener.py
```

Send an `https://x.com/.../status/...` link to the bot. macOS auto-start is in [README.md](README.md).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

No Telegram token, X credential, or network is required for the tests.
