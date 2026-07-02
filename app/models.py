"""Data models for v-shipper application."""

import base64
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

from app.validation import (
    validate_name,
    validate_backup_file,
    validate_pool_path,
    validate_mode,
    validate_owner_token,
    validate_cron,
    validate_telegram_token,
    validate_chat_id,
    validate_thread_id,
    validate_server_url,
    validate_remote_host,
    MAX_TEMPLATE_LEN,
    MAX_CREDENTIAL_LEN,
)

from app import __version__


class _PoolBase(BaseModel):
    """Shared validation for docker/backup pool config entries."""
    name: str
    pool: str
    pool_type: Literal["local", "remote"] = "local"
    remote_host: Optional[str] = None
    rsync_module: Optional[str] = None
    api_host: Optional[str] = None
    api_key: Optional[str] = None
    docker_socket: bool = False
    docker_host_path: Optional[str] = None
    container_stop_timeout: int = 120

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        return validate_name(v, "pool name")

    @model_validator(mode="after")
    def _validate_pool(self):
        if self.pool_type == "remote":
            if not self.remote_host or not self.rsync_module:
                raise ValueError(
                    f"remote pool '{self.name}' requires both remote_host and rsync_module"
                )
            validate_remote_host(self.remote_host)
            validate_name(self.rsync_module, "rsync_module")
        else:
            validate_pool_path(self.pool, "pool path")
        if self.api_host:
            validate_remote_host(self.api_host)
            if not self.api_key:
                raise ValueError(
                    f"pool '{self.name}' has api_host but is missing api_key"
                )
        if self.docker_host_path:
            validate_pool_path(self.docker_host_path, "docker_host_path")
        return self


class DockerHost(_PoolBase):
    """Docker host pool configuration."""


class BackupPool(_PoolBase):
    """Backup pool configuration."""


class WebUIConfig(BaseModel):
    """Web UI configuration."""
    port: int = Field(80, ge=1, le=65535)
    admin_user: str
    admin_password: str

    @field_validator("admin_user")
    @classmethod
    def _validate_user(cls, v):
        return validate_name(v, "admin_user")

    @field_validator("admin_password")
    @classmethod
    def _validate_password(cls, v):
        # Opaque (may be base64 or plaintext) — length cap only, no charset restriction.
        if not isinstance(v, str) or len(v) > MAX_CREDENTIAL_LEN:
            raise ValueError("admin_password is invalid or too long")
        return v


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

    @field_validator("tmp_dir", "staging_dir", "config_dir")
    @classmethod
    def _validate_dirs(cls, v, info):
        return validate_pool_path(v, info.field_name)

    @model_validator(mode="after")
    def _validate_unique_pool_names(self):
        names = [h.name for h in self.docker_hosts] + [b.name for b in self.backup_pools]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate pool names are not allowed: {', '.join(sorted(dupes))}")
        return self


# API Request/Response Models

class LoginRequest(BaseModel):
    """Login request."""
    username: str = Field(..., max_length=MAX_CREDENTIAL_LEN)
    password: str = Field(..., max_length=MAX_CREDENTIAL_LEN)


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
    has_helper: bool = False
    helper_version: Optional[str] = None
    docker_socket: bool = False
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
    status: str  # pending, running, completed, failed, cancelled
    task_type: Optional[str] = None
    progress_percent: int = 0


class TaskProgressResponse(BaseModel):
    """Task progress details."""
    task_id: str
    status: str
    task_type: Optional[str] = None
    progress_percent: int
    current_operation: Optional[str] = None
    elapsed_seconds: float = 0
    estimated_remaining_seconds: Optional[int] = None
    error: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


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


ConflictResolution = Optional[Literal["overwrite", "merge", "rename"]]


