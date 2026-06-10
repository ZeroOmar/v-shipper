"""Task queue and progress tracking."""

import uuid
import json
import time
from typing import Dict, Any, Optional, Callable
from threading import Thread, Lock
from pathlib import Path


class TaskQueue:
    """Sequential task queue with progress tracking and locking."""

    def __init__(self, tmp_dir: str = "/tmp"):
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.lock = Lock()
        self.running_task_id: Optional[str] = None
        self.locks_dir = Path(tmp_dir) / "locks"
        self.locks_dir.mkdir(exist_ok=True, parents=True)
        self.tasks_file = Path(tmp_dir) / "vshipper_tasks.json"
        self._load_tasks()
    
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
                task["elapsed_seconds"] = int(time.time() - task["started_at"])
            self._save_tasks()
    
    def start_task(self, task_id: str):
        """Mark task as running."""
        if task_id not in self.tasks:
            return
        
        with self.lock:
            task = self.tasks[task_id]
            task["status"] = "running"
            task["started_at"] = time.time()
            self.running_task_id = task_id
        
        print(f"[TASK:{task_id}] Started", flush=True)
    
    def complete_task(self, task_id: str, success: bool = True, error: Optional[str] = None):
        """Mark task as completed."""
        if task_id not in self.tasks:
            return
        
        with self.lock:
            task = self.tasks[task_id]
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


def get_task_queue(tmp_dir: str = "/tmp") -> TaskQueue:
    """Get (or initialize) the global task queue. Pass tmp_dir only on the first call."""
    global _task_queue
    if _task_queue is None:
        _task_queue = TaskQueue(tmp_dir=tmp_dir)
    return _task_queue
