# AGENTS.md

Webhook service: a GitHub issue labeled `devin-autofix` triggers a Devin session
that fixes the issue and opens a PR. The entire service is a single Flask app
in `app.py`. See README.md for the full flow and webhook setup.

## Run

```bash
cp .env.example .env          # fill in credentials first
docker compose up -d --build  # preferred
docker compose logs -f
```

Or locally:

```bash
pip install -r requirements.txt
python app.py
```

Verify it's up:

```bash
curl http://localhost:5000/health
```

## Verify

```bash
pip install -r requirements-dev.txt
pytest
```

Tests live in `test_app.py` — single file, mirroring the single-file app. Add
or update a test when you change behavior. Tests must never hit the network:
Devin calls (`create_devin_session` / `get_devin_session`) and
`post_issue_comment` are monkeypatched via fixtures in `test_app.py`.

## Conventions

- Keep everything in the single `app.py` — do not split it into modules. The
  repo was deliberately simplified to one file; keep diffs minimal and scoped
  to the issue at hand.
- Dependencies are Flask + requests + stdlib (`sqlite3`, `hmac`, `threading`).
  Do not add new dependencies for small features.
- Type hints on function signatures; module-level config read from env vars at
  the top of `app.py`.

## Security — do not weaken

- `/webhook` must verify the GitHub HMAC signature (`verify_webhook_signature`).
- `/status` must use a constant-time token comparison (`hmac.compare_digest`).
- Never commit `.env`, hardcode credentials, or log token/secret values.

## PRs

- Small, focused diffs; describe what changed and why.
- Changes to `PROMPT_TEMPLATE` alter what Devin does in *target* repos — call
  these out explicitly in the PR description.
