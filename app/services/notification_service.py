"""Notification service — Telegram alerts on task completion."""

import datetime
import json
import socket
import time
import urllib.request
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional


DEFAULT_TEMPLATE = (
    "\U0001f514 *{task_type_label}* {status_emoji}\n"
    "\U0001f4e6 Volume: `{volume}` on `{pool}`\n"
    "Status: {status}\n"
    "⏱ Duration: {elapsed}s\n"
    "\U0001f552 {timestamp}"
    "{error_block}"
)

_TASK_TYPE_LABELS: Dict[str, str] = {
    "backup": "Backup",
    "scheduled_backup": "Scheduled Backup",
    "migrate": "Migration",
    "restore": "Restore",
    "delete": "Delete",
    "rename": "Rename",
}


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
        """Route a completed task to matching notification configs."""
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
            self._send(cfg, self._build_task_message(cfg, task))

    def notify_operation(self, topic: str, context: dict):
        """Send a notification for a non-task-based operation (e.g. rename)."""
        with self._lock:
            configs = list(self._configs.values())
        for cfg in configs:
            if not cfg.get("enabled"):
                continue
            if topic not in (cfg.get("topics") or []):
                continue
            if cfg.get("on_failure_only") and not context.get("failed"):
                continue
            self._send(cfg, self._build_op_message(cfg, topic, context))

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _task_to_topic(self, task: dict) -> Optional[str]:
        t = (task.get("type") or task.get("task_type") or "").lower()
        if t == "backup":
            # Suppress scheduled sub-tasks — the summary task handles those
            if task.get("params", {}).get("scheduled"):
                return None
            return "backup"
        if t == "scheduled_backup":
            return "schedule"
        if t in ("migrate", "restore", "delete"):
            return t
        return None

    def _build_task_message(self, cfg: dict, task: dict) -> str:
        params = task.get("params") or {}
        status = task.get("status", "unknown")
        status_emoji = "✅" if status == "completed" else "❌"
        elapsed = task.get("elapsed_seconds", 0)
        elapsed_str = f"{elapsed:.1f}" if isinstance(elapsed, (int, float)) else str(elapsed)
        ts = task.get("completed_at") or time.time()
        timestamp = datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
        error = task.get("error") or ""
        error_block = f"\n❗ Error: {error}" if error else ""
        task_type = (task.get("type") or task.get("task_type") or "task").lower()
        task_type_label = _TASK_TYPE_LABELS.get(task_type, task_type.replace("_", " ").title())
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
            task_type=task_type,
            task_type_label=task_type_label,
            status=status,
            status_emoji=status_emoji,
            volume=volume,
            pool=pool,
            elapsed=elapsed_str,
            timestamp=timestamp,
            error=error,
            error_block=error_block,
            job_name=params.get("job_name") or params.get("parent_job") or "—",
            hostname=socket.gethostname(),
        )

    def _build_op_message(self, cfg: dict, topic: str, context: dict) -> str:
        status = "failed" if context.get("failed") else "completed"
        status_emoji = "✅" if status == "completed" else "❌"
        timestamp = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        error = context.get("error") or ""
        error_block = f"\n❗ Error: {error}" if error else ""
        label = _TASK_TYPE_LABELS.get(topic, topic.replace("_", " ").title())
        return self._render_template(
            cfg,
            task_type=topic,
            task_type_label=label,
            status=status,
            status_emoji=status_emoji,
            volume=context.get("volume") or "—",
            pool=context.get("pool") or "—",
            elapsed="—",
            timestamp=timestamp,
            error=error,
            error_block=error_block,
            job_name="—",
            hostname=socket.gethostname(),
        )

    def _render_template(self, cfg: dict, **kwargs: Any) -> str:
        template = (cfg.get("message_template") or "").strip() or DEFAULT_TEMPLATE
        try:
            return template.format(**kwargs)
        except KeyError as e:
            return f"[v-shipper] {kwargs.get('task_type_label', 'Task')} {kwargs.get('status', '')} — template variable {e} not found"

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
