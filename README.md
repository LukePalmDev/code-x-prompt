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
    ├── extraction + translation only (no tools, one JSON schema)
    │     ├── Claude Code CLI   default; Sonnet fallback stays inside Claude
    │     └── Ollama /api/chat  optional; never falls back to Claude
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
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — `npm install -g @anthropic-ai/claude-code` (default provider)
- **Ollama on localhost** only if you later set `provider: ollama`. Not used until then. Do not pull a model for this repo; point `ollama.model` at a tag already installed.
- An **X (Twitter) account** only if the optional authenticated fallback is needed
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather) *(only to run the bot — the pipeline can also be run manually)*

## Environment

| Name | Required | Where |
|------|----------|--------|
| `TELEGRAM_BOT_TOKEN` | Bot only | `.env`, copied from `.env.example`. Do not commit `.env`. |
| `output_dir` | Always | `config.yaml`, copied from `config.yaml.example`. This is not an environment variable. |

Claude Code must be logged in (`claude auth login`) while `provider` is unset or `claude`. The default fetch uses the public FxTwitter API and needs no X API key, bearer token, or cookie. `twscrape` reads browser cookies only when that package is installed and the public fetch fails. Ollama is not contacted unless `config.yaml` sets `provider: ollama`.

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

This checks the token's presence, output directory and the configured provider
without sending Telegram messages or running an extraction. The default provider
check is Claude login. After `provider: ollama` it only sends `GET /api/tags`
to localhost and checks that `ollama.model` is already listed; it does not
generate, pull a model, or call Claude. A successful Claude login check does
not guarantee available session quota. Public FxTwitter fetching needs no
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

Send `/stato` to the bot to check the active provider (Claude login, or the
local Ollama tag), the output folder, FxTwitter, the queue and the last
success or error. The reply text for a finished post stays `✅ Fatto. N prompt in 'Topic'.`

Reliability:

- Every link is saved in `.cache/queue/` before it is acknowledged, so a
  restart, crash or sleeping Mac never loses it; progress is saved per step.
- Transient failures (network, overload, usage limits) are retried three times
  immediately, then after 15 minutes, 1 hour and 3 hours. Permanent failures
  are moved to `.cache/failed/`. Every outcome is reported on Telegram.
- Archived failures are not retried by themselves. `--retry-failed` is the
  supported way to put them back on the original Telegram chat. Do not move
  those JSON files by hand.
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
| `pipeline.py` | Fetch, extraction (Claude or optional Ollama), folders, files and verification |
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
| `provider` | `claude` or `ollama` | `claude` when the key is absent |
| `ollama.model` | Exact local tag from `ollama list`. Required only for `provider: ollama`. No model is pulled. | *(none)* |
| `ollama.base_url` | Loopback Ollama. Remote hosts are rejected. | `http://127.0.0.1:11434` |
| `ollama.num_ctx` | Sent on every chat request. Values above 8192, including the model-card size 262144, are rejected. | `4096` |
| `ollama.num_predict` | Maximum tokens to generate. Must leave at least 512 tokens of input inside `num_ctx`. | `2048` |
| `ollama.think` | Documented `POST /api/chat` boolean. `false` asks for no thinking trace. A missing key still sends `false`. | `false` |
| `ollama.timeout_seconds` | One request, including time queued behind another client of the same server. | `240` |
| `ollama.keep_alive` | Per-request keep-alive. `0` is rejected so this client does not unload a shared model. | `5m` |
| `ollama.temperature` | Sampling temperature. | `0` |

## Claude provider

The pipeline calls Claude Code with tools disabled. The model id is `claude-sonnet-5-5` (Sonnet 5.5, the model the `sonnet` alias selects on the Anthropic API; it needs Claude Code 2.1.284 or newer, and 2.1.317 provides `--effort medium`). Effort is `medium`. `CLAUDE_CODE_EFFORT_LEVEL` overrides the flag, so that variable is set on the Claude child process and not in the service environment. Thinking on Sonnet 5.5 cannot be disabled. The command does not pass `--fallback-model` and does not read a Haiku id from `.claude/settings.json`.

```yaml
provider: claude
claude:
  model: "claude-sonnet-5-5"
  effort: medium
```

While the service `config.yaml` still says `provider: ollama`, this code keeps using Ollama. Switching is that edit in the service clone. It is not in git. A plan-limit error is archived once and is not retried at 15 minutes, 1 hour, and 3 hours. The weekly reset reported for this account is 06:00 Europe/Rome. That is not a 5-hour window.

`claude -p --output-format json` can include `usage` (`input_tokens` is only the uncached part; cache writes and reads are separate), `total_cost_usd`, and `duration_ms`. The chat reply stays short: result, model, effort, and duration in seconds. Token counts and `total_cost_usd` go to the log as `claude_usage`. That cost is the CLI list-price estimate, not the Pro subscription charge, and it is not shown in Telegram. Tokens are not converted into a percent. `/usage` is interactive and is not run. If the JSON has no `rate_limits.five_hour.used_percentage`, the reply says `Finestra 5h: non disponibile.`

