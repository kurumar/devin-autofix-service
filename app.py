import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

DEVIN_API_KEY = os.environ.get("DEVIN_API_KEY", "")
DEVIN_ORG_ID = os.environ.get("DEVIN_ORG_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
DEVIN_API = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}"
STATUS_TOKEN = os.environ.get("STATUS_TOKEN", "")
DB_FILE = os.environ.get("DB_FILE", "data/tasks.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
MAX_TASK_AGE = int(os.environ.get("MAX_TASK_AGE", "86400"))

PROMPT_TEMPLATE = """\
You are an autonomous engineering agent working on the repository: {repo_url}.

## Task
Resolve GitHub Issue #{issue_number}: {issue_title}

{issue_body}

## Instructions
1. Clone the repository and create a new branch named `autofix/issue-{issue_number}`.
2. Read the relevant files and understand the context before making any changes.
3. Implement the fix described in the issue. Make the minimal change required — do not
   refactor unrelated code or expand the scope beyond what the issue asks.
4. Verify proportionally to your change: run only the linters/tests that apply to the
   files you touched. For a documentation-only change, just run the formatting/lint hooks
   on those files (e.g. `pre-commit run --files <changed files>`) and do not run the full
   test suite or build. Never run checks across the whole repo (`--all-files`).
5. Open a Pull Request against the repository's default branch (`{default_branch}`) with:
   - Title: `fix: {issue_title}`
   - Body: `Fixes #{issue_number}\\n\\n<description of what you changed and why>`
6. Do NOT merge the PR — leave it open for human review.
7. Do not ask the user questions. If you are blocked or the issue is ambiguous, make your
   best reasonable attempt and document your assumptions in the PR description.
8. After opening the PR, your task is complete.
"""

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("devin-autofix")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    with closing(connect()) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                issue_number     INTEGER PRIMARY KEY,
                issue_title      TEXT,
                issue_url        TEXT,
                repo             TEXT,
                devin_session_id TEXT,
                devin_url        TEXT,
                status           TEXT NOT NULL,
                pr_url           TEXT,
                acus_consumed    REAL DEFAULT 0,
                created_at       TEXT,
                completed_at     TEXT,
                duration_seconds INTEGER,
                error            TEXT
            )
            """
        )


def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: tuple = ()) -> int:
    with closing(connect()) as conn, conn:  # `conn` as ctx mgr = commit/rollback
        return conn.execute(sql, params).rowcount


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_seconds(since_iso: str) -> int:
    return int((datetime.now(timezone.utc) - datetime.fromisoformat(since_iso)).total_seconds())


def build_status_report() -> dict:
    tasks = [dict(r) for r in query_all("SELECT * FROM tasks ORDER BY created_at")]
    counts = Counter(t["status"] for t in tasks)
    return {
        "total": len(tasks),
        "active": counts["running"],
        "blocked": counts["blocked"],
        "completed": counts["completed"],
        "failed": counts["failed"],
        "acus_consumed": round(sum(t["acus_consumed"] or 0 for t in tasks), 2),
        "tasks": tasks,
    }


# GitHub API helpers

def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET or not signature:
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def post_issue_comment(repo: str, issue_number: int, body: str) -> None:
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    resp = requests.post(
        url,
        headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"},
        json={"body": body},
        timeout=15,
    )
    if resp.ok:
        log.info("  Comment posted on issue #%d", issue_number)
    else:
        log.error("  Failed to comment on issue #%d: %s", issue_number, resp.text)


# Devin API helpers

DEVIN_HEADERS = {"Authorization": f"Bearer {DEVIN_API_KEY}", "Content-Type": "application/json"}
MAX_ACU_LIMIT = int(os.environ.get("MAX_ACU_LIMIT", "10"))


def create_devin_session(prompt: str, tags: list[str]) -> dict:
    resp = requests.post(
        f"{DEVIN_API}/sessions",
        headers=DEVIN_HEADERS,
        json={"prompt": prompt, "tags": tags, "max_acu_limit": MAX_ACU_LIMIT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_devin_session(session_id: str) -> dict:
    resp = requests.get(f"{DEVIN_API}/sessions/{session_id}", headers=DEVIN_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


# Handle a new labeled issue

def handle_issue_labeled(payload: dict) -> str:
    if payload.get("label", {}).get("name") != "devin-autofix":
        log.info("  Ignoring issue — label is not devin-autofix")
        return "ignored: label is not devin-autofix"

    issue = payload["issue"]
    issue_number = issue["number"]
    repo = payload["repository"]["full_name"]

    inserted = execute(
        "INSERT OR IGNORE INTO tasks "
        "(issue_number, issue_title, issue_url, repo, status, acus_consumed, created_at) "
        "VALUES (?, ?, ?, ?, 'running', 0, ?)",
        (issue_number, issue["title"], issue["html_url"], repo, now_iso()),
    )
    if not inserted:
        log.info("  Skipping issue #%d — session already exists", issue_number)
        return "duplicate"

    prompt = PROMPT_TEMPLATE.format(
        repo_url=payload["repository"]["html_url"],
        default_branch=payload["repository"].get("default_branch") or "main",
        issue_number=issue_number,
        issue_title=issue["title"],
        issue_body=issue.get("body") or "",
    )

    try:
        session = create_devin_session(prompt, tags=["devin-autofix", f"issue-{issue_number}"])
    except Exception:
        execute("DELETE FROM tasks WHERE issue_number = ?", (issue_number,))
        raise

    session_id = session["session_id"]
    devin_url = session.get("url", f"https://app.devin.ai/sessions/{session_id}")
    execute(
        "UPDATE tasks SET devin_session_id = ?, devin_url = ? WHERE issue_number = ?",
        (session_id, devin_url, issue_number),
    )

    log.info("  Devin session created: %s — %s", session_id, devin_url)
    post_issue_comment(
        repo, issue_number,
        f"**Devin is working on this!**\n\nSession: [{session_id}]({devin_url})\n\nI'll post an update when it's done.",
    )
    return f"session created: {session_id}"


# Checks active Devin sessions and updates task state

def classify_session(session: dict, has_pr: bool) -> tuple[str, str | None]:
    status = session.get("status")
    detail = session.get("status_detail")
    if has_pr:
        return "completed", None
    if status in ("error", "suspended"):
        return "failed", detail or status
    if detail == "waiting_for_user":
        return "blocked", "Devin is waiting for input and hasn't opened a PR yet"
    if status == "exit" or detail == "finished":
        return "failed", "session ended without opening a pull request"
    return "running", None


def finish_task(row: sqlite3.Row, status: str, *, pr_url: str | None = None,
                error: str | None = None, acus: float = 0) -> None:
    execute(
        "UPDATE tasks SET status = ?, completed_at = ?, duration_seconds = ?, "
        "pr_url = ?, error = ?, acus_consumed = ? WHERE issue_number = ?",
        (status, now_iso(), elapsed_seconds(row["created_at"]), pr_url, error, acus, row["issue_number"]),
    )
    sid, url, num = row["devin_session_id"], row["devin_url"], row["issue_number"]
    if status == "completed":
        log.info("  %s: completed — PR %s", sid, pr_url or "(none)")
        body = f"**Done!** Session [{sid}]({url}) completed."
        if pr_url:
            body += f"\n\nPull Request: {pr_url}"
    else:
        log.error("  %s: failed — %s", sid, error)
        body = f"**Devin encountered an error.** Session [{sid}]({url})\n\nError: {error}"
    post_issue_comment(row["repo"], num, body)


def mark_blocked(row: sqlite3.Row, error: str, acus: float) -> None:
    execute(
        "UPDATE tasks SET status = 'blocked', error = ?, acus_consumed = ? WHERE issue_number = ?",
        (error, acus, row["issue_number"]),
    )
    if row["status"] != "blocked":
        post_issue_comment(
            row["repo"], row["issue_number"],
            f"**Devin needs input to continue.** Session [{row['devin_session_id']}]({row['devin_url']}) "
            f"is waiting for a human and hasn't opened a PR yet. Please review and respond.",
        )


def update_task_from_session(row: sqlite3.Row, session: dict) -> None:
    prs = session.get("pull_requests") or []
    pr_url = prs[0].get("pr_url") if prs else None
    lifecycle, error = classify_session(session, has_pr=pr_url is not None)
    acus = session.get("acus_consumed", row["acus_consumed"]) or 0
    log.info("  Poll %s: status=%s detail=%s -> %s",
             row["devin_session_id"], session.get("status"), session.get("status_detail"), lifecycle)

    if lifecycle == "completed":
        finish_task(row, "completed", pr_url=pr_url, acus=acus)
    elif lifecycle == "failed":
        finish_task(row, "failed", error=error, acus=acus)
    elif lifecycle == "blocked":
        mark_blocked(row, error, acus)
    else:
        execute(
            "UPDATE tasks SET status = 'running', acus_consumed = ? WHERE issue_number = ?",
            (acus, row["issue_number"]),
        )


def poll_once() -> None:
    rows = query_all(
        "SELECT * FROM tasks WHERE status NOT IN ('completed', 'failed') "
        "AND devin_session_id IS NOT NULL"
    )
    for row in rows:
        if elapsed_seconds(row["created_at"]) > MAX_TASK_AGE:
            finish_task(row, "failed",
                        error=f"timed out after {MAX_TASK_AGE}s without a pull request",
                        acus=row["acus_consumed"] or 0)
            continue
        try:
            session = get_devin_session(row["devin_session_id"])
            update_task_from_session(row, session)
        except Exception as exc:
            log.error("  Error polling %s: %s", row["devin_session_id"], exc)


def poll_active_sessions() -> None:
    while True:
        time.sleep(POLL_INTERVAL)
        poll_once()


app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    if not verify_webhook_signature(request.data, request.headers.get("X-Hub-Signature-256", "")):
        log.warning("Webhook signature verification failed")
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    if request.headers.get("X-GitHub-Event") != "issues" or payload.get("action") != "labeled":
        return jsonify({"status": "ignored"})

    issue_number = payload.get("issue", {}).get("number", "?")
    log.info("Webhook: issue #%s", issue_number)

    try:
        result = handle_issue_labeled(payload)
        return jsonify({"status": "ok", "result": result})
    except Exception as exc:
        log.error("Error handling webhook: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/status")
def status():
    # Header only: a query-param token would leak into proxy and access logs.
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not STATUS_TOKEN or not hmac.compare_digest(token.encode(), STATUS_TOKEN.encode()):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(build_status_report())


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def check_env() -> None:
    required = {
        "DEVIN_API_KEY": DEVIN_API_KEY,
        "DEVIN_ORG_ID": DEVIN_ORG_ID,
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "GITHUB_WEBHOOK_SECRET": GITHUB_WEBHOOK_SECRET,
        "STATUS_TOKEN": STATUS_TOKEN,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        raise SystemExit(1)


check_env()
init_db()

threading.Thread(target=poll_active_sessions, daemon=True).start()
log.info("Poller started (interval=%ds)", POLL_INTERVAL)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)