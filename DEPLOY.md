# Deploy

The service is the Telegram listener in `prompt-listener.py`. It calls `pipeline.py`.

Install and day-to-day commands are in [README.md](README.md). Short version, from a clone that is **not** inside an iCloud-synced folder (`~/Documents`, `~/Desktop`, or `~/Library/Mobile Documents`):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-bot.txt
cp config.yaml.example config.yaml
cp .env.example .env
# edit config.yaml (output_dir) and .env (TELEGRAM_BOT_TOKEN)
claude auth login
.venv/bin/python install-service.py --install
```

After pushing new code, update the service clone with:

```bash
.venv/bin/python install-service.py --update
```

That pull is fast-forward only. It refreshes `requirements-bot.txt`, runs `--check`, and restarts the LaunchAgent only if the check passes.

`optimize-pipeline.py` is a wrapper around `pipeline.process`. It does not call the Anthropic HTTP API.
