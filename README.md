# AI-driven arXiv crawler

This script crawls recent arXiv papers, scores title relevance with a local LLM
(tested with `llama.cpp` server), keeps high-score papers, then optionally:

- saves selected papers to an HTML file
- sends the same HTML content by email

## Files

- `weekly_arXiv.py`: main script
- `settings.yaml`: runtime config
- `interest.txt`: plain-text description of your research interests
- `account.json`: email account config (required only when `send_email: true`)

## Settings

`settings.yaml` controls:

- arXiv list URL
- arXiv time window (`arxiv.recent_days`, e.g. `2` for recent 2 days)
- local LLM endpoint/model (`http://127.0.0.1:8080/v1` by default)
- relevance threshold (`selection.threshold`)
- output toggles (`output.save_html`, `output.send_email`)

## Email config

Create `account.json`:

```json
{
  "sender": {
    "server": "smtp server",
    "port": 994,
    "user": "email address",
    "passwd": "password"
  },
  "receiver": "email address"
}
```

## Run

```bash
python weekly_arXiv.py
```
