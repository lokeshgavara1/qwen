"""
analytics.py — Phase 2: Usage Analytics Service (MySQL Backend)
================================================================
Provides high-performance, asynchronous aggregation queries for the AI Gateway.
Directly reads from MySQL `request_log` and `key_quotas` tables.
"""

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import rate_limiter

SUPPORTED_RANGES = {
    "1h": {"seconds": 3600, "interval_sec": 60, "format": "%H:%M", "label": "Last 1 Hour"},
    "24h": {"seconds": 86400, "interval_sec": 3600, "format": "%m-%d %H:00", "label": "Last 24 Hours"},
    "7d": {"seconds": 604800, "interval_sec": 86400, "format": "%Y-%m-%d", "label": "Last 7 Days"},
    "30d": {"seconds": 2592000, "interval_sec": 86400, "format": "%Y-%m-%d", "label": "Last 30 Days"},
}


def validate_time_range(time_range: str) -> Tuple[str, float, int, str]:
    """
    Validates range parameter and returns (range_key, cutoff_timestamp, interval_sec, date_format).
    Raises ValueError on invalid range.
    """
    range_key = (time_range or "24h").lower().strip()
    if range_key not in SUPPORTED_RANGES:
        raise ValueError(f"Invalid time range '{time_range}'. Supported ranges: {list(SUPPORTED_RANGES.keys())}")
    cfg = SUPPORTED_RANGES[range_key]
    cutoff = time.time() - cfg["seconds"]
    return range_key, cutoff, cfg["interval_sec"], cfg["format"]


###############################################################################
# SUMMARY METRICS
###############################################################################

async def get_summary(identity: Optional[str] = None, time_range: str = "24h") -> dict:
    """
    Aggregates top-level KPI metrics for the specified time range.
    If identity is None, aggregates across all users (Admin view).
    """
    range_key, cutoff, _, _ = validate_time_range(time_range)
    pool = await rate_limiter.get_pool()

    where_clause = "WHERE created_at >= %s"
    params = [cutoff]
    if identity:
        where_clause += " AND identity = %s"
        params.append(identity)

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Aggregate request counts and status
            query = f"""
                SELECT
                    COUNT(*) as total_requests,
                    SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as successful_requests,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as failed_requests,
                    SUM(CASE WHEN status = 'rate_limited' THEN 1 ELSE 0 END) as rate_limited_requests,
                    SUM(CASE WHEN status = 'rate_limited' AND (intent = 'burst' OR intent IS NULL) THEN 1 ELSE 0 END) as burst_events,
                    SUM(CASE WHEN status = 'rate_limited' AND intent = 'hourly' THEN 1 ELSE 0 END) as quota_events,
                    COALESCE(SUM(tokens), 0) as total_tokens,
                    COALESCE(AVG(CASE WHEN latency_ms IS NOT NULL AND latency_ms > 0 THEN latency_ms END), 0) as avg_latency_ms,
                    COALESCE(MIN(CASE WHEN latency_ms IS NOT NULL AND latency_ms > 0 THEN latency_ms END), 0) as min_latency_ms,
                    COALESCE(MAX(CASE WHEN latency_ms IS NOT NULL AND latency_ms > 0 THEN latency_ms END), 0) as max_latency_ms
                FROM request_log
                {where_clause}
            """
            await cur.execute(query, params)
            row = await cur.fetchone()

    total = int(row[0] or 0)
    successful = int(row[1] or 0)
    failed = int(row[2] or 0)
    rate_limited = int(row[3] or 0)
    burst_events = int(row[4] or 0)
    quota_events = int(row[5] or 0)
    total_tokens = int(row[6] or 0)
    avg_latency = round(float(row[7] or 0), 1)
    min_latency = round(float(row[8] or 0), 1)
    max_latency = round(float(row[9] or 0), 1)

    completed_requests = successful + failed
    error_rate = round((failed / completed_requests * 100), 2) if completed_requests > 0 else 0.0

    return {
        "period": range_key,
        "identity_scope": identity or "global",
        "requests": {
            "total": total,
            "successful": successful,
            "failed": failed,
            "rate_limited": rate_limited,
        },
        "tokens": {
            "total": total_tokens,
            "avg_per_request": round(total_tokens / successful, 1) if successful > 0 else 0,
        },
        "latency": {
            "average_ms": avg_latency,
            "min_ms": min_latency,
            "max_ms": max_latency,
        },
        "errors": {
            "count": failed,
            "rate": error_rate,
        },
        "rate_limiting": {
            "rate_limit_events": burst_events,
            "quota_exceeded_events": quota_events,
            "total_blocked": rate_limited,
        },
    }


