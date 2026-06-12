# devin-autofix-service

An event-driven automation that turns labeled GitHub issues into [Devin](https://devin.ai/) sessions producing pull requests autonomously. When you add the `devin-autofix` label to any issue in the targeted repo, this service creates a Devin session to fix it and open a PR.

## How It Works

```text
  GitHub issue  +  "devin-autofix" label
            |
            v

  devin-autofix-service receives GitHub webhook
            |
            v

  devin-autofix-service creates Devin session via Devin API
            |
            v

  Devin session clones repo -> edits -> verifies -> opens PR
            |
            v

  devin-autofix-service checks

        has a PR?
         /        \
       yes          no, check Devin session status
        |                    |
        v                    +- waiting_for_user -> comment "Needs input"
  comment "Done"             |
                             +- errored / suspended ---> comment "Error"
                                 
```

1. You create a GitHub issue
2. You add the `devin-autofix` label
3. GitHub fires a webhook to the devin-autofix-service
4. Service reads the issue and creates a Devin session via the [Devin API](https://docs.devin.ai/api-reference/overview)
5. Devin autonomously clones the repo, reads the code, makes changes, verifies, opens a PR
6. Service polls Devin session status and posts progress comments on the issue
7. `GET /status` returns all tasks, total ACUs consumed, and per-task details

## Project Structure

```
.
├── app.py                # GitHub + Devin AI automation
├── .env.example          # Template for credentials
├── test_app.py           
├── AGENTS.md             
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhook` | POST | Receives GitHub webhook events (issue labeled) |
| `/status` | GET | Returns all tasks as JSON (requires `Authorization: Bearer` token) |
| `/health` | GET | Health check |

## Quick Start

### 0. Set up Devin (one-time)

In Devin, go to **Settings -> Integrations -> GitHub** and install the Devin GitHub App on the org/repo you want autofixed. This is what lets Devin clone the repo, push branches, and open pull requests.

### 1. Clone this repo

```bash
git clone https://github.com/kurumar/devin-autofix-service.git
cd devin-autofix-service
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
DEVIN_API_KEY=your_devin_api_key_here
DEVIN_ORG_ID=your_devin_org_id_here
GITHUB_TOKEN=your_github_token_here
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here
STATUS_TOKEN=your_status_token_here
```

**Generate secrets:**
```bash
openssl rand -hex 20   # for GITHUB_WEBHOOK_SECRET
openssl rand -hex 16   # for STATUS_TOKEN
```

### 3. Run with Docker

```bash
docker compose up -d --build && docker compose logs -f
```

The service starts on port 5000. Check it's running:

```bash
curl http://localhost:5000/health
```

### 4. Set up the GitHub webhook

In your target repository:

1. Go to **Settings -> Webhooks -> Add webhook**
2. **Payload URL**: `http://YOUR_SERVER_IP:5000/webhook`
3. **Content type**: `application/json`
4. **Secret**: paste the same secret from your `.env`
5. **Events**: select **"Issues"** only
6. Click **Add webhook**

### 5. Test it

1. **Create an issue** in your repo
2. **Add the `devin-autofix` label** to the issue
3. **Watch the docker compose logs:**
   ```bash
   docker compose logs -f
   ```
4. **Check status:**
   ```bash
   curl -H "Authorization: Bearer YOUR_STATUS_TOKEN" http://YOUR_SERVER_IP:5000/status
   ```
5. **Watch the GitHub issue** — Devin posts a comment when it starts and when it finishes
