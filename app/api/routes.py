"""API routes for v-shipper."""

import base64
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from app.models import (
    LoginRequest, PoolsListResponse, VolumesListResponse, VolumeDetailResponse,
    MigrateRequest, BackupRequest, RenameRequest, DeleteRequest, RestoreRequest, PoolCreateRequest,
    TaskResponse, TaskProgressResponse, HealthResponse, PoolStats, VolumeInfo
)
from app.services.volume_service import get_volume_service
from app.services.migration_service import get_migration_service
from app.services.backup_service import get_backup_service
from app.services.task_queue import get_task_queue
from app.config import validate_auth, get_config

router = APIRouter()

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
            # Generate session ID
            import uuid
            session_id = str(uuid.uuid4())
            sessions[session_id] = {"authenticated": True, "user": request.username}
            
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
    if session and session.get("session_id") in sessions:
        del sessions[session.get("session_id")]
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
                "type": host.pool_type
            }
            stats = volume_service.get_pool_stats(pool_info)
            pools_stats.append(stats)
        
        # Add backup pools
        for backup in config.backup_pools:
            pool_info = {
                "name": backup.name,
                "path": backup.path,
                "type": "backup"
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
        
        volumes = volume_service.list_volumes(pool)
        return VolumesListResponse(pool=pool, volumes=volumes)
    
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
        
        # Check if source volume is locked
        if task_queue.is_volume_locked(request.source_pool, request.source_volume):
            raise HTTPException(status_code=409, detail="Volume is locked by another operation")
        
        # Create task
        task_id = task_queue.add_task(
            task_type="migrate",
            source_pool=request.source_pool,
            source_volume=request.source_volume,
            dest_pool=request.dest_pool,
            verify=request.verify,
            delete_source=request.delete_source
        )
        
        # Execute migration in background
        import threading
        def _migrate():
            migration_service.migrate_volume(
                task_id,
                request.source_pool,
                request.source_volume,
                request.dest_pool,
                verify=request.verify,
                delete_source=request.delete_source
            )
        
        thread = threading.Thread(target=_migrate, daemon=True)
        thread.start()
        
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
        
        # Execute backup in background
        import threading
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

        success = volume_service.delete_volume(request.pool, request.volume_name)
        
        if not success:
            raise HTTPException(status_code=400, detail="Failed to delete volume")
        
        return {"status": "ok", "message": "Volume deleted"}
    
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

        task_id = task_queue.add_task(
            task_type="restore",
            source_pool=request.backup_pool,
            source_volume=request.backup_file,
            dest_pool=request.dest_pool,
            verify=True
        )

        import threading
        def _restore():
            backup_service.restore_backup(
                task_id,
                request.backup_pool,
                request.backup_file,
                request.dest_pool,
                request.dest_volume
            )

        thread = threading.Thread(target=_restore, daemon=True)
        thread.start()

        return TaskResponse(task_id=task_id, status="pending", progress_percent=0)
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


@router.get("/api/refresh")
async def refresh(session: dict = Depends(require_auth)):
    """Refresh pools (re-read from disk)."""
    try:
        # Configuration is already re-read on each request
        return {"status": "ok", "message": "Pools refreshed"}
    except Exception as e:
        print(f"[ERROR] Refresh error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


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
            progress_percent=task.get("progress_percent", 0),
            current_operation=task.get("current_operation"),
            elapsed_seconds=task.get("elapsed_seconds", 0),
            estimated_remaining_seconds=task.get("estimated_remaining_seconds"),
            error=task.get("error")
        )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Task progress error: {e}", flush=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/task/{task_id}/logs")
async def get_task_logs(task_id: str, session: dict = Depends(require_auth)):
    """Get task logs."""
    # Note: Logs are printed to stdout, not stored in app
    return {
        "status": "ok",
        "message": "Task logs are available in container stdout. Use 'docker logs' to view.",
        "task_id": task_id
    }