###############################################################################
# REQUEST TIME-SERIES
###############################################################################

async def get_request_timeseries(identity: Optional[str] = None, time_range: str = "24h") -> dict:
    """
    Returns time-series request buckets (timestamps, totals, successful, failed, tokens)
    evenly spaced for clean chart rendering.
    """
    range_key, cutoff, interval_sec, date_fmt = validate_time_range(time_range)
    now = time.time()
    pool = await rate_limiter.get_pool()

    # Pre-generate regular bucket timestamps from cutoff to now
    buckets = {}
    current_bucket_start = int(cutoff // interval_sec) * interval_sec
    while current_bucket_start <= now:
        label = datetime.fromtimestamp(current_bucket_start, tz=timezone.utc).strftime(date_fmt)
        buckets[current_bucket_start] = {
            "timestamp": label,
            "timestamp_epoch": current_bucket_start,
            "requests": 0,
            "successful": 0,
            "failed": 0,
            "rate_limited": 0,
            "tokens": 0,
            "avg_latency_ms": 0,
            "_lat_sum": 0.0,
            "_lat_count": 0,
        }
        current_bucket_start += interval_sec

    where_clause = "WHERE created_at >= %s"
    params = [cutoff]
    if identity:
        where_clause += " AND identity = %s"
        params.append(identity)

    query = f"""
        SELECT
            FLOOR(created_at / %s) * %s AS bucket_time,
            COUNT(*) as total_req,
            SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as ok_req,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as err_req,
            SUM(CASE WHEN status = 'rate_limited' THEN 1 ELSE 0 END) as rl_req,
            COALESCE(SUM(tokens), 0) as sum_tokens,
            COALESCE(SUM(latency_ms), 0) as sum_latency,
            COUNT(CASE WHEN latency_ms IS NOT NULL AND latency_ms > 0 THEN 1 END) as count_latency
        FROM request_log
        {where_clause}
        GROUP BY bucket_time
        ORDER BY bucket_time ASC
    """
    full_params = [interval_sec, interval_sec] + params

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, full_params)
            rows = await cur.fetchall()

    for row in rows:
        b_time = int(row[0])
        if b_time in buckets:
            buckets[b_time]["requests"] = int(row[1])
            buckets[b_time]["successful"] = int(row[2])
            buckets[b_time]["failed"] = int(row[3])
            buckets[b_time]["rate_limited"] = int(row[4])
            buckets[b_time]["tokens"] = int(row[5])
            lat_sum = float(row[6])
            lat_cnt = int(row[7])
            buckets[b_time]["avg_latency_ms"] = round(lat_sum / lat_cnt, 1) if lat_cnt > 0 else 0

    series_data = list(buckets.values())
    for item in series_data:
        item.pop("_lat_sum", None)
        item.pop("_lat_count", None)

    return {
        "range": range_key,
        "interval_seconds": interval_sec,
        "data": series_data,
    }


###############################################################################
# MODEL ANALYTICS
###############################################################################

