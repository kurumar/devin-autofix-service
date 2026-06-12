import hashlib
import hmac
import json
import os
import tempfile

import pytest

# Env must be set before importing app: config is read at module level, missing
# vars abort startup, and the import starts the poller thread (a long
# POLL_INTERVAL keeps it asleep).
os.environ.update(
    {
        "DB_FILE": os.path.join(tempfile.mkdtemp(), "tasks.db"),
        "GITHUB_WEBHOOK_SECRET": "test-webhook-secret",
        "STATUS_TOKEN": "test-status-token",
        "DEVIN_API_KEY": "test-devin-key",
        "DEVIN_ORG_ID": "test-org",
        "GITHUB_TOKEN": "test-github-token",
        "POLL_INTERVAL": "3600",
    }
)

import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    app.execute("DELETE FROM tasks")


@pytest.fixture(autouse=True)
def comments(monkeypatch):
    """Record issue comments instead of posting them — no test may hit the network."""
    posted = []
    monkeypatch.setattr(app, "post_issue_comment", lambda repo, num, body: posted.append((num, body)))
    return posted


@pytest.fixture
def client():
    return app.app.test_client()


@pytest.fixture
def fake_devin(monkeypatch):
    calls = []

    def create(prompt, tags):
        calls.append({"prompt": prompt, "tags": tags})
        return {"session_id": "devin-123", "url": "https://app.devin.ai/sessions/devin-123"}

    monkeypatch.setattr(app, "create_devin_session", create)
    return calls


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()


def issue_payload(number=1, label="devin-autofix"):
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {
            "number": number,
            "title": "Fix the bug",
            "html_url": f"https://github.com/acme/repo/issues/{number}",
            "body": "Steps to reproduce...",
        },
        "repository": {
            "full_name": "acme/repo",
            "html_url": "https://github.com/acme/repo",
            "default_branch": "main",
        },
    }


def post_webhook(client, payload, event="issues", signature=None):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature or sign(body)},
    )


def test_signature_valid():
    body = b'{"a": 1}'
    assert app.verify_webhook_signature(body, sign(body))


def test_signature_tampered_or_missing():
    assert not app.verify_webhook_signature(b"tampered", sign(b'{"a": 1}'))
    assert not app.verify_webhook_signature(b'{"a": 1}', "")


@pytest.mark.parametrize(
    ("session", "has_pr", "expected"),
    [
        ({"status": "running"}, True, "completed"),
        ({"status": "error", "status_detail": "boom"}, False, "failed"),
        ({"status": "suspended"}, False, "failed"),
        ({"status": "running", "status_detail": "waiting_for_user"}, False, "blocked"),
        ({"status": "exit"}, False, "failed"),
        ({"status": "running"}, False, "running"),
    ],
)
def test_classify_session(session, has_pr, expected):
    lifecycle, _ = app.classify_session(session, has_pr=has_pr)
    assert lifecycle == expected


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_status_requires_token(client):
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Query-param tokens leak into access logs and are deliberately not accepted.
    assert client.get("/status?token=test-status-token").status_code == 401


def test_status_with_token(client):
    resp = client.get("/status", headers={"Authorization": "Bearer test-status-token"})
    assert resp.status_code == 200
    report = resp.get_json()
    assert report["total"] == 0
    assert report["tasks"] == []


def test_webhook_rejects_bad_signature(client):
    assert post_webhook(client, issue_payload(), signature="sha256=bad").status_code == 403


def test_webhook_ignores_irrelevant_events(client, fake_devin):
    assert post_webhook(client, issue_payload(), event="push").get_json() == {"status": "ignored"}
    assert post_webhook(client, {"action": "opened"}).get_json() == {"status": "ignored"}
    assert "ignored" in post_webhook(client, issue_payload(label="bug")).get_json()["result"]
    assert fake_devin == []


def test_webhook_creates_session(client, fake_devin, comments):
    resp = post_webhook(client, issue_payload(number=7))
    assert resp.get_json() == {"status": "ok", "result": "session created: devin-123"}
    assert "Issue #7" in fake_devin[0]["prompt"]
    assert fake_devin[0]["tags"] == ["devin-autofix", "issue-7"]
    (task,) = app.query_all("SELECT * FROM tasks")
    assert (task["issue_number"], task["status"], task["devin_session_id"]) == (7, "running", "devin-123")
    assert len(comments) == 1


def test_webhook_skips_duplicate_issue(client, fake_devin):
    post_webhook(client, issue_payload(number=7))
    assert post_webhook(client, issue_payload(number=7)).get_json()["result"] == "duplicate"
    assert len(fake_devin) == 1


def test_webhook_devin_failure_rolls_back_task(client, monkeypatch):
    def boom(prompt, tags):
        raise RuntimeError("devin api down")

    monkeypatch.setattr(app, "create_devin_session", boom)
    resp = post_webhook(client, issue_payload(number=7))
    assert resp.status_code == 500
    assert app.query_all("SELECT * FROM tasks") == []  # row gone, redelivery can retry


def test_session_with_pr_completes_task(client, fake_devin):
    post_webhook(client, issue_payload(number=7))
    (row,) = app.query_all("SELECT * FROM tasks")
    app.update_task_from_session(
        row,
        {
            "status": "running",
            "pull_requests": [{"pr_url": "https://github.com/acme/repo/pull/9"}],
            "acus_consumed": 2.5,
        },
    )
    (task,) = app.query_all("SELECT * FROM tasks")
    assert (task["status"], task["pr_url"], task["acus_consumed"]) == (
        "completed",
        "https://github.com/acme/repo/pull/9",
        2.5,
    )
    assert task["completed_at"] is not None


def test_waiting_session_marks_task_blocked(client, fake_devin):
    post_webhook(client, issue_payload(number=7))
    (row,) = app.query_all("SELECT * FROM tasks")
    app.update_task_from_session(row, {"status": "running", "status_detail": "waiting_for_user"})
    (task,) = app.query_all("SELECT * FROM tasks")
    assert task["status"] == "blocked"
    assert task["completed_at"] is None  # blocked is not terminal — polling continues


def test_poll_marks_stale_task_failed(client, fake_devin, monkeypatch):
    post_webhook(client, issue_payload(number=7))
    app.execute("UPDATE tasks SET created_at = ?", ("2020-01-01T00:00:00+00:00",))
    monkeypatch.setattr(app, "get_devin_session", lambda sid: {"status": "running"})
    app.poll_once()
    (task,) = app.query_all("SELECT * FROM tasks")
    assert task["status"] == "failed"
    assert "timed out" in task["error"]
