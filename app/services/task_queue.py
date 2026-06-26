"""Task queue and progress tracking."""

import re
import sys
import uuid
import json
import time
import queue
from typing import Dict, Any, Optional, Callable, List
from threading import Thread, Lock
from pathlib import Path

MAX_TASK_LOG_LINES = 300

_TASK_LOG_RE = re.compile(r'^\[TASK:([a-f0-9-]+)\]\s*(.*)', re.IGNORECASE)


class _TaskLogCapture:
    """Wraps sys.stdout to route [TASK:id] prefixed lines into the in-memory log buffer."""

    def __init__(self, original, queue: "TaskQueue"):
        self._orig = original
        self._queue = queue

    def write(self, text: str) -> int:
        n = self._orig.write(text)
        current_task_id = None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _TASK_LOG_RE.match(stripped)
            if m:
                current_task_id = m.group(1)
                self._queue.log_task(m.group(1), m.group(2))
            elif current_task_id:
                # Continuation line within the same write() — attribute to the same task
                self._queue.log_task(current_task_id, stripped)
        return n or 0

    def flush(self):           self._orig.flush()
    def isatty(self):          return getattr(self._orig, "isatty", lambda: False)()
    def fileno(self):          return self._orig.fileno()
    def __getattr__(self, n):  return getattr(self._orig, n)


