# Phase 1: Rate Limiting & Quota Management

This document provides a comprehensive reference for the Rate Limiting and Quota Management system implemented in **Phase 1** of the LLM AI Gateway.

---

## 1. Overview & Architecture

The gateway employs an asynchronous, sliding-window rate limiter backed by SQLite (`gateway_usage.db`). It protects downstream worker nodes (Ollama instances) from burst spikes and regulates usage quotas across public and authenticated clients.

```
                  ┌──────────────────────┐
                  │   Incoming Request   │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Identify Requester   │
                  │ (API Key Name / IP)  │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Check Sliding Window │
                  │ Burst & Hourly Quota │
                  └──────────┬───────────┘
                             │
            ┌────────────────┴────────────────┐
            │                                 │
     [Within Limits]                   [Limit Exceeded]
            │                                 │
            ▼                                 ▼
┌───────────────────────┐         ┌───────────────────────┐
│ Forward to Ollama /   │         │ HTTP 429 Too Many Req │
│ Worker Node           │         │ Retry-After Header    │
└───────────────────────┘         └───────────────────────┘
```

---

## 2. Rate Limit Policy

All rate limits are centralized in `rate_limiter.py` under `RATE_LIMIT_CONFIG`:

| Tier | Identity Format | Burst Limit | Hourly Quota | Storage |
| :--- | :--- | :--- | :--- | :--- |
| **Public / Anonymous** | `ip:<client_ip>` | 10 req / 60 sec | 20 req / hour | SQLite (`request_log`) |
| **Authenticated Key** | `key:<sha256[:16]>` | 10 req / 60 sec | 200 req / hour *(overridable)* | SQLite (`request_log`) |

### Identity Privacy
When an API key is provided, the gateway computes an SHA-256 digest of the key's assigned `name` (`key:<sha256_prefix>`). Plaintext secrets are never written to the request log or stored in rate-limiting tables.

---

## 3. Rate Limit Response & Headers

When a request exceeds either burst or hourly quota, the gateway immediately returns `HTTP 429 Too Many Requests`:

### Response Headers
```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Retry-After: 45
```

### Response Body
```json
{
  "error": "rate_limited",
  "detail": "Rate limit exceeded. Please wait 45 seconds before retrying.",
  "retry_after": 45,
  "limit_type": "burst",
  "burst": {
    "used": 10,
    "limit": 10,
    "remaining": 0,
    "window_sec": 60
  },
  "hourly": {
    "used": 10,
    "limit": 20,
    "remaining": 10
  }
}
```

---

## 4. API Endpoints

### 4.1. Check Current Usage
**`GET /api/usage`**

Returns the caller's current usage statistics, remaining burst, and hourly quota.

- **Authentication**: Optional (Bearer token). Returns IP-level stats if no key provided.
- **Example Response**:
```json
{
  "identity": "ip:127.0.0.1",
  "burst": {
    "used": 3,
    "limit": 10,
    "remaining": 7,
    "window_sec": 60
  },
  "hourly": {
    "used": 12,
    "limit": 20,
    "remaining": 8
  },
  "tokens": {
    "used_this_hour": 540
  },
  "successful_this_hour": 12,
  "requests_last_minute": 3,
  "requests_last_hour": 12,
  "burst_limit": 10,
  "hourly_limit": 20,
  "remaining_burst": 7,
  "remaining_hourly": 8,
  "total_tokens": 540,
  "total_requests_all_time": 45
}
```

---

### 4.2. Admin Set Quota Override
**`POST /api/admin/set-quota`**

Allows administrators to set custom burst and hourly quotas for any specific identity.

- **Authentication**: Required (`Authorization: Bearer <ADMIN_API_KEY>`). The API key must have `"name": "admin"`.
- **Request Body**:
```json
{
  "identity": "key:4f8a9b1c2d3e4f5a",
  "hourly_quota": 1000,
  "burst_limit": 30,
  "burst_window": 60
}
```
- **Validation**:
  - `identity`: string (required)
  - `hourly_quota`: integer (1 – 100,000)
  - `burst_limit`: integer (1 – 1,000)
  - `burst_window`: integer (10 – 3,600, default 60)

---

## 5. Database Schema & Maintenance

Database: `gateway_usage.db` (created automatically on startup via `rate_limiter.init_db()`).

### `request_log` Table
```sql
CREATE TABLE IF NOT EXISTS request_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity    TEXT    NOT NULL,
    model       TEXT,
    status      TEXT    NOT NULL DEFAULT 'ok',
    tokens      INTEGER,
    latency_ms  REAL,
    intent      TEXT,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl_identity_time ON request_log (identity, created_at);
```

### `key_quotas` Table (Overrides)
```sql
CREATE TABLE IF NOT EXISTS key_quotas (
    identity        TEXT PRIMARY KEY,
    hourly_quota    INTEGER NOT NULL,
    burst_limit     INTEGER NOT NULL,
    burst_window    INTEGER NOT NULL,
    updated_at      REAL    NOT NULL
);
```

### Data Retention & Cleanup
A background task runs every hour (`CLEANUP_INTERVAL_SEC = 3600`) to delete records older than 7 days (`RECORD_RETENTION_DAYS = 7`), preventing unbounded database growth.

---

## 6. Frontend Integration

1. **Sidebar Hourly Usage Widget**: Displays live hourly request count and quota bar in the sidebar footer (`Requests: X / Y`).
2. **User-Friendly Error Handling**: When receiving a 429 response, the frontend throws a `RateLimitError` and displays an amber toast with the specific cooldown countdown (`⏳ Rate limit reached. Please wait Xs before trying again.`).
