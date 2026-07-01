"""Centralized input validation — single source of truth for names, paths, and config.

Imported by models.py (API + config validation), routes.py (path/query params),
and the services (defense-in-depth path containment). All validators raise
``ValueError`` on bad input so they compose with Pydantic field validators and the
services' existing try/except handling.
"""

import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


# ── Constants ─────────────────────────────────────────────────────────────────

# Docker-style names: start alphanumeric, then alphanumeric / _ . - ; max 255 chars.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
MAX_NAME_LEN = 255
MAX_PATH_LEN = 4096
MAX_TEMPLATE_LEN = 4096
MAX_CREDENTIAL_LEN = 1024

# Telegram bot token: "<digits>:<35+ url-safe chars>".
_TOKEN_RE = re.compile(r"^\d{3,}:[A-Za-z0-9_-]{20,}$")
# Chat id: signed integer, or "@username" (>=5 word chars).
_CHAT_ID_RE = re.compile(r"^-?\d+$|^@\w{4,}$")
# remote_host: "host" or "host:port".
_REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+(?::\d{1,5})?$")

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── Names ─────────────────────────────────────────────────────────────────────

def validate_name(value: str, field: str = "name") -> str:
    """Validate a pool / volume / schedule name (Docker-style). Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if ".." in value:
        raise ValueError(f"{field} must not contain '..'")
    if "/" in value or "\\" in value:
        raise ValueError(f"{field} must not contain path separators")
    if not NAME_RE.match(value):
        raise ValueError(
            f"{field} may only contain letters, digits, '_', '.', '-', must start "
            f"with a letter or digit, and be at most {MAX_NAME_LEN} characters"
        )
    return value


def validate_backup_file(value: str, field: str = "backup_file") -> str:
    """Validate a backup archive filename: Docker-style name ending in .tar.gz."""
    value = validate_name(value, field)
    if not value.endswith(".tar.gz"):
        raise ValueError(f"{field} must be a .tar.gz archive")
    return value


# ── Permissions (chmod / chown) ──────────────────────────────────────────────

# Octal mode: 3-4 digits, each 0-7 (e.g. "755", "644", "0775").
_MODE_RE = re.compile(r"^[0-7]{3,4}$")
# A single user or group token: numeric id, or a Unix-style name. No spaces, ':',
# '/', or control chars. (Defense-in-depth — subprocess uses list args, never a shell.)
_OWNER_TOKEN_RE = re.compile(r"^[0-9]+$|^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")


def validate_mode(value: str, field: str = "mode") -> str:
    """Validate an octal permission mode (e.g. "755"). Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not _MODE_RE.match(value):
        raise ValueError(f"{field} must be an octal mode like 755 or 0644")
    return value


def validate_owner_token(value: str, field: str = "owner") -> str:
    """Validate a single user/group token (numeric id or Unix name). Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if not _OWNER_TOKEN_RE.match(value):
        raise ValueError(f"{field} must be a numeric id or a valid user/group name")
    return value


# ── Paths ─────────────────────────────────────────────────────────────────────

def validate_pool_path(value: str, field: str = "path") -> str:
    """Validate a filesystem path: absolute, no control chars, length-capped."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if _CONTROL_CHARS_RE.search(value):
        raise ValueError(f"{field} must not contain control characters")
    if len(value) > MAX_PATH_LEN:
        raise ValueError(f"{field} must be at most {MAX_PATH_LEN} characters")
    if not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute path")
    return value


def safe_join(base, *parts: str) -> Path:
    """Join ``parts`` onto ``base`` and assert the result stays inside ``base``.

    Returns the resolved Path; raises ValueError on traversal. This is the
    reusable replacement for the inline ``is_relative_to`` checks in the services.
    """
    base_resolved = Path(base).resolve()
    target = base_resolved
    for part in parts:
        target = target / part
    target_resolved = target.resolve()
    if not target_resolved.is_relative_to(base_resolved):
        raise ValueError(f"path escapes its base directory: {'/'.join(str(p) for p in parts)}")
    return target_resolved


# ── Cron ──────────────────────────────────────────────────────────────────────

# crontab day-of-week numbering: 0 and 7 are Sunday, 1=Mon .. 6=Sat.
_CRON_DOW_NAMES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']


def _convert_cron_dow_field(field: str) -> str:
    """Translate the day-of-week field from crontab numbering to APScheduler names.

    APScheduler's CronTrigger uses 0=Monday for numeric weekdays, while standard
    crontab uses 0/7=Sunday, 1=Monday. `CronTrigger.from_crontab` does NOT bridge
    this, so numeric expressions like `1,3,5` (intended Mon/Wed/Fri) would wrongly
    fire Tue/Thu/Sat. We map numeric weekdays to unambiguous names, preserving
    ranges (`1-5`) and steps (`1-5/2`), and leaving names (`mon-fri`) untouched."""
    import re
    parts = []
    for part in field.split(','):
        rng, sep, step = part.partition('/')
        rng = re.sub(r'\d+', lambda m: _CRON_DOW_NAMES[int(m.group(0)) % 7], rng)
        parts.append(rng + sep + step)
    return ','.join(parts)


def make_cron_trigger(expr: str, timezone):
    """Build an APScheduler CronTrigger from a 5-field crontab expression, with
    correct crontab day-of-week semantics (0/7=Sunday) and the given timezone.

    This is the single source of truth shared by `validate_cron` (which validates
    with a placeholder tz) and the scheduler (which schedules with the real tz),
    so what validates can always be scheduled."""
    from apscheduler.triggers.cron import CronTrigger
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)}")
    minute, hour, day, month, dow = fields
    return CronTrigger(
        minute=minute, hour=hour, day=day, month=month,
        day_of_week=_convert_cron_dow_field(dow), timezone=timezone,
    )


def validate_cron(expr: str, field: str = "cron") -> str:
    """Validate a cron expression via APScheduler. Raises ValueError on bad input."""
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError(f"{field} must not be empty")
    expr = expr.strip()
    try:
        make_cron_trigger(expr, "UTC")
    except Exception as e:
        raise ValueError(f"invalid {field} expression: {e}")
    return expr


# ── Notification fields ─────────────────────────────────────────────────────────

def validate_telegram_token(value: str, field: str = "token") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    value = value.strip()
    if len(value) > MAX_CREDENTIAL_LEN or not _TOKEN_RE.match(value):
        raise ValueError(f"{field} is not a valid Telegram bot token")
    return value


def validate_chat_id(value: str, field: str = "chat_id") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    value = value.strip()
    if not _CHAT_ID_RE.match(value):
        raise ValueError(f"{field} must be a numeric id or @username")
    return value


def validate_thread_id(value: Optional[str], field: str = "message_thread_id") -> Optional[str]:
    if value is None or value == "":
        return None
    value = str(value).strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError(f"{field} must be numeric")
    return value


def validate_server_url(value: str, field: str = "server_url") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    value = value.strip()
    if len(value) > MAX_PATH_LEN:
        raise ValueError(f"{field} is too long")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field} must be an http(s) URL")
    if not parsed.netloc:
        raise ValueError(f"{field} must include a host")
    return value.rstrip("/")


def validate_remote_host(value: str, field: str = "remote_host") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    value = value.strip()
    if not _REMOTE_HOST_RE.match(value):
        raise ValueError(f"{field} must be 'host' or 'host:port'")
    if ":" in value:
        port = int(value.split(":", 1)[1])
        if not (1 <= port <= 65535):
            raise ValueError(f"{field} port must be between 1 and 65535")
    return value