async def get_model_stats(identity: Optional[str] = None, time_range: str = "24h") -> list:
    """
    Returns breakdown of requests, tokens, average latency, and error rate grouped by model.
    """
    _, cutoff, _, _ = validate_time_range(time_range)
    pool = await rate_limiter.get_pool()

    where_clause = "WHERE created_at >= %s AND model IS NOT NULL"
    params = [cutoff]
    if identity:
        where_clause += " AND identity = %s"
        params.append(identity)

    query = f"""
        SELECT
            model,
            COUNT(*) as total_requests,
            SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as failed,
            COALESCE(SUM(tokens), 0) as total_tokens,
            COALESCE(AVG(CASE WHEN latency_ms IS NOT NULL AND latency_ms > 0 THEN latency_ms END), 0) as avg_latency
        FROM request_log
        {where_clause}
        GROUP BY model
        ORDER BY total_requests DESC
    """

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    results = []
    for row in rows:
        model = row[0]
        reqs = int(row[1])
        succ = int(row[2])
        fail = int(row[3])
        tokens = int(row[4])
        avg_lat = round(float(row[5]), 1)
        err_rate = round((fail / reqs * 100), 2) if reqs > 0 else 0.0

        results.append({
            "model": model,
            "requests": reqs,
            "successful": succ,
            "failed": fail,
            "tokens": tokens,
            "average_latency_ms": avg_lat,
            "error_rate": err_rate,
        })

    return results


###############################################################################
# LATENCY ANALYTICS
###############################################################################

async def get_latency_stats(identity: Optional[str] = None, time_range: str = "24h") -> dict:
    """
    Returns latency summary along with latency distribution tiers.
    """
    _, cutoff, _, _ = validate_time_range(time_range)
    pool = await rate_limiter.get_pool()

    where_clause = "WHERE created_at >= %s AND latency_ms IS NOT NULL AND latency_ms > 0"
    params = [cutoff]
    if identity:
        where_clause += " AND identity = %s"
        params.append(identity)

    query = f"""
        SELECT
            COALESCE(AVG(latency_ms), 0) as avg_lat,
            COALESCE(MIN(latency_ms), 0) as min_lat,
            COALESCE(MAX(latency_ms), 0) as max_lat,
            SUM(CASE WHEN latency_ms < 500 THEN 1 ELSE 0 END) as under_500ms,
            SUM(CASE WHEN latency_ms >= 500 AND latency_ms < 1000 THEN 1 ELSE 0 END) as tier_500_1000ms,
            SUM(CASE WHEN latency_ms >= 1000 AND latency_ms < 3000 THEN 1 ELSE 0 END) as tier_1s_3s,
            SUM(CASE WHEN latency_ms >= 3000 AND latency_ms < 5000 THEN 1 ELSE 0 END) as tier_3s_5s,
            SUM(CASE WHEN latency_ms >= 5000 THEN 1 ELSE 0 END) as over_5s,
            COUNT(*) as total_measured
        FROM request_log
        {where_clause}
    """

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

    total = int(row[8] or 0)
    return {
        "average_ms": round(float(row[0] or 0), 1),
        "min_ms": round(float(row[1] or 0), 1),
        "max_ms": round(float(row[2] or 0), 1),
        "distribution": {
            "< 500ms": int(row[3] or 0),
            "500ms - 1s": int(row[4] or 0),
            "1s - 3s": int(row[5] or 0),
            "3s - 5s": int(row[6] or 0),
            "> 5s": int(row[7] or 0),
        },
        "total_measured_requests": total,
    }


###############################################################################
# ERROR ANALYTICS
###############################################################################

