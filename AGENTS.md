# AGENTS.md

Webhook service: a GitHub issue labeled `devin-autofix` triggers a Devin
session that fixes the issue and opens a PR. The entire service is a single
Flask app in `app.py`; setup and webhook config are in README.md.

## Test

```bash
pip install -r requirements-dev.txt
pytest
```

All tests are in `test_app.py`, one file mirroring the one-file app. Update a
test when you change behavior. Tests never hit the network: Devin calls and
`post_issue_comment` are monkeypatched via fixtures.

## Run

```bash
cp .env.example .env   # fill in credentials
python app.py          # or: docker compose up -d --build
```

## Conventions

- Keep everything in `app.py` — the repo was deliberately simplified to one
  file. Minimal diffs, scoped to the issue at hand.
- No new dependencies: Flask + requests + stdlib only.
- Type hints on functions; config from env vars at the top of `app.py`.

## Security — do not weaken

- `/webhook` must verify the GitHub HMAC signature (`verify_webhook_signature`).
- `/status` must compare tokens in constant time (`hmac.compare_digest`).
- Never commit `.env`, hardcode credentials, or log secret values.

## PRs

- Small, focused diffs; CI runs `pytest` on every PR.
- `PROMPT_TEMPLATE` changes alter what Devin does in *target* repos — call
  them out explicitly in the PR description.
