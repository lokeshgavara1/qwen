# Phase 2: Usage Analytics Dashboard (MySQL Backend)

This document provides a comprehensive technical guide for the Usage Analytics service, API endpoints, and interactive dashboard implemented in **Phase 2** of the LLM AI Gateway.

---

## 1. Overview & Architecture

Phase 2 introduces a real-time, observational analytics engine directly on top of the MySQL storage layer (`ai_gateway` database) managed via `aiomysql` connection pooling.

```
                  ┌────────────────────────┐
                  │ Client / Dashboard UI  │
                  └───────────┬────────────┘
                              │
                              ▼
                  ┌────────────────────────┐
                  │  FastAPI Analytics API │
                  │  (Scoping & Auth Layer)│
                  └───────────┬────────────┘
                              │
                              ▼
                  ┌────────────────────────┐
                  │   analytics.py Service │
                  │   Aggregation Queries  │
                  └───────────┬────────────┘
                              │
                              ▼
                  ┌────────────────────────┐
                  │ MySQL Connection Pool  │
                  │   request_log table    │
                  └────────────────────────┘
```

---

## 2. Database Schema (MySQL)

Database: `ai_gateway` (Character set: `utf8mb4`, Collation: `utf8mb4_unicode_ci`)

### `request_log` Table
```sql
CREATE TABLE IF NOT EXISTS request_log (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    identity    VARCHAR(64) NOT NULL,
    model       VARCHAR(64) NULL,
    status      VARCHAR(32) NOT NULL DEFAULT 'ok',
    tokens      INT NULL,
    latency_ms  DOUBLE NULL,
    intent      VARCHAR(32) NULL,
    created_at  DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Composite & Query Indexes
CREATE INDEX idx_rl_identity_time ON request_log (identity, created_at);
CREATE INDEX idx_rl_created_at ON request_log (created_at);
CREATE INDEX idx_rl_model_time ON request_log (model, created_at);
CREATE INDEX idx_rl_status ON request_log (status);
```

### `key_quotas` Table
```sql
CREATE TABLE IF NOT EXISTS key_quotas (
    identity        VARCHAR(64) PRIMARY KEY,
    hourly_quota    INT NOT NULL,
    burst_limit     INT NOT NULL,
    burst_window    INT NOT NULL,
    updated_at      DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## 3. Supported Time Ranges

Analytics endpoints support validated, non-expensive sliding windows:

| Range Identifier | Window Duration | Bucket Interval | Date Format Label |
| :--- | :--- | :--- | :--- |
| `1h` | Last 1 Hour (3,600s) | 60 seconds (1 minute) | `HH:MM` |
| `24h` *(default)* | Last 24 Hours (86,400s) | 3,600 seconds (1 hour) | `MM-DD HH:00` |
| `7d` | Last 7 Days (604,800s) | 86,400 seconds (1 day) | `YYYY-MM-DD` |
| `30d` | Last 30 Days (2,592,000s) | 86,400 seconds (1 day) | `YYYY-MM-DD` |

---

## 4. Analytics REST API Endpoints

### 4.1. Summary KPIs
**`GET /api/analytics/summary?range=24h`**

Returns aggregated request counts, token usage, latency (avg, min, max), error rate %, and rate-limiting / quota events.

- **Sample Response**:
```json
{
  "period": "24h",
  "identity_scope": "global",
  "requests": {
    "total": 1250,
    "successful": 1198,
    "failed": 32,
    "rate_limited": 20
  },
  "tokens": {
    "total": 384500,
    "avg_per_request": 320.9
  },
  "latency": {
    "average_ms": 1840.5,
    "min_ms": 320.0,
    "max_ms": 8420.0
  },
  "errors": {
    "count": 32,
    "rate": 2.6
  },
  "rate_limiting": {
    "rate_limit_events": 15,
    "quota_exceeded_events": 5,
    "total_blocked": 20
  }
}
```

---

### 4.2. Request Time-Series Trends
**`GET /api/analytics/requests?range=24h`**

Returns continuous time-series buckets populated with totals, successes, failures, rate-limit events, tokens, and average latency.

- **Sample Response**:
```json
{
  "range": "24h",
  "interval_seconds": 3600,
  "data": [
    {
      "timestamp": "09-15 08:00",
      "timestamp_epoch": 1789459200,
      "requests": 52,
      "successful": 49,
      "failed": 2,
      "rate_limited": 1,
      "tokens": 14200,
      "avg_latency_ms": 1450.2
    }
  ]
}
```

---

### 4.3. Model Usage Breakdown
**`GET /api/analytics/models?range=24h`**

Returns model-level distribution of requests, token consumption, average latency, and error rate.

- **Sample Response**:
```json
[
  {
    "model": "mistral:7b",
    "requests": 730,
    "successful": 715,
    "failed": 15,
    "tokens": 202500,
    "average_latency_ms": 1650.4,
    "error_rate": 2.05
  },
  {
    "model": "qwen2.5vl:7b",
    "requests": 520,
    "successful": 510,
    "failed": 10,
    "tokens": 182000,
    "average_latency_ms": 2100.0,
    "error_rate": 1.92
  }
]
```

---

### 4.4. Latency Distribution
**`GET /api/analytics/latency?range=24h`**

Returns latency statistics alongside histogram distribution tiers (`<500ms`, `500ms-1s`, `1s-3s`, `3s-5s`, `>5s`).

---

### 4.5. Recent Activity Log
**`GET /api/analytics/recent?limit=20`**

Returns bounded recent requests with status badges, model names, tokens, latency, and masked identities.

---

### 4.6. API Key Usage (Admin Only)
**`GET /api/analytics/keys?range=24h`**

Returns aggregated usage broken down by API key / identity.
- **Authorization**: Requires `Authorization: Bearer <ADMIN_KEY>`.

---

## 5. Security & Scoping Rules

1. **User Isolation**: Standard API keys and public IP callers receive analytics strictly filtered to their own requests (`identity = "key:<hash>"` or `"ip:<ip>"`).
2. **Admin Privileges**: Only API keys carrying `"name": "admin"` can query system-wide aggregate metrics or access the `/api/analytics/keys` endpoint.
3. **Data Masking**: Prompts, credentials, and raw client IP segments are masked in recent activity logs.

---

## 6. Interactive Dashboard UI

The gateway provides a responsive, dark-themed analytics dashboard at:
👉 **`http://localhost:8000/dashboard`**

Features:
- **Time Range Switcher**: Instant switching between `1h`, `24h`, `7d`, and `30d`.
- **Live SVG Charts**: Request activity time-series (total, success, fail, rate-limited) and model breakdown bars.
- **KPI Summary Cards**: Real-time totals, error rates, token accounting, and latency ranges.
- **Auto-Refresh**: Configurable intervals (5s, 15s, 30s, or manual).
