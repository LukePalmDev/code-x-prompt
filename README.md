# Prompt X

Automatically extract prompts from X (Twitter) posts and save them as bilingual Markdown files — English original + Italian translation, organized by topic.

Paste an X link into Telegram → the bot fetches the thread, reads any image carousels, splits out each prompt, translates everything, and writes tidy `.md` files to any folder you choose.

## How it works

```
X post URL
    │
    ▼ Telegram bot (prompt-listener.py)
    │
    ▼ Claude Code CLI  (/x-prompt skill)
    │
    ├── fetch-x.py  →  tweet text + images (twscrape, no browser needed)
    │
    └── saves to output_dir/
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

- **Python 3.11+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — `npm install -g @anthropic-ai/claude-code`
- An **X (Twitter) account** for twscrape authentication
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather) *(optional — you can also run the pipeline manually)*

## Setup

### 1. Clone and install

```bash
git clone https://github.com/your-username/code-x-prompt.git
cd code-x-prompt
pip install -r requirements.txt
```

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

### 4. First run — configure X credentials

On the first call, `fetch-x.py` will prompt you for your X username and password. Credentials are saved to `.twscrape_accounts.db` locally and renewed automatically.

```bash
python3 fetch-x.py https://x.com/someone/status/123456789
```

### 5. Start the bot

```bash
python3 prompt-listener.py
```

Send any X post URL to your Telegram bot and the pipeline runs automatically.

## Auto-start on macOS login (optional)

Copy the launchd plist template and edit it with your repo path:

```bash
cp com.promptx.listener.plist.example ~/Library/LaunchAgents/com.promptx.listener.plist
# edit the file: replace /ABSOLUTE/PATH/TO/code-x-prompt with the real path
launchctl load ~/Library/LaunchAgents/com.promptx.listener.plist
```

## Manual usage (without Telegram)

You can run the pipeline directly from Claude Code CLI:

```bash
cd code-x-prompt
claude -p "/x-prompt https://x.com/someone/status/123456789"
```

Or interactively inside a Claude Code session:

```
/x-prompt https://x.com/someone/status/123456789
```

## Files

| File | Description |
|------|-------------|
| `fetch-x.py` | Fetches tweet text and images via twscrape (no browser) |
| `prompt-listener.py` | Telegram bot that triggers the pipeline |
| `CLAUDE.md` | Instructions for the Claude Code AI agent |
| `config.yaml` | Your local config (gitignored) |
| `config.yaml.example` | Config template |
| `.env` | Telegram bot token (gitignored) |
| `.env.example` | Token template |
| `com.promptx.listener.plist.example` | macOS launchd template for auto-start |

## Configuration reference

`config.yaml` supports:

| Key | Description | Default |
|-----|-------------|---------|
| `output_dir` | Where to save extracted prompts | *(required)* |

## Notes

- X blocks unauthenticated access — twscrape handles auth transparently after the first interactive setup.
- Image carousels (common in prompt posts) are downloaded and read via Claude's vision capability.
- Topic folder names are always in Italian; prompt files use the language of their content.
- `.twscrape_accounts.db` and `.env` are gitignored — your credentials never leave your machine.

## License

MIT
