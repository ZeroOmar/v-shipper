"""API routes for v-shipper."""

import base64
import threading
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from fastapi.responses import JSONResponse
from app.models import (
    LoginRequest, PoolsListResponse, VolumesListResponse, VolumeDetailResponse,
    MigrateRequest, BackupRequest, RenameRequest, DeleteRequest, RestoreRequest, PoolCreateRequest,
    TaskResponse, TaskProgressResponse, TasksListResponse, HealthResponse, PoolStats, VolumeInfo,
    BackupSchedule, BackupScheduleCreate, SchedulesResponse, VolumeCreateRequest,
)
from app.services.volume_service import get_volume_service
from app.services.migration_service import get_migration_service
from app.services.backup_service import get_backup_service
from app.services.task_queue import get_task_queue
from app.services.scheduler_service import get_scheduler_service
from app.config import validate_auth, get_config

router = APIRouter()


def _volume_exists_in_pool(volume_service, pool: dict, volume_name: str) -> bool:
    """Return True if volume_name already exists in the given pool."""
    try:
        if pool.get('pool_type') == 'remote':
            target = volume_service._build_rsync_target(pool, volume_name, trailing_slash=True)
            ok, out, _ = volume_service._run_rsync_list(target)
            return ok and bool(out.strip())
        return Path(pool['path']).joinpath(volume_name).exists()
    except Exception:
        return False


# Session storage (simple in-memory, per-session)
sessions = {}


def get_session(request: Request) -> dict:
    """Get session from request.

    Supports session id in cookie, query parameter `session_id`, or
    `Authorization: Bearer <session_id>` header for flexibility in clients.
    """
    # 1) Cookie
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        return sessions.get(session_id, {})

    # 2) Query parameter
    session_id = request.query_params.get("session_id")
    if session_id and session_id in sessions:
        return sessions.get(session_id, {})

    # 3) Authorization header (Bearer)
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        token = auth.split(None, 1)[1].strip()
        if token and token in sessions:
            return sessions.get(token, {})

    return {}


def require_auth(session: dict = Depends(get_session)) -> dict:
    """Require authenticated session."""
    if not session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


@router.post("/api/login")
async def login(request: LoginRequest, response: Response):
    """Login endpoint."""
    try:
        if validate_auth(request.username, request.password):
            session_id = str(uuid.uuid4())
            sessions[session_id] = {"authenticated": True, "user": request.username, "session_id": session_id}
            
            response_data = {"status": "ok", "message": "Login successful"}
            # Set session cookie so browsers will send it automatically
            response.set_cookie("session_id", session_id, httponly=True, samesite='Lax')
            return {"status": "ok", "message": "Login successful", "session_id": session_id}
        else:
            raise HTTPException(status_code=401, detail="Invalid credentials")
    except Exception as e:
        print(f"[ERROR] Login error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/logout")
async def logout(session: dict = Depends(get_session)):
    """Logout endpoint."""
    session_id = session.get("session_id")
    if session_id and session_id in sessions:
        del sessions[session_id]
    return {"status": "ok"}


@router.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return HealthResponse()


@router.get("/api/pools", response_model=PoolsListResponse)
async def list_pools(session: dict = Depends(require_auth)):
    """List all pools with disk usage stats."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        
        pools_stats = []
        
        # Add docker host pools
        for host in config.docker_hosts:
            pool_info = {
                "name": host.name,
                "path": host.pool,
                "type": host.pool_type,
                "pool_type": host.pool_type,
                "remote_host": host.remote_host,
                "rsync_module": host.rsync_module,
                "role": "docker"
            }
            stats = volume_service.get_pool_stats(pool_info)
            pools_stats.append(stats)

        # Add backup pools
        for backup in config.backup_pools:
            pool_info = {
                "name": backup.name,
                "path": backup.pool,
                "type": "backup",
                "pool_type": backup.pool_type,
                "remote_host": backup.remote_host,
                "rsync_module": backup.rsync_module,
                "role": "backup"
            }
            stats = volume_service.get_pool_stats(pool_info)
            pools_stats.append(stats)
        
        return PoolsListResponse(pools=pools_stats)
    
    except Exception as e:
        print(f"[ERROR] Failed to list pools: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/volumes", response_model=VolumesListResponse)
async def list_volumes(pool: str, session: dict = Depends(require_auth)):
    """List volumes in a pool."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        
        volumes, warnings = volume_service.list_volumes(pool)
        return VolumesListResponse(pool=pool, volumes=volumes, warnings=warnings)
    
    except Exception as e:
        print(f"[ERROR] Failed to list volumes: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/volume/{pool}/{volume_name}", response_model=VolumeDetailResponse)
