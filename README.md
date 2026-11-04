# Prompt X

Automatically extract prompts from X (Twitter) posts and save them as bilingual Markdown files — English original + Italian translation, organized by topic.

Paste an X link into Telegram → the bot fetches the thread text, splits out each prompt, translates everything, and writes `.md` files to your configured folder. The current workflow skips images.

## How it works

```
X post URL
    │
    ▼ Telegram bot (prompt-listener.py)   persistent queue, notifications, /stato
    │
    ▼ pipeline.py                         every deterministic step
    │
    ├── fetch-x.py  →  thread text (FxTwitter; optional twscrape fallback)
    ├── Claude Code CLI  →  extraction + translation only (no tools, JSON schema)
    │
    └── writes and verifies in output_dir/
            └── {Topic}/
                ├── Inglese/   ← verbatim English prompts
                └── Italiano/  ← Italian translations
```

**Example output** for a post with 5 copywriting prompts:

```
prompt-x-output/
└── Copywriting/
    ├── Inglese/
    │   ├── 01-hook-generator.md
    │   ├── 02-cold-email-opener.md
    │   └── ...
    └── Italiano/
        ├── 01-generatore-hook.md
        ├── 02-apertura-email-fredda.md
        └── ...
```

## Prerequisites

- **Python 3.9+** for the minimal bot; **3.11+** for all optional dependencies
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — `npm install -g @anthropic-ai/claude-code`
- An **X (Twitter) account** only if the optional authenticated fallback is needed
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather) *(only to run the bot — the pipeline can also be run manually)*

## Environment

| Name | Required | Where |
|------|----------|--------|
| `TELEGRAM_BOT_TOKEN` | Bot only | `.env`, copied from `.env.example`. Do not commit `.env`. |
| `output_dir` | Always | `config.yaml`, copied from `config.yaml.example`. This is not an environment variable. |

Claude Code must be logged in (`claude auth login`). The default fetch uses the public FxTwitter API and needs no X API key, bearer token, or cookie. `twscrape` reads browser cookies only when that package is installed and the public fetch fails.

## Setup

### 1. Clone and install

```bash
git clone https://github.com/LukePalmDev/code-x-prompt.git
cd code-x-prompt
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-bot.txt
```

`requirements-bot.txt` installs only the dependencies needed by the listener
and the public fetch. `requirements.txt` adds the optional `twscrape` fallback
(`twscrape`, `browser-cookie3`).

