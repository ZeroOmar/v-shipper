"""Backup scheduling service — APScheduler-backed cron jobs with retention."""

import json
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_config


class SchedulerService:
    def __init__(self, tmp_dir: str, config_dir: str, backup_service, task_queue):
        self._scheduler = BackgroundScheduler(timezone='UTC')
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        self._jobs_file = Path(config_dir) / 'vshipper_schedules.json'
        self.jobs: Dict[str, dict] = {}
        self._backup_service = backup_service
        self._task_queue = task_queue
        self._load()
        self._scheduler.start()
        self._reschedule_all()
        print("[SCHEDULER] Scheduler service started", flush=True)

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._jobs_file.exists():
                self.jobs = json.loads(self._jobs_file.read_text())
                print(f"[SCHEDULER] Loaded {len(self.jobs)} scheduled job(s)", flush=True)
        except Exception as e:
            print(f"[SCHEDULER] Failed to load schedules: {e}", flush=True)
            self.jobs = {}

    def _save(self) -> None:
        try:
            self._jobs_file.write_text(json.dumps(self.jobs, indent=2))
        except Exception as e:
            print(f"[SCHEDULER] Failed to save schedules: {e}", flush=True)

    # ── APScheduler helpers ───────────────────────────────────────────────────

    def _schedule_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job or not job.get('enabled', True):
            return
        try:
            self._scheduler.add_job(
                self._run_backup_job,
                CronTrigger.from_crontab(job['cron'], timezone='UTC'),
                id=job_id,
                replace_existing=True,
                args=[job_id],
            )
        except Exception as e:
            print(f"[SCHEDULER] Failed to schedule job {job_id}: {e}", flush=True)

    def _unschedule_job(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

    def _reschedule_all(self) -> None:
        for job_id, job in self.jobs.items():
            if job.get('enabled', True):
                self._schedule_job(job_id)

    def get_next_run(self, job_id: str) -> Optional[float]:
        try:
            apj = self._scheduler.get_job(job_id)
            if apj and apj.next_run_time:
                return apj.next_run_time.timestamp()
        except Exception:
            pass
        return None

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def list_jobs(self) -> List[dict]:
        result = []
        for job_id, job in self.jobs.items():
            enriched = dict(job)
            enriched['next_run'] = self.get_next_run(job_id)
            result.append(enriched)
        return result

    def get_job(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        enriched = dict(job)
        enriched['next_run'] = self.get_next_run(job_id)
        return enriched

    def create_job(self, data: dict) -> dict:
        job_id = str(uuid.uuid4())
        job = {
            'id': job_id,
            'name': data['name'],
            'cron': data['cron'],
            'backup_pool': data['backup_pool'],
            'volumes': data['volumes'],
            'retention': data.get('retention', 7),
            'enabled': True,
        }
        self.jobs[job_id] = job
        self._save()
        self._schedule_job(job_id)
        print(f"[SCHEDULER] Created job '{job['name']}' ({job_id}) cron={job['cron']}", flush=True)
        return self.get_job(job_id)

    def update_job(self, job_id: str, data: dict) -> Optional[dict]:
        if job_id not in self.jobs:
            return None
        job = self.jobs[job_id]
        job.update({
            'name': data['name'],
            'cron': data['cron'],
            'backup_pool': data['backup_pool'],
            'volumes': data['volumes'],
            'retention': data.get('retention', 7),
        })
        self._save()
        self._unschedule_job(job_id)
        if job.get('enabled', True):
            self._schedule_job(job_id)
        print(f"[SCHEDULER] Updated job '{job['name']}' ({job_id})", flush=True)
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        if job_id not in self.jobs:
            return False
        name = self.jobs[job_id].get('name', job_id)
        self._unschedule_job(job_id)
        del self.jobs[job_id]
        self._save()
        print(f"[SCHEDULER] Deleted job '{name}' ({job_id})", flush=True)
        return True

    def toggle_job(self, job_id: str) -> Optional[dict]:
        if job_id not in self.jobs:
            return None
        job = self.jobs[job_id]
        job['enabled'] = not job.get('enabled', True)
        self._save()
        if job['enabled']:
            self._schedule_job(job_id)
        else:
            self._unschedule_job(job_id)
        print(f"[SCHEDULER] Job '{job['name']}' {'enabled' if job['enabled'] else 'disabled'}", flush=True)
        return self.get_job(job_id)

    def trigger_now(self, job_id: str) -> None:
        if job_id not in self.jobs:
            return
        t = threading.Thread(target=self._run_backup_job, args=[job_id], daemon=True)
        t.start()

    # ── Execution ─────────────────────────────────────────────────────────────

    def _run_backup_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return

        volumes = job['volumes']
        summary_task_id = self._task_queue.add_task(
            'scheduled_backup',
            job_id=job_id,
            job_name=job['name'],
            total_volumes=len(volumes),
            backup_pool=job['backup_pool'],
        )
        self._task_queue.start_task(summary_task_id)
        print(f"[TASK:{summary_task_id}] Starting scheduled backup '{job['name']}' — {len(volumes)} volume(s)", flush=True)

        n = len(volumes)
        results = {}
        for i, vol in enumerate(volumes):
            pool_name = vol['pool']
            vol_name = vol['volume']
            self._task_queue.update_progress(summary_task_id, {
                'progress_percent': int(i * 100 / n),
                'current_operation': f"{i + 1}/{n} volumes",
            })

            if self._task_queue.is_volume_locked(pool_name, vol_name):
                print(f"[TASK:{summary_task_id}] ⏭ Skipped {pool_name}/{vol_name}: locked by another operation", flush=True)
                results[f"{pool_name}/{vol_name}"] = 'skipped'
            else:
                sub_task_id = self._task_queue.add_task(
                    'backup',
                    source_pool=pool_name,
                    source_volume=vol_name,
                    backup_pool=job['backup_pool'],
                    scheduled=True,
                    parent_job=job['name'],
                )
                try:
                    self._backup_service.backup_volume(sub_task_id, pool_name, vol_name, job['backup_pool'])
                    results[f"{pool_name}/{vol_name}"] = 'ok'
                    self._apply_retention(job, vol, summary_task_id)
                except Exception as e:
                    print(f"[TASK:{summary_task_id}] ✗ Failed {pool_name}/{vol_name}: {e}", flush=True)
                    results[f"{pool_name}/{vol_name}"] = 'failed'

            self._task_queue.update_progress(summary_task_id, {
                'progress_percent': min(99, int((i + 1) * 100 / n)),
            })

        succeeded = sum(1 for v in results.values() if v == 'ok')
        skipped = sum(1 for v in results.values() if v == 'skipped')
        failed = n - succeeded - skipped

        print(f"[TASK:{summary_task_id}] ── Summary ──────────────────────", flush=True)
        for vol_key, result in results.items():
            icon = '✓' if result == 'ok' else ('⏭' if result == 'skipped' else '✗')
            print(f"[TASK:{summary_task_id}] {icon} {vol_key}: {result}", flush=True)
        print(f"[TASK:{summary_task_id}] {succeeded} succeeded · {skipped} skipped · {failed} failed", flush=True)

        error = f"{failed} volume(s) failed" if failed else None
        self._task_queue.complete_task(summary_task_id, success=(failed == 0), error=error)

    def _apply_retention(self, job: dict, vol: dict, task_id: Optional[str] = None) -> None:
        def log(msg: str) -> None:
            print(f"[RETENTION] {msg}", flush=True)
            if task_id:
                print(f"[TASK:{task_id}] {msg}", flush=True)

        try:
            config = get_config()
            bp = next((p for p in config.backup_pools if p.name == job['backup_pool']), None)
            if not bp:
                return

            pattern = f"{vol['pool']}_{vol['volume']}_*.tar.gz"

            if bp.pool_type == 'remote':
                self._apply_retention_remote(bp, pattern, job['retention'], log)
            else:
                pool_path = Path(bp.pool)
                archives = sorted(pool_path.glob(pattern))
                to_delete = archives[:max(0, len(archives) - job['retention'])]
                for f in to_delete:
                    f.unlink(missing_ok=True)
                    log(f"Deleted old archive (retention={job['retention']}): {f.name}")
        except Exception as e:
            log(f"Error applying retention: {e}")

    def _apply_retention_remote(self, bp, pattern: str, retention: int, log) -> None:
        """Delete oldest archives beyond retention count from a remote rsync daemon pool."""
        remote_host = bp.remote_host
        rsync_module = bp.rsync_module
        if not remote_host or not rsync_module:
            log(f"Remote pool '{bp.name}' missing remote_host or rsync_module, skipping retention")
            return

        remote_base = f"rsync://{remote_host}/{rsync_module}/"
        try:
            result = subprocess.run(
                ["rsync", "--list-only", remote_base],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log(f"Could not list remote pool '{bp.name}': {result.stderr.strip()}")
                return

            import fnmatch
            archives = sorted(
                line.split()[-1]
                for line in result.stdout.splitlines()
                if line and not line.startswith('d') and fnmatch.fnmatch(line.split()[-1], pattern)
            )
            to_delete = archives[:max(0, len(archives) - retention)]
            if not to_delete:
                return

            with tempfile.TemporaryDirectory() as empty_dir:
                for archive_name in to_delete:
                    cmd = [
                        "rsync", "-az", "--delete",
                        f"--filter=+ {archive_name}",
                        "--filter=- *",
                        f"{empty_dir}/",
                        remote_base,
                    ]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        log(f"Deleted old archive (retention={retention}): {archive_name}")
                    else:
                        log(f"Failed to delete {archive_name}: {r.stderr.strip()}")
        except subprocess.TimeoutExpired:
            log(f"Timeout while applying retention to remote pool '{bp.name}'")
        except Exception as e:
            log(f"Error applying remote retention: {e}")

    def shutdown(self) -> None:
        try:
            self._scheduler.shutdown(wait=False)
            print("[SCHEDULER] Scheduler stopped", flush=True)
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_scheduler_service: Optional[SchedulerService] = None


def get_scheduler_service(config=None, backup_service=None, task_queue=None) -> SchedulerService:
    global _scheduler_service
    if _scheduler_service is None:
        if config is None or backup_service is None or task_queue is None:
            raise RuntimeError("SchedulerService not yet initialized")
        _scheduler_service = SchedulerService(
            config.tmp_dir, config.config_dir, backup_service, task_queue
        )
    return _scheduler_service
