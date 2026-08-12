"""
Ysilitel API
============

Standalone key-management API for Python 3.12+ and PostgreSQL.

Install the only runtime dependency:
    pip install "psycopg[binary]"

The hosting provider should terminate HTTPS in front of this process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import string
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote
from wsgiref.simple_server import WSGIRequestHandler, make_server

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError as exc:  # pragma: no cover - startup guidance
    raise SystemExit(
        'Missing dependency. Install it with: pip install "psycopg[binary]"'
    ) from exc


# ============================================================
# НАСТРОЙКИ. ВПИШИТЕ ЗНАЧЕНИЯ ТОЛЬКО В ЭТИ СТРОКИ.
# ============================================================
DATABASE_URL = "postgresql://postgres.youlaigbankwxljbgwxc:4gLMDOmt16wYmgOa@aws-1-eu-west-1.pooler.supabase.com:5432/postgres"
ADMIN_API_TOKEN = "q8kP2vT1mL9xR4sZ7nA0cD6eF3gH5jK2"

HOST = "0.0.0.0"
PORT = 5000
CORS_ORIGIN = "*"
RATE_LIMIT_PER_MINUTE = 60
DB_CONNECT_TIMEOUT = 10

if DATABASE_URL.startswith("PASTE_"):
    raise SystemExit(
        "В app.py не заполнена DATABASE_URL. "
        "Вставьте строку подключения PostgreSQL в блок НАСТРОЙКИ."
    )
if ADMIN_API_TOKEN.startswith("PASTE_"):
    raise SystemExit(
        "В app.py не заполнен ADMIN_API_TOKEN. "
        "Задайте длинный случайный токен в блоке НАСТРОЙКИ."
    )

KEY_PREFIX = "Усилитель"
KEY_ALPHABET = string.ascii_uppercase + string.digits
KEY_RANDOM_LENGTH = 9
MAX_BODY_BYTES = 64 * 1024

if not DATABASE_URL:
    raise SystemExit(
        "DATABASE_URL is not set. Set it to your PostgreSQL connection string "
        "before starting app.py."
    )


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class RateLimiter:
    """Small in-process limiter for public endpoints."""

    def __init__(self, limit: int):
        self.limit = limit
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[int, int]] = {}

    def allowed(self, client: str, endpoint: str) -> bool:
        now = int(time.time())
        bucket = now // 60
        key = (client, endpoint)
        with self._lock:
            count_bucket, count = self._buckets.get(key, (bucket, 0))
            if count_bucket != bucket:
                self._buckets[key] = (bucket, 1)
                self._remove_old(bucket)
                return True
            if count >= self.limit:
                return False
            self._buckets[key] = (bucket, count + 1)
            return True

    def _remove_old(self, current_bucket: int) -> None:
        if len(self._buckets) < 2000:
            return
        self._buckets = {
            key: value
            for key, value in self._buckets.items()
            if value[0] >= current_bucket - 1
        }


rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def json_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def hours_remaining(expires_at: datetime | None, now: datetime | None = None) -> int:
    if expires_at is None:
        return 0
    current = now or utc_now()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return max(0, math.ceil((expires_at - current).total_seconds() / 3600))


def connect() -> psycopg.Connection:
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=DB_CONNECT_TIMEOUT,
        row_factory=dict_row,
    )


def init_database() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS keys (
        id BIGSERIAL PRIMARY KEY,
        key_value TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('DAY', 'HOUR')),
        duration INTEGER NOT NULL CHECK (duration > 0),
        max_devices INTEGER NOT NULL CHECK (max_devices > 0),
        max_percent INTEGER NOT NULL CHECK (max_percent BETWEEN 0 AND 100),
        status TEXT NOT NULL DEFAULT 'waiting'
            CHECK (status IN ('waiting', 'active', 'full', 'expired', 'deleted')),
        first_activation TIMESTAMPTZ NULL,
        expires_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted_at TIMESTAMPTZ NULL
    );

    CREATE TABLE IF NOT EXISTS activations (
        id BIGSERIAL PRIMARY KEY,
        key_id BIGINT NOT NULL REFERENCES keys(id),
        device_id TEXT NOT NULL,
        activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_check TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (key_id, device_id)
    );

    CREATE INDEX IF NOT EXISTS idx_keys_key_value ON keys(key_value);
    CREATE INDEX IF NOT EXISTS idx_keys_status ON keys(status);
    CREATE INDEX IF NOT EXISTS idx_activations_device_id ON activations(device_id);
    CREATE INDEX IF NOT EXISTS idx_activations_key_id ON activations(key_id);
    """
    with connect() as conn:
        conn.execute(schema)


