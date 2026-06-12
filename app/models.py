"""Data models for v-shipper application."""

import base64
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator


class DockerHost(BaseModel):
    """Docker host pool configuration."""
    name: str
    pool: str
    pool_type: str = "local"  # 'local' or 'remote'
    remote_host: Optional[str] = None
    rsync_module: Optional[str] = None


class BackupPool(BaseModel):
    """Backup pool configuration."""
    name: str
    pool: str
    pool_type: str = "local"  # 'local' or 'remote'
    remote_host: Optional[str] = None
    rsync_module: Optional[str] = None


class WebUIConfig(BaseModel):
    """Web UI configuration."""
    port: int = 80
    admin_user: str
    admin_password: str


class AppConfig(BaseModel):
    """Application configuration from YAML."""
    docker_hosts: List[DockerHost] = Field(default_factory=list)
    backup_pools: List[BackupPool] = Field(default_factory=list)
    web_ui: WebUIConfig
    tmp_dir: str = "/tmp"
    staging_dir: str = "/tmp/staging"
    config_dir: str = "/config"
    
    @field_validator('docker_hosts', 'backup_pools', mode='before')
    @classmethod
    def validate_lists(cls, v):
        if v is None:
            return []
        return v


# API Request/Response Models

class LoginRequest(BaseModel):
    """Login request."""
    username: str
    password: str


class PoolStats(BaseModel):
    """Pool statistics."""
    name: str
    pool_type: str
    role: str = "docker"
    total_gb: float
    used_gb: float
    available_gb: float
    usage_percent: float
    reachable: bool = True
    error: Optional[str] = None


class PoolsListResponse(BaseModel):
    """List of pools with stats."""
    pools: List[PoolStats]


class VolumeInfo(BaseModel):
    """Volume metadata."""
    name: str
    path: str
    size_gb: float
    size_bytes: int = 0
    size_loading: bool = False
    created_timestamp: Optional[int] = None
    backups: List[str] = Field(default_factory=list)


class VolumesListResponse(BaseModel):
    """List of volumes in a pool."""
    pool: str
    volumes: List[VolumeInfo]
    warnings: List[str] = Field(default_factory=list)


class TaskResponse(BaseModel):
    """Task response."""
    task_id: str
    status: str  # pending, running, completed, failed
    task_type: Optional[str] = None
    progress_percent: int = 0


class TaskProgressResponse(BaseModel):
    """Task progress details."""
    task_id: str
    status: str
    task_type: Optional[str] = None
    progress_percent: int
    current_operation: Optional[str] = None
    elapsed_seconds: int = 0
    estimated_remaining_seconds: Optional[int] = None
    error: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class TasksListResponse(BaseModel):
    """List of persisted tasks."""
    tasks: List[TaskProgressResponse]


class VolumeDetailResponse(BaseModel):
    """Detailed volume information."""
    name: str
    pool: str
    size_gb: float
    size_bytes: int = 0
    created_timestamp: Optional[int] = None
    backups: List[str]
    locked: bool = False
    permissions: Optional[str] = None


class MigrateRequest(BaseModel):
    """Migration request."""
    source_pool: str
    source_volume: str
    dest_pool: str
    verify: bool = True
    delete_source: bool = False
    conflict_resolution: Optional[str] = None  # 'overwrite', 'merge', 'rename'
    rename_dest: Optional[str] = None


class BackupRequest(BaseModel):
    """Backup request."""
    source_pool: str
    source_volume: str
    backup_pool: str
    verify: bool = True


class RenameRequest(BaseModel):
    """Rename request."""
    pool: str
    old_name: str
    new_name: str


class DeleteRequest(BaseModel):
    """Delete request."""
    pool: str
    volume_name: str
    confirm: bool = False


class RestoreRequest(BaseModel):
    """Restore backup request."""
    backup_pool: str
    backup_file: str
    dest_pool: str
    dest_volume: str
    conflict_resolution: Optional[str] = None  # 'overwrite', 'merge', 'rename'
    rename_dest: Optional[str] = None


class PoolCreateRequest(BaseModel):
    """Create new pool request."""
    name: str
    path: str


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str = "0.0.11"


# ── Backup Schedule Models ────────────────────────────────────────────────────

class ScheduleVolume(BaseModel):
    """A volume entry in a backup schedule."""
    pool: str
    volume: str


class BackupSchedule(BaseModel):
    """A persisted backup schedule job."""
    id: str
    name: str
    cron: str
    backup_pool: str
    volumes: List[ScheduleVolume]
    retention: int = 7
    enabled: bool = True
    next_run: Optional[float] = None  # UTC unix timestamp


class BackupScheduleCreate(BaseModel):
    """Request body for creating or updating a backup schedule."""
    name: str
    cron: str
    backup_pool: str
    volumes: List[ScheduleVolume]
    retention: int = 7


class SchedulesResponse(BaseModel):
    """List of backup schedules."""
    schedules: List[BackupSchedule]
