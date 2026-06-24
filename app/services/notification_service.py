"""Notification service — Telegram alerts on task completion."""

import datetime
import json
import re
import socket
import time
import urllib.request
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

# Matches a single {placeholder} token (word chars only — no attribute/index access).
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def format_duration(seconds: float) -> str:
    """Format a duration given in seconds as hh:mm:ss.SSS."""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "00:00:00.000"
    if total < 0:
        total = 0.0
    ms = round(total * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


DEFAULT_TEMPLATE = (
    "\U0001f514 *{task_type_label}* {status_emoji}\n"
    "`{target}`\n"
    "\n"
    "Status: *{status}*\n"
    "⏱ {elapsed}\n"
    "⏱ Started: {started_at}\n"
    "\U0001f3c1 Finished: {timestamp}\n"
    "\U0001f5a5 Host: {hostname}"
    "{params_block}"
    "{error_block}"
)

_TASK_TYPE_LABELS: Dict[str, str] = {
    "backup": "Backup",
    "scheduled_backup": "Scheduled Backup",
    "migrate": "Migration",
    "restore": "Restore",
    "delete": "Delete",
    "rename": "Rename",
    "create": "Create Volume",
    "permissions": "Permissions",
    "bulk_backup": "Bulk Backup",
    "bulk_migrate": "Bulk Migration",
    "bulk_restore": "Bulk Restore",
    "bulk_delete": "Bulk Delete",
    "bulk_permissions": "Bulk Permissions",
}

_PARAM_LABELS: Dict[str, str] = {
    "source_pool":       "Source Pool",
    "source_volume":     "Source Volume",
    "dest_pool":         "Destination Pool",
    "dest_volume":       "Destination Volume",
    "dest_volume_name":  "Dest Volume Name",
    "backup_pool":       "Backup Pool",
    "backup_file":       "Backup File",
    "pool":              "Pool",
    "volume_name":       "Volume",
    "new_name":          "New Name",
    "mode":              "Mode",
    "owner":             "Owner",
    "verify":            "Verify",
    "delete_source":     "Delete Source",
    "compress":          "Compress",
    "exclude_patterns":  "Exclude Patterns",
    "job_name":          "Job Name",
    "parent_job":        "Job Name",
    "retention_count":   "Retention",
    "volumes":           "Volumes",
    "total_volumes":     "Total Volumes",
    "completed_volumes": "Completed",
    "failed_volumes":    "Failed",
    "total_items":       "Total Items",
}

# Params that are internal implementation details, not meaningful to the user.
_HIDDEN_PARAMS = {"scheduled", "conflict_resolution", "rename_dest",
                  "bulk", "parent_task_id", "action", "label", "items"}


class NotificationService:
    def __init__(self, config_dir: str):
        self._file = Path(config_dir) / "vshipper_notifications.json"
        self._configs: Dict[str, dict] = {}
        self._lock = Lock()
        self._load()

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def list_all(self) -> List[dict]:
        with self._lock:
            return list(self._configs.values())

    def get(self, cfg_id: str) -> Optional[dict]:
        with self._lock:
            return self._configs.get(cfg_id)

    def create(self, data: dict) -> dict:
        cfg = dict(data)
        cfg["id"] = str(uuid.uuid4())
        cfg.setdefault("enabled", True)
        with self._lock:
            self._configs[cfg["id"]] = cfg
            self._save()
        return cfg

    def update(self, cfg_id: str, data: dict) -> Optional[dict]:
        with self._lock:
            if cfg_id not in self._configs:
                return None
            cfg = dict(data)
            cfg["id"] = cfg_id
            cfg.setdefault("enabled", self._configs[cfg_id].get("enabled", True))
            self._configs[cfg_id] = cfg
            self._save()
            return cfg

    def delete(self, cfg_id: str) -> bool:
        with self._lock:
            if cfg_id not in self._configs:
                return False
            del self._configs[cfg_id]
            self._save()
            return True

    def toggle(self, cfg_id: str) -> Optional[dict]:
        with self._lock:
            if cfg_id not in self._configs:
                return None
            self._configs[cfg_id]["enabled"] = not self._configs[cfg_id].get("enabled", True)
            self._save()
            return dict(self._configs[cfg_id])

    def test(self, cfg_id: str) -> bool:
        """Send a test message to verify connectivity."""
        cfg = self.get(cfg_id)
        if not cfg:
            return False
        hostname = socket.gethostname()
        text = (
            "\U0001f514 *Test Notification*\n"
            "v-shipper is connected — notifications are working.\n"
            f"\U0001f5a5 Host: {hostname}"
        )
        return self._send(cfg, text)

    # ── Notification dispatch ─────────────────────────────────────────────────

    def notify_task_completion(self, task: dict):
        """Route a completed task to all matching notification configs."""
        topic = self._task_to_topic(task)
        if not topic:
            return
        with self._lock:
            configs = list(self._configs.values())
        for cfg in configs:
            if not cfg.get("enabled"):
                continue
            if topic not in (cfg.get("topics") or []):
                continue
            if cfg.get("on_failure_only") and task.get("status") != "failed":
                continue
            self._send(cfg, self._build_message(cfg, task))

    # ── Internal helpers ─────────────────────────────────────────────────────

    _ITEM_TOPICS = ("backup", "migrate", "restore", "delete", "rename", "create", "permissions")

    def _task_to_topic(self, task: dict) -> Optional[str]:
        params = task.get("params", {}) or {}
        # Sub-tasks of a scheduled run or a bulk action are reported by their
        # summary task, not individually.
        if params.get("scheduled") or params.get("parent_task_id"):
            return None
        t = (task.get("type") or task.get("task_type") or "").lower()
        if t == "scheduled_backup":
            return "schedule"
        if t.startswith("bulk_"):
            # A bulk action notifies its base topic (bulk_backup → "backup", …).
            base = t[len("bulk_"):]
            return base if base in self._ITEM_TOPICS else None
        if t in self._ITEM_TOPICS:
            return t
        return None

    def _build_target_label(self, task_type: str, params: dict) -> str:
        """Compute a human-readable source → dest label matching the task details page."""
        if task_type == "scheduled_backup":
            job = params.get("job_name") or params.get("parent_job") or ""
            pool = params.get("backup_pool") or ""
            return f"{job} → {pool}" if job else pool or "—"
        if task_type.startswith("bulk_"):
            return params.get("label") or f"{params.get('total_items', 0)} item(s)"
        if task_type in ("delete", "create"):
            pool = params.get("pool") or params.get("source_pool") or ""
            vol = params.get("volume_name") or params.get("source_volume") or "—"
            return f"{pool}/{vol}" if pool else vol
        if task_type == "rename":
            pool = params.get("pool") or ""
            vol = params.get("volume_name") or "—"
            new = params.get("new_name") or "?"
            return f"{pool}/{vol} → {new}" if pool else f"{vol} → {new}"
        if task_type == "backup":
            src_pool = params.get("source_pool") or ""
            src_vol = params.get("source_volume") or "—"
            dst = params.get("backup_pool") or "backup pool"
            src = f"{src_pool}/{src_vol}" if src_pool else src_vol
            return f"{src} → {dst}"
        if task_type == "migrate":
            src = f"{params.get('source_pool', '?')}/{params.get('source_volume', '?')}"
            dst_pool = params.get("dest_pool") or "?"
            dst_vol = params.get("dest_volume") or params.get("dest_volume_name") or ""
            return f"{src} → {dst_pool}/{dst_vol}" if dst_vol else f"{src} → {dst_pool}"
        if task_type == "restore":
            file_ = params.get("backup_file") or params.get("source_volume") or "—"
            dst_pool = params.get("dest_pool") or ""
            dst_vol = params.get("dest_volume_name") or params.get("dest_volume") or ""
            dst = f"{dst_pool}/{dst_vol}" if dst_pool else dst_vol or "destination"
            return f"{file_} → {dst}"
        vol = params.get("source_volume") or params.get("volume_name") or params.get("backup_file") or "—"
        pool = params.get("source_pool") or params.get("pool") or ""
        return f"{pool}/{vol}" if pool else vol

    def _build_params_block(self, params: dict) -> str:
        """Render all task params as a formatted list, omitting internal fields."""
        lines = []
        seen_labels: set = set()
        for k, v in params.items():
            if k in _HIDDEN_PARAMS:
                continue
            if v is None or v == "" or v == [] or v == {}:
                continue
            label = _PARAM_LABELS.get(k, k.replace("_", " ").title())
            # Deduplicate when two keys map to the same label (e.g. job_name + parent_job)
            if label in seen_labels:
                continue
            seen_labels.add(label)
            if isinstance(v, bool):
                val = "Yes" if v else "No"
            elif isinstance(v, list):
                val = ", ".join(str(x) for x in v)
            elif isinstance(v, dict):
                val = json.dumps(v)
            else:
                val = str(v)
            lines.append(f"  {label}: `{val}`")
        if not lines:
            return ""
        return "\n\n\U0001f4cb *Parameters*\n" + "\n".join(lines)

    def _build_message(self, cfg: dict, task: dict) -> str:
        """Build a notification message directly from the task dict."""
        params = task.get("params") or {}
        task_type = (task.get("type") or task.get("task_type") or "task").lower()
        task_type_label = _TASK_TYPE_LABELS.get(task_type, task_type.replace("_", " ").title())
        status = task.get("status", "unknown")
        status_emoji = "✅" if status == "completed" else "❌"

        elapsed = task.get("elapsed_seconds", 0)
        elapsed_str = format_duration(elapsed) if isinstance(elapsed, (int, float)) else str(elapsed)

        completed_ts = task.get("completed_at") or time.time()
        timestamp = datetime.datetime.fromtimestamp(completed_ts).strftime("%d/%m/%Y %H:%M:%S")
        started_ts = task.get("started_at")
        started_at = datetime.datetime.fromtimestamp(started_ts).strftime("%d/%m/%Y %H:%M:%S") if started_ts else "—"

        error = task.get("error") or ""
        error_block = f"\n❗ Error: {error}" if error else ""

        # Legacy convenience aliases kept so custom templates using them still work
        volume = (
            params.get("source_volume")
            or params.get("volume_name")
            or params.get("dest_volume")
            or params.get("job_name")
            or "—"
        )
        pool = (
            params.get("source_pool")
            or params.get("pool")
            or params.get("dest_pool")
            or "—"
        )

        return self._render_template(
            cfg,
            task_id=task.get("task_id") or "—",
            task_type=task_type,
            task_type_label=task_type_label,
            status=status,
            status_emoji=status_emoji,
            target=self._build_target_label(task_type, params),
            elapsed=elapsed_str,
            started_at=started_at,
            timestamp=timestamp,
            current_operation=task.get("current_operation") or "",
            error=error,
            error_block=error_block,
            params_block=self._build_params_block(params),
            # legacy single-field aliases
            volume=volume,
            pool=pool,
            source_volume=params.get("source_volume") or params.get("volume_name") or "—",
            source_pool=params.get("source_pool") or params.get("pool") or "—",
            dest_volume=params.get("dest_volume") or params.get("dest_volume_name") or "—",
            dest_pool=params.get("dest_pool") or "—",
            backup_pool=params.get("backup_pool") or "—",
            backup_file=params.get("backup_file") or "—",
            job_name=params.get("job_name") or params.get("parent_job") or "—",
            hostname=socket.gethostname(),
        )

    def _render_template(self, cfg: dict, **kwargs: Any) -> str:
        template = (cfg.get("message_template") or "").strip() or DEFAULT_TEMPLATE
        # Safe substitution: replace only known {field} tokens from the allowlist.
        # Unknown tokens are left literal. This avoids Python str.format() attribute/
        # index traversal (e.g. {hostname.__class__...}) on user-supplied templates.
        def _sub(match: "re.Match") -> str:
            key = match.group(1)
            return str(kwargs[key]) if key in kwargs else match.group(0)
        return _PLACEHOLDER_RE.sub(_sub, template)

    def _send(self, cfg: dict, text: str) -> bool:
        try:
            server_url = (cfg.get("server_url") or "https://api.telegram.org").rstrip("/")
            token = cfg.get("token", "")
            chat_id = cfg.get("chat_id", "")
            url = f"{server_url}/bot{token}/sendMessage"
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            thread_id = cfg.get("message_thread_id")
            if thread_id:
                try:
                    payload["message_thread_id"] = int(thread_id)
                except (TypeError, ValueError):
                    pass
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    print(f"[NOTIFY] Telegram rejected message: {result.get('description', result)}", flush=True)
                    return False
                return True
        except Exception as e:
            print(f"[NOTIFY] Failed to send Telegram message: {e}", flush=True)
            return False

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self):
        try:
            if self._file.exists():
                raw = json.loads(self._file.read_text())
                if isinstance(raw, dict):
                    self._configs = raw
        except Exception as e:
            print(f"[WARNING] Could not load notifications config: {e}", flush=True)

    def _save(self):
        try:
            self._file.write_text(json.dumps(self._configs, indent=2))
        except Exception as e:
            print(f"[WARNING] Could not save notifications config: {e}", flush=True)


# Singleton ───────────────────────────────────────────────────────────────────

_notification_service: Optional[NotificationService] = None


def get_notification_service(config_dir: Optional[str] = None) -> Optional[NotificationService]:
    """Get (or initialize) the global notification service singleton."""
    global _notification_service
    if _notification_service is None:
        if config_dir is None:
            return None
        _notification_service = NotificationService(config_dir)
    return _notification_service