def generate_key_value(duration: int, key_type: str) -> str:
    # The visible duration part is intentional: 1DAY, 7DAYS, 1HOUR, 12HOURS.
    if key_type == "DAY":
        unit = "DAY" if duration == 1 else "DAYS"
    else:
        unit = "HOUR" if duration == 1 else "HOURS"
    random_part = "".join(
        secrets.choice(KEY_ALPHABET) for _ in range(KEY_RANDOM_LENGTH)
    )
    return f"{KEY_PREFIX}-{duration}{unit}-{random_part}"


def normalize_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "validation_error", "type must be DAY or HOUR")
    normalized = value.strip().upper()
    if normalized in {"DAYS", "DAY"}:
        return "DAY"
    if normalized in {"HOURS", "HOUR"}:
        return "HOUR"
    raise ApiError(400, "validation_error", "type must be DAY or HOUR")


def positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ApiError(400, "validation_error", f"{field} must be a positive integer")
    return value


def validate_create_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(400, "validation_error", "JSON object is required")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise ApiError(
            400,
            "validation_error",
            "name must be a non-empty string up to 120 characters",
        )

    key_type = normalize_type(payload.get("type"))
    duration = positive_int(payload.get("duration"), "duration")
    max_devices = positive_int(payload.get("max_devices"), "max_devices")
    max_percent = payload.get("max_percent")
    if (
        isinstance(max_percent, bool)
        or not isinstance(max_percent, int)
        or not 0 <= max_percent <= 100
    ):
        raise ApiError(
            400,
            "validation_error",
            "max_percent must be an integer from 0 to 100",
        )

    return {
        "name": name.strip(),
        "type": key_type,
        "duration": duration,
        "max_devices": max_devices,
        "max_percent": max_percent,
    }