class MigrateRequest(BaseModel):
    """Migration request."""
    source_pool: str
    source_volume: str
    dest_pool: str
    verify: bool = True
    delete_source: bool = False
    conflict_resolution: ConflictResolution = None
    rename_dest: Optional[str] = None
    stop_containers_before: bool = False
    start_containers_after: bool = False

    @field_validator("source_pool", "dest_pool")
    @classmethod
    def _validate_pool(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("source_volume")
    @classmethod
    def _validate_volume(cls, v):
        return validate_name(v, "source_volume")

    @field_validator("rename_dest")
    @classmethod
    def _validate_rename_dest(cls, v):
        return v if v is None else validate_name(v, "rename_dest")


class BackupRequest(BaseModel):
    """Backup request."""
    source_pool: str
    source_volume: str
    backup_pool: str
    verify: bool = True
    stop_containers_before: bool = False
    start_containers_after: bool = False

    @field_validator("source_pool", "source_volume", "backup_pool")
    @classmethod
    def _validate(cls, v, info):
        return validate_name(v, info.field_name)


class RenameRequest(BaseModel):
    """Rename request."""
    pool: str
    old_name: str
    new_name: str
    stop_containers_before: bool = False

    @field_validator("pool", "old_name", "new_name")
    @classmethod
    def _validate(cls, v, info):
        return validate_name(v, info.field_name)


class DeleteRequest(BaseModel):
    """Delete request."""
    pool: str
    volume_name: str
    confirm: bool = False
    stop_containers_before: bool = False

    @field_validator("pool", "volume_name")
    @classmethod
    def _validate(cls, v, info):
        return validate_name(v, info.field_name)


class PermissionsRequest(BaseModel):
    """Change-permissions request (chmod / chown)."""
    pool: str
    volume_name: str
    mode: Optional[str] = None     # run chmod if present
    owner: Optional[str] = None    # user token; run chown if owner + group present
    group: Optional[str] = None    # group token
    stop_containers_before: bool = False
    start_containers_after: bool = False

    @field_validator("pool", "volume_name")
    @classmethod
    def _validate(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v):
        return v if v is None else validate_mode(v, "mode")

    @field_validator("owner", "group")
    @classmethod
    def _validate_owner(cls, v, info):
        return v if v is None else validate_owner_token(v, info.field_name)

    @model_validator(mode="after")
    def _require_change(self):
        if (self.owner is None) != (self.group is None):
            raise ValueError("owner and group must be provided together")
        if not self.mode and self.owner is None:
            raise ValueError("at least a mode or an owner/group change is required")
        return self


class RestoreRequest(BaseModel):
    """Restore backup request."""
    backup_pool: str
    backup_file: str
    dest_pool: str
    dest_volume: str
    conflict_resolution: ConflictResolution = None
    rename_dest: Optional[str] = None

    @field_validator("backup_pool", "dest_pool", "dest_volume")
    @classmethod
    def _validate_names(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("backup_file")
    @classmethod
    def _validate_file(cls, v):
        return validate_backup_file(v)

    @field_validator("rename_dest")
    @classmethod
    def _validate_rename_dest(cls, v):
        return v if v is None else validate_name(v, "rename_dest")


# Conflict handling for bulk migrate/restore. "skip" is resolved by the bulk
# runner (skip items whose destination already exists); "overwrite"/"merge" are
# passed through to the underlying service as a ConflictResolution.
BulkConflict = Optional[Literal["skip", "overwrite", "merge"]]

_MAX_BULK_ITEMS = 500


def _validate_name_list(values, field: str):
    if not values:
        raise ValueError(f"{field} must not be empty")
    if len(values) > _MAX_BULK_ITEMS:
        raise ValueError(f"{field} exceeds the {_MAX_BULK_ITEMS}-item limit")
    return [validate_name(v, field) for v in values]


class BulkBackupRequest(BaseModel):
    """Back up multiple volumes from one pool to one backup pool."""
    source_pool: str
    source_volumes: List[str]
    backup_pool: str
    verify: bool = True
    stop_containers_before: bool = False
    start_containers_after: bool = False

    @field_validator("source_pool", "backup_pool")
    @classmethod
    def _validate_pool(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("source_volumes")
    @classmethod
    def _validate_volumes(cls, v):
        return _validate_name_list(v, "source_volumes")


class BulkMigrateRequest(BaseModel):
    """Migrate multiple volumes from one pool to one destination pool."""
    source_pool: str
    source_volumes: List[str]
    dest_pool: str
    verify: bool = True
    delete_source: bool = False
    conflict_resolution: BulkConflict = "skip"
    stop_containers_before: bool = False
    start_containers_after: bool = False

    @field_validator("source_pool", "dest_pool")
    @classmethod
    def _validate_pool(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("source_volumes")
    @classmethod
    def _validate_volumes(cls, v):
        return _validate_name_list(v, "source_volumes")


class BulkDeleteRequest(BaseModel):
    """Delete multiple volumes or backup artifacts from one pool."""
    pool: str
    volumes: List[str]
    confirm: bool = False
    stop_containers_before: bool = False

    @field_validator("pool")
    @classmethod
    def _validate_pool(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("volumes")
    @classmethod
    def _validate_volumes(cls, v):
        return _validate_name_list(v, "volumes")


class BulkPermissionsRequest(BaseModel):
    """Apply the same chmod/chown to multiple volumes in one pool."""
    pool: str
    volumes: List[str]
    mode: Optional[str] = None
    owner: Optional[str] = None
    group: Optional[str] = None
    stop_containers_before: bool = False
    start_containers_after: bool = False

    @field_validator("pool")
    @classmethod
    def _validate_pool(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("volumes")
    @classmethod
    def _validate_volumes(cls, v):
        return _validate_name_list(v, "volumes")

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v):
        return v if v is None else validate_mode(v, "mode")

    @field_validator("owner", "group")
    @classmethod
    def _validate_owner(cls, v, info):
        return v if v is None else validate_owner_token(v, info.field_name)

    @model_validator(mode="after")
    def _require_change(self):
        if (self.owner is None) != (self.group is None):
            raise ValueError("owner and group must be provided together")
        if not self.mode and self.owner is None:
            raise ValueError("at least a mode or an owner/group change is required")
        return self


class BulkRestoreRequest(BaseModel):
    """Restore multiple backup archives from one backup pool into one pool.

    Each archive's destination volume name is derived from its filename (the
    pool prefix and timestamp are stripped), matching the single-restore default.
    """
    backup_pool: str
    backup_files: List[str]
    dest_pool: str
    conflict_resolution: BulkConflict = "skip"

    @field_validator("backup_pool", "dest_pool")
    @classmethod
    def _validate_pool(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("backup_files")
    @classmethod
    def _validate_files(cls, v):
        if not v:
            raise ValueError("backup_files must not be empty")
        if len(v) > _MAX_BULK_ITEMS:
            raise ValueError(f"backup_files exceeds the {_MAX_BULK_ITEMS}-item limit")
        return [validate_backup_file(f) for f in v]


class PoolCreateRequest(BaseModel):
    """Create new pool request."""
    name: str
    path: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        return validate_name(v, "name")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v):
        return validate_pool_path(v, "path")


class VolumeCreateRequest(BaseModel):
    """Create new volume (directory) request."""
    pool: str
    volume_name: str

    @field_validator("pool", "volume_name")
    @classmethod
    def _validate(cls, v, info):
        return validate_name(v, info.field_name)


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str = __version__


# ── Backup Schedule Models ────────────────────────────────────────────────────

class ScheduleVolume(BaseModel):
    """A volume entry in a backup schedule."""
    pool: str
    volume: str

    @field_validator("pool", "volume")
    @classmethod
    def _validate(cls, v, info):
        return validate_name(v, info.field_name)


class BackupSchedule(BaseModel):
    """A persisted backup schedule job."""
    id: str
    name: str
    cron: str
    backup_pool: str
    volumes: List[ScheduleVolume]
    retention: int = 7
    enabled: bool = True
    stop_containers_before: bool = False
    start_containers_after: bool = False
    next_run: Optional[float] = None  # UTC unix timestamp


class BackupScheduleCreate(BaseModel):
    """Request body for creating or updating a backup schedule."""
    name: str
    cron: str
    backup_pool: str
    volumes: List[ScheduleVolume]
    retention: int = Field(7, ge=1, le=365)
    stop_containers_before: bool = False
    start_containers_after: bool = False

    @field_validator("name", "backup_pool")
    @classmethod
    def _validate_names(cls, v, info):
        return validate_name(v, info.field_name)

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, v):
        return validate_cron(v)


class SchedulesResponse(BaseModel):
    """List of backup schedules."""
    schedules: List[BackupSchedule]


# ── Notification Models ───────────────────────────────────────────────────────

class NotificationConfig(BaseModel):
    """A persisted Telegram notification configuration."""
    id: str
    name: str
    token: str
    chat_id: str
    message_thread_id: Optional[str] = None
    topics: List[str]
    on_failure_only: bool = False
    server_url: str = "https://api.telegram.org"
    message_template: Optional[str] = None
    enabled: bool = True


class NotificationCreate(BaseModel):
    """Request body for creating or updating a notification config."""
    name: str
    token: str
    chat_id: str
    message_thread_id: Optional[str] = None
    topics: List[str]
    on_failure_only: bool = False
    server_url: str = "https://api.telegram.org"
    message_template: Optional[str] = Field(None, max_length=MAX_TEMPLATE_LEN)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        return validate_name(v, "name")

    @field_validator("token")
    @classmethod
    def _validate_token(cls, v):
        return validate_telegram_token(v)

    @field_validator("chat_id")
    @classmethod
    def _validate_chat_id(cls, v):
        return validate_chat_id(v)

    @field_validator("message_thread_id")
    @classmethod
    def _validate_thread_id(cls, v):
        return validate_thread_id(v)

    @field_validator("server_url")
    @classmethod
    def _validate_server_url(cls, v):
        return validate_server_url(v)

    @field_validator("topics")
    @classmethod
    def _validate_topics(cls, v):
        allowed = {"backup", "schedule", "migrate", "restore", "delete", "rename", "create", "permissions"}
        for t in v:
            if t not in allowed:
                raise ValueError(f"unknown topic: {t}")
        return v


class NotificationsResponse(BaseModel):
    """List of notification configs."""
    notifications: List[NotificationConfig]