async def get_volume(pool: str, volume_name: str, session: dict = Depends(require_auth)):
    """Get detailed information about a volume."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        
        detail = volume_service.get_volume_detail(pool, volume_name)
        if not detail:
            raise HTTPException(status_code=404, detail="Volume not found")
        
        return VolumeDetailResponse(**detail)
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Failed to get volume detail: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/migrate", response_model=TaskResponse)
async def migrate_volume(request: MigrateRequest, session: dict = Depends(require_auth)):
    """Start volume migration."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        migration_service = get_migration_service(config, volume_service)
        task_queue = get_task_queue()

        if task_queue.is_volume_locked(request.source_pool, request.source_volume):
            raise HTTPException(status_code=409, detail="Volume is locked by another operation")

        # Determine effective destination name
        effective_dest = (
            request.rename_dest
            if request.conflict_resolution == 'rename' and request.rename_dest
            else request.source_volume
        )

        # Pre-check: if no resolution (or rename), verify dest doesn't already exist
        if request.conflict_resolution in (None, 'rename'):
            dest_pool = volume_service.get_pool_by_name(request.dest_pool)
            if dest_pool and _volume_exists_in_pool(volume_service, dest_pool, effective_dest):
                return JSONResponse(status_code=409, content={
                    "detail": {
                        "code": "destination_exists",
                        "dest_volume": effective_dest,
                        "dest_pool": request.dest_pool,
                    }
                })

        task_id = task_queue.add_task(
            task_type="migrate",
            source_pool=request.source_pool,
            source_volume=request.source_volume,
            dest_pool=request.dest_pool,
            verify=request.verify,
            delete_source=request.delete_source,
            conflict_resolution=request.conflict_resolution,
            rename_dest=request.rename_dest,
        )

        def _migrate():
            migration_service.migrate_volume(
                task_id,
                request.source_pool,
                request.source_volume,
                request.dest_pool,
                verify=request.verify,
                delete_source=request.delete_source,
                conflict_resolution=request.conflict_resolution,
                rename_dest=request.rename_dest,
            )

        threading.Thread(target=_migrate, daemon=True).start()
        return TaskResponse(task_id=task_id, status="pending", progress_percent=0)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Migration error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/backup", response_model=TaskResponse)