def validate_string(value: Any, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ApiError(
            400,
            "validation_error",
            f"{field} must be a non-empty string up to {max_length} characters",
        )
    return value.strip()


def status_for(row: dict[str, Any], used_devices: int, now: datetime | None = None) -> str:
    current = now or utc_now()
    if row.get("deleted_at") is not None or row.get("status") == "deleted":
        return "deleted"
    expires_at = row.get("expires_at")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= current:
            return "expired"
    if row.get("first_activation") is None:
        return "waiting"
    if used_devices >= int(row["max_devices"]):
        return "full"
    return "active"


def public_activation_response(row: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "success": True,
        "valid": True,
        "percent": row["max_percent"],
        "expires_at": json_time(row["expires_at"]),
        "hours_remaining": hours_remaining(row["expires_at"]),
        "key_status": status,
    }


def admin_key_response(row: dict[str, Any], status: str | None = None) -> dict[str, Any]:
    actual_status = status or row["calculated_status"]
    return {
        "id": row["id"],
        "key_value": row["key_value"],
        "name": row["name"],
        "type": row["type"],
        "duration": row["duration"],
        "max_devices": row["max_devices"],
        "used_devices": row["used_devices"],
        "max_percent": row["max_percent"],
        "status": actual_status,
        "first_activation": json_time(row["first_activation"]),
        "created_at": json_time(row["created_at"]),
        "expires_at": json_time(row["expires_at"]),
        "time_left": (
            f"{hours_remaining(row['expires_at'])} hours"
            if row["expires_at"] is not None
            else "not started"
        ),
        "time_left_seconds": max(
            0,
            int(
                (
                    (row["expires_at"] - utc_now()).total_seconds()
                    if row["expires_at"] is not None
                    else 0
                )
            ),
        ),
    }


def select_key_by_id(conn: psycopg.Connection, key_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT k.*,
               COUNT(a.id)::int AS used_devices
        FROM keys k
        LEFT JOIN activations a ON a.key_id = k.id
        WHERE k.id = %s
        GROUP BY k.id
        """,
        (key_id,),
    ).fetchone()
    if row is None:
        return None
    row["calculated_status"] = status_for(row, row["used_devices"])
    return row


def select_key_by_value(
    conn: psycopg.Connection, key_value: str, for_update: bool = False
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = conn.execute(
        f"SELECT * FROM keys WHERE key_value = %s{suffix}",
        (key_value,),
    ).fetchone()
    if row is None:
        return None
    used = conn.execute(
        "SELECT COUNT(*)::int AS count FROM activations WHERE key_id = %s",
        (row["id"],),
    ).fetchone()["count"]
    row["used_devices"] = used
    row["calculated_status"] = status_for(row, used)
    return row


def create_key(payload: Any) -> dict[str, Any]:
    data = validate_create_payload(payload)
    for _ in range(10):
        key_value = generate_key_value(data["duration"], data["type"])
        try:
            with connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO keys
                        (key_value, name, type, duration, max_devices, max_percent)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        key_value,
                        data["name"],
                        data["type"],
                        data["duration"],
                        data["max_devices"],
                        data["max_percent"],
                    ),
                ).fetchone()
                row["used_devices"] = 0
                row["calculated_status"] = "waiting"
                result = admin_key_response(row, "waiting")
                result["success"] = True
                return result
        except psycopg.errors.UniqueViolation:
            continue
    raise ApiError(500, "server_error", "Could not generate a unique key")


def list_keys() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT k.*,
                   COUNT(a.id)::int AS used_devices
            FROM keys k
            LEFT JOIN activations a ON a.key_id = k.id
            GROUP BY k.id
            ORDER BY k.id ASC
            """
        ).fetchall()
    result = []
    for row in rows:
        row["calculated_status"] = status_for(row, row["used_devices"])
        result.append(
            {
                "id": row["id"],
                "key_value": row["key_value"],
                "name": row["name"],
                "type": row["type"],
                "duration": row["duration"],
                "max_devices": row["max_devices"],
                "used_devices": row["used_devices"],
                "max_percent": row["max_percent"],
                "status": row["calculated_status"],
                "created_at": json_time(row["created_at"]),
            }
        )
    return result


def activate_key(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(400, "validation_error", "JSON object is required")
    key_value = validate_string(payload.get("key_code"), "key_code", 120)
    device_id = validate_string(payload.get("device_id"), "device_id")

    with connect() as conn:
        row = select_key_by_value(conn, key_value, for_update=True)
        if row is None:
            raise ApiError(404, "invalid_key", "Key is invalid")
        status = row["calculated_status"]
        if status == "deleted":
            raise ApiError(403, "key_deleted", "Key was deleted")
        if status == "expired":
            raise ApiError(403, "key_expired", "Key expired")

        existing = conn.execute(
            """
            SELECT id FROM activations
            WHERE key_id = %s AND device_id = %s
            """,
            (row["id"], device_id),
        ).fetchone()

        if existing is None and status == "full":
            raise ApiError(409, "key_full", "All available devices are occupied")

        now = utc_now()
        if row["first_activation"] is None:
            expires_at = now + (
                timedelta(days=row["duration"])
                if row["type"] == "DAY"
                else timedelta(hours=row["duration"])
            )
            conn.execute(
                """
                UPDATE keys
                SET first_activation = %s, expires_at = %s, status = 'active'
                WHERE id = %s
                """,
                (now, expires_at, row["id"]),
            )
            row["first_activation"] = now
            row["expires_at"] = expires_at
            row["calculated_status"] = "active"

        if existing is None:
            conn.execute(
                """
                INSERT INTO activations (key_id, device_id, activated_at, last_check)
                VALUES (%s, %s, %s, %s)
                """,
                (row["id"], device_id, now, now),
            )
            row["used_devices"] += 1
        else:
            conn.execute(
                "UPDATE activations SET last_check = %s WHERE id = %s",
                (now, existing["id"]),
            )

        final_status = status_for(row, row["used_devices"], now)
        return public_activation_response(row, final_status)


def check_key(key_value: str, device_id: str) -> dict[str, Any]:
    key_value = validate_string(unquote(key_value), "key", 120)
    device_id = validate_string(unquote(device_id), "device_id")

    with connect() as conn:
        row = select_key_by_value(conn, key_value)
        if row is None:
            raise ApiError(404, "invalid_key", "Key is invalid")
        if row["calculated_status"] == "deleted":
            raise ApiError(403, "key_deleted", "Key was deleted")
        if row["calculated_status"] == "expired":
            raise ApiError(403, "key_expired", "Key expired")
        if row["calculated_status"] == "waiting":
            raise ApiError(403, "invalid_key", "Key has not been activated")

        activation = conn.execute(
            """
            SELECT id FROM activations
            WHERE key_id = %s AND device_id = %s
            """,
            (row["id"], device_id),
        ).fetchone()
        if activation is None:
            raise ApiError(403, "invalid_device", "Device is not activated for this key")

        conn.execute(
            "UPDATE activations SET last_check = %s WHERE id = %s",
            (utc_now(), activation["id"]),
        )
        return public_activation_response(row, row["calculated_status"])


def parse_id(value: str) -> int:
    try:
        key_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "validation_error", "id must be an integer") from exc
    if key_id <= 0:
        raise ApiError(400, "validation_error", "id must be a positive integer")
    return key_id


def delete_key(key_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT id FROM keys WHERE id = %s", (key_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "Key not found")
        conn.execute(
            """
            UPDATE keys
            SET status = 'deleted', deleted_at = COALESCE(deleted_at, NOW())
            WHERE id = %s
            """,
            (key_id,),
        )
    return {"success": True, "deleted": True, "id": key_id}


def read_json_body(environ: dict[str, Any]) -> Any:
    try:
        content_length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError as exc:
        raise ApiError(400, "validation_error", "Invalid Content-Length") from exc
    if content_length <= 0 or content_length > MAX_BODY_BYTES:
        raise ApiError(400, "validation_error", "Request body is missing or too large")
    raw = environ["wsgi.input"].read(content_length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "validation_error", "Request body must be valid JSON") from exc


def admin_authorized(environ: dict[str, Any]) -> bool:
    if not ADMIN_API_TOKEN:
        raise ApiError(503, "server_not_configured", "ADMIN_API_TOKEN is not configured")
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return False
    supplied = header[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, ADMIN_API_TOKEN)


def json_response(
    start_response: Any,
    payload: dict[str, Any],
    status: int = 200,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("Access-Control-Allow-Origin", CORS_ORIGIN),
        ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
        ("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    phrase = {
        200: "OK",
        201: "Created",
        204: "No Content",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        409: "Conflict",
        429: "Too Many Requests",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "Error")
    start_response(f"{status} {phrase}", headers)
    return [body]


def error_payload(error: ApiError) -> dict[str, Any]:
    return {"success": False, "error": error.error, "message": error.message}


def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "") or "/"
    client = environ.get("REMOTE_ADDR", "unknown")

    if method == "OPTIONS":
        return json_response(start_response, {"success": True}, 204)

    try:
        if method == "GET" and path == "/api/status":
            with connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return json_response(
                start_response,
                {"success": True, "status": "online", "time": json_time(utc_now())},
            )

        if path == "/api/activate" or path.startswith("/api/check/"):
            endpoint = "activate" if path == "/api/activate" else "check"
            if not rate_limiter.allowed(client, endpoint):
                raise ApiError(429, "rate_limited", "Too many requests")

        if method == "POST" and path == "/api/activate":
            return json_response(start_response, activate_key(read_json_body(environ)))

        check_parts = path.split("/")
        if method == "GET" and len(check_parts) == 5 and check_parts[1:3] == [
            "api",
            "check",
        ]:
            return json_response(
                start_response, check_key(check_parts[3], check_parts[4])
            )

        is_admin_route = (
            path == "/api/keys"
            or path.startswith("/api/keys/")
            or path.startswith("/api/key/")
        )
        if is_admin_route:
            if not admin_authorized(environ):
                raise ApiError(401, "unauthorized", "Administrative token is required")

            if method == "POST" and path == "/api/keys":
                return json_response(start_response, create_key(read_json_body(environ)), 201)

            if method == "GET" and path == "/api/keys":
                return json_response(
                    start_response, {"success": True, "keys": list_keys()}
                )

            detail_parts = path.split("/")
            if (
                method == "GET"
                and len(detail_parts) == 4
                and detail_parts[1:3] in (["api", "keys"], ["api", "key"])
            ):
                key_id = parse_id(detail_parts[3])
                with connect() as conn:
                    row = select_key_by_id(conn, key_id)
                if row is None:
                    raise ApiError(404, "not_found", "Key not found")
                return json_response(
                    start_response,
                    {"success": True, "key": admin_key_response(row)},
                )

            if method == "DELETE" and len(detail_parts) == 4 and detail_parts[1:3] == [
                "api",
                "keys",
            ]:
                return json_response(start_response, delete_key(parse_id(detail_parts[3])))

        raise ApiError(404, "not_found", "Endpoint not found")
    except ApiError as exc:
        return json_response(start_response, error_payload(exc), exc.status)
    except Exception:
        print("Unhandled API error", file=sys.stderr)
        return json_response(
            start_response,
            {"success": False, "error": "server_error", "message": "Internal server error"},
            500,
        )


class QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> None:
    init_database()
    httpd = make_server(HOST, PORT, application, handler_class=QuietRequestHandler)
    print(f"Ysilitel API listening on http://{HOST}:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Ysilitel API", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()