async def get_error_stats(identity: Optional[str] = None, time_range: str = "24h") -> dict:
    """
    Returns error category distribution and trend metrics.
    """
    _, cutoff, _, _ = validate_time_range(time_range)
    pool = await rate_limiter.get_pool()

    where_clause = "WHERE created_at >= %s"
    params = [cutoff]
    if identity:
        where_clause += " AND identity = %s"
        params.append(identity)

    query = f"""
        SELECT
            COUNT(*) as total_requests,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as gateway_errors,
            SUM(CASE WHEN status = 'rate_limited' AND (intent = 'burst' OR intent IS NULL) THEN 1 ELSE 0 END) as burst_blocked,
            SUM(CASE WHEN status = 'rate_limited' AND intent = 'hourly' THEN 1 ELSE 0 END) as quota_blocked,
            SUM(CASE WHEN status = 'rate_limited' THEN 1 ELSE 0 END) as total_rate_limited
        FROM request_log
        {where_clause}
    """

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

    total = int(row[0] or 0)
    errors = int(row[1] or 0)
    burst = int(row[2] or 0)
    quota = int(row[3] or 0)
    total_rl = int(row[4] or 0)

    return {
        "total_requests": total,
        "gateway_errors": errors,
        "rate_limit_burst_events": burst,
        "quota_exceeded_events": quota,
        "total_rate_limited": total_rl,
        "error_rate": round((errors / total * 100), 2) if total > 0 else 0.0,
    }


###############################################################################
# RECENT ACTIVITY
###############################################################################

async def get_recent_activity(identity: Optional[str] = None, limit: int = 20) -> list:
    """
    Returns bounded recent requests.
    Ommits all sensitive prompt and secret content.
    """
    limit = max(1, min(limit or 20, 100))
    pool = await rate_limiter.get_pool()

    where_clause = ""
    params = []
    if identity:
        where_clause = "WHERE identity = %s"
        params.append(identity)

    query = f"""
        SELECT id, identity, model, status, tokens, latency_ms, intent, created_at
        FROM request_log
        {where_clause}
        ORDER BY created_at DESC
        LIMIT %s
    """
    params.append(limit)

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    results = []
    for row in rows:
        raw_identity = row[1]
        # Mask IP or Key for public safety
        if raw_identity.startswith("ip:"):
            parts = raw_identity.replace("ip:", "").split(".")
            masked_id = f"ip:{parts[0]}.{parts[1]}.*.*" if len(parts) == 4 else raw_identity
        else:
            masked_id = raw_identity

        results.append({
            "id": row[0],
            "identity": masked_id,
            "model": row[2] or "—",
            "status": row[3],
            "tokens": row[4] if row[4] is not None else 0,
            "latency_ms": round(row[5], 1) if row[5] is not None else 0,
            "intent": row[6] or "chat",
            "timestamp": datetime.fromtimestamp(row[7], tz=timezone.utc).isoformat(),
            "created_at": row[7],
        })

    return results


###############################################################################
# API KEY USAGE (Admin Only)
###############################################################################

async def get_key_usage(time_range: str = "24h") -> list:
    """
    Returns aggregated usage broken down by API key / identity (Admin only).
    """
    _, cutoff, _, _ = validate_time_range(time_range)
    pool = await rate_limiter.get_pool()

    query = """
        SELECT
            identity,
            COUNT(*) as total_requests,
            SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'rate_limited' THEN 1 ELSE 0 END) as rate_limited,
            COALESCE(SUM(tokens), 0) as total_tokens,
            COALESCE(AVG(CASE WHEN latency_ms IS NOT NULL AND latency_ms > 0 THEN latency_ms END), 0) as avg_latency,
            MAX(created_at) as last_active
        FROM request_log
        WHERE created_at >= %s
        GROUP BY identity
        ORDER BY total_requests DESC
    """

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (cutoff,))
            rows = await cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "identity": row[0],
            "is_key": row[0].startswith("key:"),
            "requests": int(row[1]),
            "successful": int(row[2]),
            "failed": int(row[3]),
            "rate_limited": int(row[4]),
            "tokens": int(row[5]),
            "average_latency_ms": round(float(row[6]), 1),
            "last_active": datetime.fromtimestamp(row[7], tz=timezone.utc).isoformat() if row[7] else "—",
        })

    return results