async def backup_volume(request: BackupRequest, session: dict = Depends(require_auth)):
    """Start volume backup."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        backup_service = get_backup_service(config, volume_service)
        task_queue = get_task_queue()
        
        # Check if source volume is locked
        if task_queue.is_volume_locked(request.source_pool, request.source_volume):
            raise HTTPException(status_code=409, detail="Volume is locked by another operation")
        
        # Create task
        task_id = task_queue.add_task(
            task_type="backup",
            source_pool=request.source_pool,
            source_volume=request.source_volume,
            backup_pool=request.backup_pool,
            verify=request.verify
        )
        
        def _backup():
            backup_service.backup_volume(
                task_id,
                request.source_pool,
                request.source_volume,
                request.backup_pool,
                verify=request.verify
            )
        
        thread = threading.Thread(target=_backup, daemon=True)
        thread.start()
        
        return TaskResponse(task_id=task_id, status="pending", progress_percent=0)
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Backup error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/volume/create")
async def create_volume(request: VolumeCreateRequest, session: dict = Depends(require_auth)):
    """Create a new volume directory in a local pool."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        success = volume_service.create_volume(request.pool, request.volume_name)
        if not success:
            raise HTTPException(status_code=400, detail="Failed to create volume — it may already exist or the pool is remote")
        return {"status": "created"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Volume create error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/rename")
async def rename_volume(request: RenameRequest, session: dict = Depends(require_auth)):
    """Rename a volume."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        
        success = volume_service.rename_volume(request.pool, request.old_name, request.new_name)
        
        if not success:
            raise HTTPException(status_code=400, detail="Failed to rename volume")
        
        return {"status": "ok", "message": "Volume renamed"}
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Rename error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/delete")
async def delete_volume(request: DeleteRequest, session: dict = Depends(require_auth)):
    """Delete a volume or backup artifact."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        task_queue = get_task_queue()
        
        detail = volume_service.get_volume_detail(request.pool, request.volume_name)
        if detail is None:
            raise HTTPException(status_code=404, detail="Volume or backup not found")

        # Warn when deleting a volume without a confirmed backup
        if not detail.get("backups") and not request.confirm:
            return {
                "status": "warning",
                "message": "No backups found for this volume. Confirm to delete.",
                "require_confirmation": True
            }

        task_id = task_queue.add_task(
            task_type="delete",
            pool=request.pool,
            volume_name=request.volume_name
        )

        task_queue.start_task(task_id)
        def _delete():
            try:
                success = volume_service.delete_volume(request.pool, request.volume_name)
                if not success:
                    task_queue.complete_task(task_id, success=False, error="Failed to delete volume")
                else:
                    task_queue.complete_task(task_id, success=True)
            except Exception as exc:
                task_queue.complete_task(task_id, success=False, error=str(exc))

        thread = threading.Thread(target=_delete, daemon=True)
        thread.start()

        return TaskResponse(task_id=task_id, status="pending", progress_percent=0, task_type="delete")
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Delete error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/restore", response_model=TaskResponse)
async def restore_backup(request: RestoreRequest, session: dict = Depends(require_auth)):
    """Restore a backup archive into a pool."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        backup_service = get_backup_service(config, volume_service)
        task_queue = get_task_queue()

        # Effective dest name: use rename_dest when resolution is 'rename'
        effective_dest = (
            request.rename_dest
            if request.conflict_resolution == 'rename' and request.rename_dest
            else request.dest_volume
        )

        # Pre-check: if no resolution (or rename), verify dest doesn't already exist
        if request.conflict_resolution in (None, 'rename'):
            dest_pool = volume_service.get_pool_by_name(request.dest_pool)
            if dest_pool and _volume_exists_in_pool(volume_service, dest_pool, effective_dest):
                return JSONResponse(status_code=409, content={
                    "detail": {
                        "code": "destination_exists",
                        "dest_volume": effective_dest,
                        "dest_pool": request.dest_pool,
                    }
                })

        task_id = task_queue.add_task(
            task_type="restore",
            backup_pool=request.backup_pool,
            backup_file=request.backup_file,
            dest_pool=request.dest_pool,
            dest_volume_name=effective_dest,
            verify=True,
            conflict_resolution=request.conflict_resolution,
        )

        def _restore():
            backup_service.restore_backup(
                task_id,
                request.backup_pool,
                request.backup_file,
                request.dest_pool,
                effective_dest,
                conflict_resolution=request.conflict_resolution,
            )

        threading.Thread(target=_restore, daemon=True).start()
        return TaskResponse(task_id=task_id, status="pending", task_type="restore", progress_percent=0)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Restore error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/pool/create")
async def create_pool(request: PoolCreateRequest, session: dict = Depends(require_auth)):
    """Create a new pool."""
    try:
        config = get_config()
        volume_service = get_volume_service(config)
        
        success = volume_service.create_pool(request.path)
        
        if not success:
            raise HTTPException(status_code=400, detail="Failed to create pool")
        
        return {"status": "ok", "message": "Pool created"}
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Pool creation error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/tasks", response_model=TasksListResponse)
async def list_tasks(session: dict = Depends(require_auth)):
    """List persisted task history."""
    try:
        task_queue = get_task_queue()
        sorted_tasks = sorted(task_queue.tasks.values(), key=lambda item: item.get("created_at", 0), reverse=True)
        tasks = [
            TaskProgressResponse(
                task_id=item["task_id"],
                status=item["status"],
                task_type=item.get("type"),
                progress_percent=item.get("progress_percent", 0),
                current_operation=item.get("current_operation"),
                elapsed_seconds=item.get("elapsed_seconds", 0),
                estimated_remaining_seconds=item.get("estimated_remaining_seconds"),
                error=item.get("error"),
                params=item.get("params", {}),
                started_at=item.get("started_at"),
                completed_at=item.get("completed_at"),
            )
            for item in sorted_tasks
        ]
        return TasksListResponse(tasks=tasks)
    except Exception as e:
        print(f"[ERROR] Task list error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/refresh")
async def refresh(session: dict = Depends(require_auth)):
    """Refresh pools (re-read from disk)."""
    try:
        # Configuration is already re-read on each request
        return {"status": "ok", "message": "Pools refreshed"}
    except Exception as e:
        print(f"[ERROR] Refresh error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/debug/cleanup")
async def cleanup_debug(session: dict = Depends(require_auth)):
    """Delete persisted task data and lock files for troubleshooting."""
    try:
        task_queue = get_task_queue()
        deleted_tasks_file = 0
        deleted_lock_files = 0

        if task_queue.tasks_file.exists():
            task_queue.tasks_file.unlink()
            deleted_tasks_file = 1

        if task_queue.locks_dir.exists():
            for lock_file in task_queue.locks_dir.glob("*.lock"):
                try:
                    lock_file.unlink()
                    deleted_lock_files += 1
                except Exception:
                    pass

        return {
            "status": "ok",
            "message": "Cleanup completed",
            "deleted_tasks_file": deleted_tasks_file,
            "deleted_lock_files": deleted_lock_files
        }
    except Exception as e:
        print(f"[ERROR] Cleanup error: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/task/{task_id}/progress", response_model=TaskProgressResponse)
async def get_task_progress(task_id: str, session: dict = Depends(require_auth)):
    """Get task progress."""
    try:
        task_queue = get_task_queue()
        task = task_queue.get_task(task_id)
        
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        return TaskProgressResponse(
            task_id=task["task_id"],
            status=task["status"],
            task_type=task.get("type"),
            progress_percent=task.get("progress_percent", 0),
            current_operation=task.get("current_operation"),
            elapsed_seconds=task.get("elapsed_seconds", 0),
            estimated_remaining_seconds=task.get("estimated_remaining_seconds"),
            error=task.get("error"),
            params=task.get("params", {}),
            started_at=task.get("started_at"),
            completed_at=task.get("completed_at"),
        )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Task progress error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/task/{task_id}/logs")
async def get_task_logs(task_id: str, session: dict = Depends(require_auth)):
    """Get captured log lines for a task."""
    config = get_config()
    tmp_dir = str(config.tmp_dir) if config.tmp_dir else "/tmp"
    task_queue = get_task_queue(tmp_dir=tmp_dir)
    return {
        "task_id": task_id,
        "lines": task_queue.get_task_logs(task_id)
    }


# ── Backup Schedule Endpoints ─────────────────────────────────────────────────

def _job_to_model(job: dict) -> BackupSchedule:
    return BackupSchedule(
        id=job['id'],
        name=job['name'],
        cron=job['cron'],
        backup_pool=job['backup_pool'],
        volumes=job.get('volumes', []),
        retention=job.get('retention', 7),
        enabled=job.get('enabled', True),
        next_run=job.get('next_run'),
    )


@router.get("/api/schedules", response_model=SchedulesResponse)
async def list_schedules(session: dict = Depends(require_auth)):
    """List all backup schedule jobs."""
    svc = get_scheduler_service()
    return SchedulesResponse(schedules=[_job_to_model(j) for j in svc.list_jobs()])


@router.post("/api/schedules", response_model=BackupSchedule)
async def create_schedule(body: BackupScheduleCreate, session: dict = Depends(require_auth)):
    """Create a new backup schedule job."""
    from apscheduler.triggers.cron import CronTrigger
    try:
        CronTrigger.from_crontab(body.cron, timezone='UTC')
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {body.cron!r}")
    svc = get_scheduler_service()
    job = svc.create_job({
        'name': body.name,
        'cron': body.cron,
        'backup_pool': body.backup_pool,
        'volumes': [v.model_dump() for v in body.volumes],
        'retention': body.retention,
    })
    return _job_to_model(job)


@router.put("/api/schedules/{job_id}", response_model=BackupSchedule)
async def update_schedule(job_id: str, body: BackupScheduleCreate, session: dict = Depends(require_auth)):
    """Update an existing backup schedule job."""
    from apscheduler.triggers.cron import CronTrigger
    try:
        CronTrigger.from_crontab(body.cron, timezone='UTC')
    except Exception:
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {body.cron!r}")
    svc = get_scheduler_service()
    job = svc.update_job(job_id, {
        'name': body.name,
        'cron': body.cron,
        'backup_pool': body.backup_pool,
        'volumes': [v.model_dump() for v in body.volumes],
        'retention': body.retention,
    })
    if job is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _job_to_model(job)


@router.delete("/api/schedules/{job_id}")
async def delete_schedule(job_id: str, session: dict = Depends(require_auth)):
    """Delete a backup schedule job."""
    svc = get_scheduler_service()
    if not svc.delete_job(job_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"status": "deleted"}


@router.post("/api/schedules/{job_id}/toggle", response_model=BackupSchedule)
async def toggle_schedule(job_id: str, session: dict = Depends(require_auth)):
    """Enable or disable a backup schedule job."""
    svc = get_scheduler_service()
    job = svc.toggle_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _job_to_model(job)


@router.post("/api/schedules/{job_id}/run")
async def run_schedule_now(job_id: str, session: dict = Depends(require_auth)):
    """Trigger a backup schedule job immediately."""
    svc = get_scheduler_service()
    if job_id not in svc.jobs:
        raise HTTPException(status_code=404, detail="Schedule not found")
    svc.trigger_now(job_id)
    return {"status": "triggered"}
