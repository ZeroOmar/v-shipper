"""Bulk action service.

Runs a set of single-volume/single-backup operations sequentially under one
"summary" task, mirroring the scheduled-backup pattern in scheduler_service.py:
the summary task is what the queue worker runs; inside it each item gets its own
sub-task (linked via parent_task_id) and the real per-item service method is
called directly, one at a time. The UI groups the sub-tasks under the summary's
detail view, exactly like a scheduled run.
"""

from typing import Callable, List, Optional, Tuple, TypedDict

from app.services.task_queue import get_task_queue


class BulkOperation(TypedDict, total=False):
    item: str                                   # display label (volume or backup file)
    lock: Optional[Tuple[str, str]]             # (pool, volume) to honour cooperative locks
    sub_type: str                               # task type for the per-item sub-task
    sub_params: dict                            # add_task params for the sub-task
    precheck: Optional[Callable[[], Optional[str]]]  # returns a skip-reason, or None to proceed
    run: Callable[[str], bool]                  # run(sub_task_id) -> success


class BulkService:
    """Orchestrates sequential bulk operations behind a single summary task."""

    def __init__(self):
        self.task_queue = get_task_queue()

    def enqueue(self, action: str, label: str, operations: List[BulkOperation]) -> str:
        """Create the summary task and hand the whole batch to the task queue.

        The batch waits its turn behind any running task and then runs its items
        one at a time. Returns the summary task id.
        """
        summary_task_id = self.task_queue.add_task(
            f"bulk_{action}",
            action=action,
            label=label,
            total_items=len(operations),
            items=[op["item"] for op in operations],
        )
        self.task_queue.submit(summary_task_id, lambda: self._run(summary_task_id, label, operations))
        return summary_task_id

    def _run(self, summary_task_id: str, label: str, operations: List[BulkOperation]) -> None:
        n = len(operations)
        print(f"[TASK:{summary_task_id}] Starting bulk action '{label}' — {n} item(s)", flush=True)
        results = {}

        for i, op in enumerate(operations):
            item = op["item"]
            self.task_queue.update_progress(summary_task_id, {
                "progress_percent": int(i * 100 / n) if n else 100,
                "current_operation": f"{i + 1}/{n}: {item}",
            })

            lock = op.get("lock")
            if lock and self.task_queue.is_volume_locked(lock[0], lock[1]):
                print(f"[TASK:{summary_task_id}] ⏭ Skipped {item}: locked by another operation", flush=True)
                results[item] = "skipped"
                continue

            precheck = op.get("precheck")
            if precheck is not None:
                reason = precheck()
                if reason:
                    print(f"[TASK:{summary_task_id}] ⏭ Skipped {item}: {reason}", flush=True)
                    results[item] = "skipped"
                    continue

            sub_task_id = self.task_queue.add_task(
                op["sub_type"], bulk=True, parent_task_id=summary_task_id, **op["sub_params"]
            )
            try:
                ok = bool(op["run"](sub_task_id))
            except Exception as e:
                print(f"[TASK:{summary_task_id}] ✗ {item}: {e}", flush=True)
                ok = False

            if ok:
                results[item] = "ok"
            else:
                results[item] = "failed"
                sub = self.task_queue.get_task(sub_task_id)
                reason = (sub or {}).get("error") or "operation returned failure"
                print(f"[TASK:{summary_task_id}] ✗ {item}: {reason}", flush=True)

            self.task_queue.update_progress(summary_task_id, {
                "progress_percent": min(99, int((i + 1) * 100 / n)) if n else 100,
            })

        succeeded = sum(1 for v in results.values() if v == "ok")
        skipped = sum(1 for v in results.values() if v == "skipped")
        failed = n - succeeded - skipped

        print(f"[TASK:{summary_task_id}] ── Summary ──────────────────────", flush=True)
        for item, result in results.items():
            icon = "✓" if result == "ok" else ("⏭" if result == "skipped" else "✗")
            print(f"[TASK:{summary_task_id}] {icon} {item}: {result}", flush=True)
        print(f"[TASK:{summary_task_id}] {succeeded} succeeded · {skipped} skipped · {failed} failed", flush=True)

        error = None if failed == 0 else f"{failed} of {n} item(s) failed"
        self.task_queue.complete_task(summary_task_id, success=(failed == 0), error=error)


# Global bulk service instance
_bulk_service = None


def get_bulk_service() -> BulkService:
    """Get (or initialize) the global bulk service."""
    global _bulk_service
    if _bulk_service is None:
        _bulk_service = BulkService()
    return _bulk_service