### 2. Configure the output directory

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` and set `output_dir` to wherever you want your prompts saved:

```yaml
output_dir: "~/Documents/my-prompts"
# or an iCloud path:
# output_dir: "~/Library/Mobile Documents/com~apple~CloudDocs/AI/prompts"
```

### 3. Set up the Telegram bot

```bash
cp .env.example .env
```

Edit `.env` and add your bot token:

```
TELEGRAM_BOT_TOKEN=your-token-from-botfather
```

### 4. Check the configuration

Authenticate Claude with `claude auth login`, then check the local setup:

```bash
.venv/bin/python prompt-listener.py --check
```

This checks the token's presence, output directory and Claude login without
sending Telegram messages or running an extraction. A successful login check
does not guarantee available session quota. Public FxTwitter fetching needs no
X credentials; twscrape is used only when installed and the public fetch fails.

### 5. Start the bot

```bash
.venv/bin/python prompt-listener.py
```

Send any X post URL to your Telegram bot and the pipeline runs automatically.

## Auto-start on macOS login (optional)

Generate a configuration with the actual project and virtual environment paths:

```bash
.venv/bin/python install-service.py
# Preview: .cache/com.promptx.listener.plist
.venv/bin/python install-service.py --install
```

The installer checks the configuration, writes the LaunchAgent, and starts it
(or restarts an existing instance). It verifies that the process stays running
and stops it if startup fails. The listener finds Claude in `~/.local/bin`
and uses `.venv/bin` for workflow commands, even with launchd's minimal PATH.

```bash
launchctl print gui/$(id -u)/com.promptx.listener
tail -n 100 logs/listener.log
```

Send `/stato` to the bot to check Claude login, the output folder, FxTwitter,
the queue and the last success or error.

Reliability:

- Every link is saved in `.cache/queue/` before it is acknowledged, so a
  restart, crash or sleeping Mac never loses it; progress is saved per step.
- Transient failures (network, overload, usage limits) are retried three times
  immediately, then after 15 minutes, 1 hour and 3 hours. Permanent failures
  are moved to `.cache/failed/`. Every outcome is reported on Telegram.
- An unexpected restart is reported to the chat that first used the bot.
- A post already processed (`.cache/processed.json`) is answered without
  calling Claude; existing folders are never reused for a different post.
- Success is reported only after both languages are verified on disk.
- The log is kept in `logs/listener.log` (rotated, survives reboots).

Run the service from a clone outside folders synced by iCloud (`~/Documents`,
`~/Desktop`), for example `~/Developer/code-x-prompt`. macOS evicts the files of
those folders, `.venv` and the code included, and a background service cannot
read an evicted file (`[Errno 11] Resource deadlock avoided`): the process stays
up but Telegram, `fetch-x.py` or the CA bundle fail. `--check`, the installer
and `/stato` report this as `Runtime`. Only the output folder may be in iCloud.

GitHub is the source of truth, so the code can be edited in any other clone
(including one in `~/Documents`). To deploy what was pushed, run in the service
clone:

```bash
.venv/bin/python install-service.py --update
```

It pulls (fast-forward only), refreshes the dependencies, runs `--check` and
restarts the service; if the check fails the running service is left untouched.
`.env`, `config.yaml`, `.cache/` and `logs/` exist only in the service clone.

If Telegram stays unreachable for 15 minutes while the network is up, the
service exits and launchd restarts it (`Telegram unusable` in the log).

On macOS, a background service may be denied access to a protected folder such
as the iCloud output folder even when the same command works in a terminal. An
`Operation not permitted` error indicates this privacy restriction.
The required access must be explicitly approved in System Settings before
reinstalling the service; the installer does not change macOS permissions.
For framework Python installations, the installer starts the corresponding
`Resources/Python.app/Contents/MacOS/Python` and preserves `.venv` through
`__PYVENV_LAUNCHER__`. This uses the same app authorized in System Settings;
granting access to Python.app does not necessarily authorize the separate
command-line executable. If Python offers no toggle in Files and
Folders, adding Python.app to Full Disk Access grants broader access to
protected files and requires the user's explicit consent.

Regression tests (no network or Telegram messages):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Manual usage (without Telegram)

Run the same pipeline without Telegram:

```bash
.venv/bin/python prompt-listener.py --process "https://x.com/someone/status/123456789"
```

Inside a Claude Code session, pasting an X link makes Claude run this command
(see `CLAUDE.md`).

## Files

| File | Description |
|------|-------------|
| `fetch-x.py` | Fetches thread text via FxTwitter (twscrape fallback) |
| `pipeline.py` | Fetch, extraction with Claude, folders, files and verification |
| `prompt-listener.py` | Telegram bot, persistent queue, notifications, `/stato` |
| `CLAUDE.md` | Instructions for interactive Claude Code sessions |
| `config.yaml` | Your local config (gitignored) |
| `config.yaml.example` | Config template |
| `.env` | Telegram bot token (gitignored) |
| `.env.example` | Token template |
| `com.promptx.listener.plist.example` | macOS launchd template for auto-start |
| `install-service.py` | Generates and optionally installs the LaunchAgent |
| `requirements-bot.txt` | Minimal listener dependencies |
| `optimize-pipeline.py` | Same pipeline as `--process` |

## Configuration reference

`config.yaml` supports:

| Key | Description | Default |
|-----|-------------|---------|
| `output_dir` | Where to save extracted prompts | *(required)* |

## Notes

- The primary fetch uses the public FxTwitter API; the optional twscrape fallback uses browser cookies.
- The current extraction workflow reads thread text and skips image carousels.
- Topic folder names are always in Italian; prompt files use the language of their content.
- `.twscrape_accounts.db` and `.env` are gitignored — your credentials never leave your machine.

## License

MIT