Check the resolved id without a generation, without Telegram, and without fetching X:

```bash
.venv/bin/python prompt-listener.py --smoke-claude
```

Exit 2 means the service config is still Ollama, and neither Claude nor Ollama was called.

## Optional local Ollama

The running service stays on Claude until `config.yaml` in the service clone sets `provider: ollama`. Copying `config.yaml.example` does not switch provider: the Ollama block is commented. Do not change the Ollama service, its environment, or the models it has loaded. Trader (gaming-classifier) already uses the same server and the same tag.

Verified local tag to copy into `ollama.model` when you enable it:

```yaml
provider: ollama
ollama:
  base_url: "http://127.0.0.1:11434"
  model: "gemma4:12b-it-q4_K_M"
  timeout_seconds: 240
  num_ctx: 4096
  num_predict: 2048
  temperature: 0
  think: false
  keep_alive: "5m"
```

`gemma4:12b-it-q4_K_M` is about 8.0GB (Q4_K_M). On the Mac mini M4 with 16GB of memory, a local smoke at `num_ctx` 4096 left about 14% RAM free, took about 35 seconds including the cold load, and generated at about 23 tokens per second. That measurement is why 4096 is sent explicitly. A missing `num_ctx` would otherwise follow the model default, which previously truncated a classifier at 4K and whose card context is 262144, too large for this machine. Posts that do not fit the budget fail before the request. The reply is discarded when generation hits `num_predict`, when `prompt_eval_count` fills `num_ctx`, or when the JSON does not match the schema. Nothing is shortened and then saved.

`think` is the [Ollama chat field](https://docs.ollama.com/api/chat): `true` requests a thinking trace, `false` requests none, and omitting it on the wire uses the model default. This model's default is thinking on, so every extraction sends `false` unless `ollama.think` is `true`. The schema goes in `format`. `tools` is not sent. Structured output uses the same system prompt and the same `topic` / `prompts` object as Claude.

This process makes one Ollama call at a time and waits out a manual run on the same pipeline lock. It cannot reserve the server against Trader. Timeouts, connection failures, and a busy runner are retried (5s, then 20s, three attempts in all) and stay retryable by the Telegram queue. Invalid JSON is retried the same way and then becomes a permanent error. None of those paths call Claude.

### Offline checks, no Telegram

Regression tests mock Ollama and do not use the network, Telegram, Claude, or Ollama:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`--check` does not send Telegram and does not generate. With the default config it still checks Claude login. After `provider: ollama` it only calls `GET http://127.0.0.1:11434/api/tags`.

```bash
.venv/bin/python prompt-listener.py --check
```

Model smoke, only after the service config sets `provider: ollama` and the verified tag. It does not create the Telegram bot, so a configured token cannot be used to send. It does not fetch X, write prompt files, or update `processed.json`. Exit `0` is a valid local JSON extraction. Exit `2` means the provider is still Claude and neither Claude nor Ollama was called. Exit `1` is a local failure, also with no Telegram send.

```bash
.venv/bin/python prompt-listener.py --smoke-provider
```

Leave the LaunchAgent on Claude until that command has been run on the mini and the result is accepted. Deploy of this code with the existing `config.yaml` does not change the provider.

## Retry archived jobs

A job that exhausted its retries stays in `.cache/failed/{tweet_id}.json` with the original `url`, `chat_id`, `message_id` and `received`. The running service does not pick it up again until this command accepts it. Nothing in the command sends Telegram, fetches X, or calls the model. After a successful accept, the service already running delivers the result to that same chat and reply message.

From the service clone, read-only counts and the per-job decision:

```bash
.venv/bin/python prompt-listener.py --retry-failed
```

`--retry-failed --dry-run` is the same read-only command. It does not create or rewrite `.cache/queue`, `.cache/failed`, or `.cache/failed-audit`.

Accept the eligible records into the queue:

```bash
.venv/bin/python prompt-listener.py --retry-failed --execute
```

`--execute` writes a queue copy, checks that `url`, `chat_id`, `message_id`, `rounds=0` and `retry_at=0` read back, and only then moves the original file to `.cache/failed-audit/`. The audit file is the original bytes. A second execute does not create another queue file and does not replace an audit file. Records already listed in `.cache/processed.json`, already queued for the same chat, missing a real `chat_id` or `message_id`, or stored under the wrong file name are left where they are. `output_dir` is not changed.

The lines `eligible` and `accepted` include `chat=` and `message=` from the archived job. Those are the only route used. The bot token is not printed.

Regression tests do not read `.env` or the service `config.yaml`. They inject the provider. Run them once on a checkout with no `config.yaml`, and they also pass when that file selects Ollama:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Notes

- The primary fetch uses the public FxTwitter API; the optional twscrape fallback uses browser cookies.
- The current extraction workflow reads thread text and skips image carousels.
- Topic folder names are always in Italian; prompt files use the language of their content.
- `.twscrape_accounts.db` and `.env` are gitignored — your credentials never leave your machine.

## License

MIT
