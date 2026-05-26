"""Data models for v-shipper application."""

import base64
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


class DockerHost(BaseModel):
    """Docker host pool configuration."""
    name: str
    ip: str
    pool: str
    pool_type: str  # 'local' or 'remote'
    ssh_user: Optional[str] = None
    ssh_key: Optional[str] = None


class BackupPool(BaseModel):
    """Backup pool configuration."""
    name: str
    path: str


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
    total_gb: float
    used_gb: float
    available_gb: float
    usage_percent: float


class PoolsListResponse(BaseModel):
    """List of pools with stats."""
    pools: List[PoolStats]


class VolumeInfo(BaseModel):
    """Volume metadata."""
    name: str
    path: str
    size_gb: float
    created_timestamp: Optional[int] = None
    backups: List[str] = Field(default_factory=list)


class VolumesListResponse(BaseModel):
    """List of volumes in a pool."""
    pool: str
    volumes: List[VolumeInfo]


class VolumeDetailResponse(BaseModel):
    """Detailed volume information."""
    name: str
    pool: str
    size_gb: float
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


class PoolCreateRequest(BaseModel):
    """Create new pool request."""
    name: str
    path: str


class TaskResponse(BaseModel):
    """Task response."""
    task_id: str
    status: str  # pending, running, completed, failed
    progress_percent: int = 0


class TaskProgressResponse(BaseModel):
    """Task progress details."""
    task_id: str
    status: str
    progress_percent: int
    current_operation: Optional[str] = None
    elapsed_seconds: int = 0
    estimated_remaining_seconds: Optional[int] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str = "1.0.0"