class TaskQueue:
    """Sequential task queue with progress tracking and locking."""

    def __init__(self, tmp_dir: str = "/tmp", config_dir: str = "/config"):
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.lock = Lock()
        self.log_lock = Lock()
        self.log_lines: Dict[str, List[str]] = {}
        self.running_task_id: Optional[str] = None
        self.locks_dir = Path(tmp_dir) / "locks"
        self.locks_dir.mkdir(exist_ok=True, parents=True)
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        self.tasks_file = Path(config_dir) / "vshipper_tasks.json"
        self._load_tasks()

        # Single worker thread drains this FIFO so tasks run strictly one at a
        # time. Endpoints/scheduler enqueue runnables via submit(); a task
        # triggered while another runs waits its turn as "pending".
        self._work_queue: "queue.Queue" = queue.Queue()
        self._worker = Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def submit(self, task_id: str, runnable: Callable[[], Any]) -> None:
        """Enqueue an already-created task's runnable for sequential execution."""
        self._work_queue.put((task_id, runnable))

    def _worker_loop(self) -> None:
        """Run queued tasks one at a time, in the order they were submitted."""
        while True:
            task_id, runnable = self._work_queue.get()
            try:
                task = self.tasks.get(task_id)
                # Skip tasks finalized or removed before their turn (e.g. marked
                # failed by restart recovery, or a future cancel).
                if not task or task.get("status") != "pending":
                    continue
                self.start_task(task_id)
                runnable()
            except Exception as e:
                print(f"[TASK:{task_id}] Unhandled error in task: {e}", flush=True)
                task = self.tasks.get(task_id)
                if task and task.get("status") not in ("completed", "failed"):
                    self.complete_task(task_id, success=False, error=str(e))
            finally:
                self._work_queue.task_done()

    def add_task(self, task_type: str, **kwargs) -> str:
        """Add a new task to the queue."""
        task_id = str(uuid.uuid4())
        
        with self.lock:
            self.tasks[task_id] = {
                "task_id": task_id,
                "type": task_type,
                "status": "pending",
                "progress_percent": 0,
                "current_operation": None,
                "elapsed_seconds": 0,
                "estimated_remaining_seconds": None,
                "error": None,
                "created_at": time.time(),
                "started_at": None,
                "completed_at": None,
                "params": kwargs
            }
            self._save_tasks()
        
        print(f"[TASK:{task_id}] Created task type={task_type}", flush=True)
        return task_id
    
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task by ID."""
        return self.tasks.get(task_id)
    
    def update_progress(self, task_id: str, updates: Dict[str, Any]):
        """Update task progress."""
        if task_id not in self.tasks:
            return
        
        with self.lock:
            task = self.tasks[task_id]
            
            # Update task fields
            for key, value in updates.items():
                if key in task:
                    task[key] = value
            
            # Update elapsed time
            if task.get("started_at"):
                task["elapsed_seconds"] = round(time.time() - task["started_at"], 1)
            self._save_tasks()
    
    def start_task(self, task_id: str):
        """Mark task as running.

        Idempotent: a task is started once by the queue worker, so a later
        start_task() from the service running it is a no-op and doesn't log a
        second "Started" line (the task was already logged "Created" on enqueue).
        """
        if task_id not in self.tasks:
            return

        with self.lock:
            task = self.tasks[task_id]
            if task["status"] == "running":
                return  # already started — don't re-stamp or double-log
            task["status"] = "running"
            task["started_at"] = time.time()
            self.running_task_id = task_id

        print(f"[TASK:{task_id}] Started", flush=True)
    
    def complete_task(self, task_id: str, success: bool = True, error: Optional[str] = None):
        """Mark task as completed.

        Idempotent: first finalize wins. A service that finalizes its own task
        (backup/migrate/restore) keeps its detailed error/success even when a
        caller (e.g. bulk_service) also calls complete_task afterwards.
        """
        if task_id not in self.tasks:
            return

        with self.lock:
            task = self.tasks[task_id]
            if task["status"] in ("completed", "failed"):
                return  # already finalized — don't re-stamp or fire a second notification
            task["status"] = "completed" if success else "failed"
            task["completed_at"] = time.time()

            if error:
                task["error"] = error
                print(f"[TASK:{task_id}] Failed: {error}", flush=True)
            else:
                task["progress_percent"] = 100
                print(f"[TASK:{task_id}] Completed successfully", flush=True)

            if self.running_task_id == task_id:
                self.running_task_id = None
            self._save_tasks()
            task_snapshot = dict(task)

        Thread(target=self._fire_notification, args=[task_snapshot], daemon=True).start()

    def _fire_notification(self, task: dict):
        try:
            from app.services.notification_service import get_notification_service
            svc = get_notification_service()
            if svc:
                svc.notify_task_completion(task)
        except Exception as e:
            print(f"[TASK:{task.get('task_id')}] [WARNING] Notification error: {e}", flush=True)

    def create_lockfile(self, pool: str, volume: str) -> str:
        """Create exclusive lock for volume operation."""
        lock_file = self.locks_dir / f"{pool}_{volume}.lock"
        lock_file.write_text(str(uuid.uuid4()))
        return str(lock_file)
    
    def remove_lockfile(self, lock_file: str):
        """Remove lock file."""
        try:
            Path(lock_file).unlink()
        except Exception as e:
            print(f"[WARNING] Failed to remove lock file {lock_file}: {e}", flush=True)
    
    def is_volume_locked(self, pool: str, volume: str) -> bool:
        """Check if volume is locked."""
        lock_file = self.locks_dir / f"{pool}_{volume}.lock"
        return lock_file.exists()
    
    def get_lock_file(self, pool: str, volume: str) -> str:
        """Get lock file path for volume."""
        return str(self.locks_dir / f"{pool}_{volume}.lock")

    def log_task(self, task_id: str, message: str):
        """Append a log line to a task's in-memory buffer."""
        with self.log_lock:
            buf = self.log_lines.setdefault(task_id, [])
            buf.append(message)
            if len(buf) > MAX_TASK_LOG_LINES:
                del buf[0]

    def get_task_logs(self, task_id: str) -> List[str]:
        """Return captured log lines for a task."""
        with self.log_lock:
            return list(self.log_lines.get(task_id, []))

    def _load_tasks(self):
        """Load persisted tasks from disk."""
        try:
            if self.tasks_file.exists():
                raw = json.loads(self.tasks_file.read_text())
                if isinstance(raw, dict):
                    self.tasks = raw
        except Exception as e:
            print(f"[WARNING] Unable to load persisted tasks: {e}", flush=True)
        
        for task in self.tasks.values():
            if task.get("status") in {"running", "pending"}:
                task["status"] = "failed"
                task["error"] = "Server restarted while task was in progress."
                task["completed_at"] = time.time()
        self._save_tasks()

    def _save_tasks(self):
        """Persist tasks to disk."""
        try:
            self.tasks_file.write_text(json.dumps(self.tasks, indent=2))
        except Exception as e:
            print(f"[WARNING] Unable to save tasks: {e}", flush=True)


# Global task queue instance — lazily initialized so tmp_dir can be configured
_task_queue: Optional[TaskQueue] = None


def get_task_queue(tmp_dir: str = "/tmp", config_dir: str = "/config") -> TaskQueue:
    """Get (or initialize) the global task queue. Pass dirs only on the first call."""
    global _task_queue
    if _task_queue is None:
        _task_queue = TaskQueue(tmp_dir=tmp_dir, config_dir=config_dir)
        # Install stdout interceptor once so all [TASK:id] print()s flow into the log buffer
        if not isinstance(sys.stdout, _TaskLogCapture):
            sys.stdout = _TaskLogCapture(sys.stdout, _task_queue)
    return _task_queue
