# How a post is processed

`pipeline.py` does the deterministic work: fetch, folder name, numbering, writing, and verification. Claude Code is called once per new post, with tools disabled and a fixed JSON schema, and only to extract and translate prompts.

- The fetch uses the public FxTwitter API. Images are not downloaded on this path.
- A post already recorded in `.cache/processed.json` does not call Claude again.
- Complete prompt files are never overwritten.
- The default model comes from `.claude/settings.json`, with Sonnet as the fallback inside Claude only.
- `provider: ollama` is optional. It uses the same JSON schema, sends an explicit `num_ctx`, and does not fall back to Claude. See [README.md](README.md).

`cost-comparison.py` prints token estimates from an older direct-API experiment. Those numbers are not a measurement of the current pipeline.

Run a post with:

```bash
.venv/bin/python prompt-listener.py --process "<x-url>"
```
