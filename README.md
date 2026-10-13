<div align="center">

# 📲 Free SMS Gateway

**Self-hosted SMS alerts for your infrastructure — powered by Free Mobile.**

A lightweight, security-conscious REST gateway for routing notifications from multiple apps and servers to the SMS number associated with your Free Mobile account.

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Deploy-Docker-2496ED?logo=docker&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

[**Quick start**](#-quick-start) · [**API reference**](#-api-reference) · [**Security**](#-security) · [**Documentation française**](docs/README.fr.md) · [**Production checklist (FR)**](docs/PRODUCTION_CHECKLIST.fr.md)

</div>

> [!IMPORTANT]
> This is a community project, **not affiliated with or endorsed by Free Mobile**. Free's personal notification API sends SMS messages **only to the subscriber's own Free Mobile number**. It is not a general-purpose SMS provider or a tool for sending to arbitrary recipients.

## ✨ Features

- 🔑 **Per-service bearer tokens** — keep the Free Mobile API key on one host.
- 📥 **Persistent SQLite queue** — alerts survive normal restarts.
- ⏱️ **Rate limiting & quotas** — all SMS attempts are persisted and paced, with a 24-hour attempt quota.
- ♻️ **Idempotency & deduplication** — collapse repeats by fingerprint and request ID.
- ⌛ **Alert TTL and bounded backlog** — expired alerts are not sent; queue size and retention are configurable.
- ❤️ **Readiness checks** — detect dead workers, stale backlogs and unresolved critical failures.
- 🚨 **Prioritized delivery** — `critical` → `warning` → `info`.
- 🛡️ **Conservative retries** — avoid automatic replay when transport outcome is uncertain.
- 🐳 **Docker Compose** — non-root process, read-only root filesystem and localhost binding.
- 🔎 **Status endpoint** — query each alert's processing state without revealing other clients' alerts.

## 🧩 Architecture

```text
Monitoring   Scheduled jobs   Applications
    │              │               │
    └──────────────┼───────────────┘
                   │ Bearer tokens (private network)
                   ▼
         ┌───────────────────────┐
         │  FastAPI gateway      │
         │  Auth · quotas · dedup │
         └───────────┬───────────┘
                     │
         ┌───────────▼───────────┐
         │ SQLite persisted queue│
         │ Single delivery worker│
         └───────────┬───────────┘
                     │ HTTPS
                     ▼
             Free Mobile SMS API
                     │
                     ▼
          Subscriber's own phone
```

## ⚡ Quick start

**Requirements:** Docker and Docker Compose; a Free Mobile subscription with the **"Notifications par SMS"** option enabled.

```bash
git clone https://github.com/DissidentAI/free-sms-gateway.git
cd free-sms-gateway
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(36))'
```

Edit `.env` and replace **all** placeholder values (**startup rejects the included example credentials**):

- `FREE_USER`: your Free Mobile subscriber login.
- `FREE_API_KEY`: your Free Mobile SMS API key (**never share it**).
- `CLIENT_KEYS`: JSON dictionary of client names and **distinct, randomly generated** secrets (at least 32 characters each).

```bash
chmod 600 .env
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
```

> [!CAUTION]
> The Docker port is intentionally bound to `127.0.0.1`. Do **not** expose the plain HTTP endpoint directly to the public internet. Use a private overlay network (Tailscale/WireGuard), an SSH tunnel, or a properly configured HTTPS reverse proxy.

## 📨 Send your first alert

```bash
curl -fsS http://127.0.0.1:8080/v1/alerts \
  -H 'Authorization: Bearer YOUR_MONITORING_CLIENT_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "severity": "critical",
    "message": "A monitored service is unavailable",
    "fingerprint": "service-unavailable",
    "request_id": "incident-000001"
  }'
```

Response (`202 Accepted`):

```json
{"id": 1, "status": "queued"}
```

Look up its status with the **same client token**:

```bash
curl -fsS http://127.0.0.1:8080/v1/alerts/1 \
  -H 'Authorization: Bearer YOUR_MONITORING_CLIENT_TOKEN'
```

A `sent` status means that **Free's API accepted the request (HTTP 200)**, not that the handset acknowledged delivery.

## 📖 API reference

| Method | Endpoint | Purpose | Auth |
| --- | --- | --- | --- |
| `GET` | `/healthz` | HTTP liveness, not SMS delivery | None (private network only) |
| `GET` | `/readyz` | Worker, SQLite, backlog and critical-failure readiness | None (private network only) |
| `POST` | `/v1/alerts` | Enqueue a new alert | Per-client bearer token |
| `GET` | `/v1/alerts/{id}` | Read own alert's status | Same per-client bearer token |

**POST payload**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `message` | string | Yes | 1–500 characters; long texts may become multiple SMS parts |
| `severity` | enum | No | `info`, `warning` (default), `critical` |
| `fingerprint` | string | No | Stable event identifier for time-window deduplication |
| `request_id` | string | No | 8–120 character idempotency identifier |
| `ttl_seconds` | integer | No | 60–604800; defaults to 3600 (1 hour) |

**Statuses:** `queued`, `sending`, `sent`, `failed`, `uncertain`, `expired`, `acknowledged`.

**HTTP behavior:** `202` indicates an accepted **queue operation**, `401` invalid token, `429` per-client quota exceeded, `422` invalid payload, `503` pending queue full. Configure request-body size limits on your private reverse proxy.

## ⚙️ Configuration

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `FREE_USER` | Free Mobile account login | Required |
| `FREE_API_KEY` | Free Mobile SMS API credential | Required |
| `CLIENT_KEYS` | JSON map of client names → bearer tokens | Required |
| `MIN_SMS_INTERVAL_SEC` | Minimum spacing between all attempts (even rejected) | `60` |
| `DAILY_SMS_LIMIT` | Maximum attempts in a rolling 24-hour window | `100` |
| `CLIENT_HOURLY_LIMIT` | Enqueued events per client per hour | `20` |
| `DEDUP_WINDOW_SEC` | Fingerprint deduplication window | `300` |
| `DB_PATH` | SQLite file path | `/data/alerts.sqlite3` |
| `DEFAULT_ALERT_TTL_SEC` | Default expiry of unsent alerts | `3600` |
| `MAX_PENDING_ALERTS` | Combined queued/sending capacity | `1000` |
| `RETENTION_DAYS` | Retention for terminal outcomes; unresolved uncertain events remain until review | `30` |
| `MAX_BACKLOG_AGE_SEC` | Age threshold for `/readyz` failure | `600` |
| `MAX_SEND_ATTEMPTS` | Maximum rate-limit retries per alert | `6` |

Free Mobile's API error codes may include `400` (bad parameters), `402` (sending too many SMS too quickly) and `403` (authentication or authorization). Only explicit rate-limit errors (`402`/`429`) are automatically retried. HTTP `5xx`, transport failures and other ambiguous responses are marked `uncertain` to avoid duplicate SMS. The gateway does not guarantee handset delivery.

## 🔒 Security

- **No real account identifiers, telephone numbers or API keys** are included in the repository.
- The sample `.env.example` contains only dummy placeholders; actual `.env` files are excluded from Git and Docker image builds.
- Use a unique client token per integration and restrict access with private networking.
- SQLite stores notification text: do not put passwords, access tokens or private personal data in message content.
- A lost network response after sending may mean an SMS **did arrive**; such alerts are flagged `uncertain`, not blindly retried.
- For incident alerts about the gateway's own host, monitor it **from another machine** and use an independent fallback channel.

See [SECURITY.md](SECURITY.md) for reporting and deployment advice.

## 🧪 Local tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

Tests mock Free's API — no real SMS is sent.

## 🧰 Operations & backups

The private CLI supports status counts, SQLite-consistent backups and operator acknowledgement of unresolved alerts:

```bash
docker compose exec gateway python scripts/ops.py stats
docker compose exec gateway python scripts/ops.py backup /data/alerts-backup.sqlite3
docker compose cp gateway:/data/alerts-backup.sqlite3 ./alerts-backup.sqlite3
chmod 600 ./alerts-backup.sqlite3
```

**Move backups off-host and test a restore.** Acknowledging an alert never resends it or confirms delivery. See the [French operations runbook](docs/OPERATIONS.fr.md) and [production checklist](docs/PRODUCTION_CHECKLIST.fr.md).

## 🛣️ Roadmap

- [ ] Small authenticated web dashboard with delivery history
- [ ] Telegram/email fallback channel
- [x] TTL, local retention policy and bounded backlog
- [ ] Metrics, independent fallback and verified restore rehearsal
- [ ] Multi-instance coordination and observability
- [ ] Systemd/non-Docker deployment guide

## 📄 License

MIT — see [LICENSE](LICENSE). Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).