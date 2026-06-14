# AGENTS.md

Webhook service: a GitHub issue labeled `devin-autofix` triggers a Devin
session that fixes the issue and opens a PR. Single Flask app in `app.py`;
setup and webhook config are in README.md.

## Test

```bash
pip install -r requirements-dev.txt
pytest
```

Tests in `test_app.py` must not hit the network — Devin calls and
`post_issue_comment` are monkeypatched. Importing `app` runs `check_env()`
(exits if an env var is unset), `init_db()`, and starts the poller thread, so
the test file sets all env vars and a long `POLL_INTERVAL` *before* `import app`.

## Architecture

Two entry points over one SQLite `tasks` table keyed by `issue_number`:

- **`/webhook`** — `handle_issue_labeled` dedups with `INSERT OR IGNORE` (the DB
  row, not an in-memory set, is the unit). On Devin API failure it **deletes the
  row** so redelivery retries clean.
- **`poll_active_sessions`** (daemon thread) — every `POLL_INTERVAL`s, reconciles
  each non-terminal task against the Devin API.

`classify_session(session, has_pr)` check order is deliberate — **do not reorder**:

1. has a PR → `completed` (a PR wins over any status; keep first).
2. `status` in `error`/`suspended` → `failed`.
3. `status_detail == waiting_for_user` (note: `status_detail`, not `status`) → `blocked`.
4. `status == exit` or `status_detail == finished`, no PR → `failed`.
5. else → `running`.

`poll_once` also fails tasks older than `MAX_TASK_AGE` before calling the API — a
`failed` path that skips `classify_session`.

Comments fire on **state transition**, not per poll, or the issue gets spammed:
`mark_blocked` guards with `if row["status"] != "blocked"`, and terminal states
comment once because `finish_task` drops the task from the polled set (`status
NOT IN ('completed','failed')`). Keep this guard on any new comment. `blocked` is
**not** terminal — it stays in the loop and can still flip to `completed`.

## Conventions

- Keep everything in `app.py` — the repo was deliberately simplified to one file.
- No new dependencies: Flask + requests + stdlib only.
- Changing the `tasks` schema (`init_db`) means updating `build_status_report`
  and `finish_task` together — the `/status` JSON is a public shape.

## Security — do not weaken

- `/webhook` must verify the GitHub HMAC signature (`verify_webhook_signature`).
- `/status` must compare tokens in constant time (`hmac.compare_digest`) and
  **fail closed** when `STATUS_TOKEN` is unset.

## PRs

- `PROMPT_TEMPLATE` changes alter what Devin does in *target* repos — call them
  out explicitly in the PR description.
