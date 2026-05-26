"""Volume management service."""

import os
import shutil
import time
from pathlib import Path
from typing import List, Dict, Optional
from app.models import VolumeInfo, PoolStats
from app.services.task_queue import get_task_queue


class VolumeService:
    """Service for managing volumes in pools."""
    
    def __init__(self, config):
        self.config = config
        self.task_queue = get_task_queue()
    
    def get_pool_by_name(self, pool_name: str) -> Optional[Dict]:
        """Get pool configuration by name."""
        for host in self.config.docker_hosts:
            if host.name == pool_name:
                return {
                    "name": host.name,
                    "path": host.pool,
                    "type": host.pool_type,
                    "ip": host.ip,
                    "ssh_user": host.ssh_user,
                    "ssh_key": host.ssh_key
                }
        return None
    
    def get_backup_pool_by_name(self, pool_name: str) -> Optional[Dict]:
        """Get backup pool configuration by name."""
        for backup in self.config.backup_pools:
            if backup.name == pool_name:
                return {
                    "name": backup.name,
                    "path": backup.path
                }
        return None
    
    def get_pool_stats(self, pool: Dict) -> PoolStats:
        """Get disk usage statistics for a pool."""
        pool_path = pool.get("path") or pool.get("pool")
        
        try:
            stat_info = os.statvfs(pool_path)
            total_bytes = stat_info.f_blocks * stat_info.f_frsize
            free_bytes = stat_info.f_bavail * stat_info.f_frsize
            used_bytes = total_bytes - free_bytes
            
            total_gb = total_bytes / (1024 ** 3)
            used_gb = used_bytes / (1024 ** 3)
            available_gb = free_bytes / (1024 ** 3)
            usage_percent = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0
            
            return PoolStats(
                name=pool["name"],
                pool_type=pool.get("type", "unknown"),
                total_gb=round(total_gb, 2),
                used_gb=round(used_gb, 2),
                available_gb=round(available_gb, 2),
                usage_percent=round(usage_percent, 2)
            )
        except Exception as e:
            print(f"[ERROR] Failed to get pool stats for {pool['name']}: {e}", flush=True)
            return PoolStats(
                name=pool["name"],
                pool_type=pool.get("type", "unknown"),
                total_gb=0,
                used_gb=0,
                available_gb=0,
                usage_percent=0
            )
    
    def list_volumes(self, pool_name: str) -> List[VolumeInfo]:
        """List all volumes in a pool."""
        pool = self.get_pool_by_name(pool_name)
        
        if not pool:
            print(f"[ERROR] Pool {pool_name} not found", flush=True)
            return []
        
        pool_path = Path(pool["path"])
        volumes = []
        
        try:
            if not pool_path.exists():
                print(f"[WARNING] Pool path {pool_path} does not exist", flush=True)
                return []
            
            for volume_dir in pool_path.iterdir():
                if volume_dir.is_dir() and not volume_dir.name.startswith('.'):
                    size_gb = self._get_dir_size(volume_dir) / (1024 ** 3)
                    
                    # Get creation time (stat ctime)
                    stat_info = volume_dir.stat()
                    created_timestamp = int(stat_info.st_ctime)
                    
                    # Check for backups (simplified - check backup pools for this volume)
                    backups = self._find_backups(volume_dir.name)
                    
                    # Get permissions
                    permissions = oct(stat_info.st_mode)[-3:]
                    
                    volumes.append(VolumeInfo(
                        name=volume_dir.name,
                        path=str(volume_dir),
                        size_gb=round(size_gb, 2),
                        created_timestamp=created_timestamp,
                        backups=backups
                    ))
        except Exception as e:
            print(f"[ERROR] Failed to list volumes in {pool_name}: {e}", flush=True)
        
        return sorted(volumes, key=lambda v: v.name)
    
    def rename_volume(self, pool_name: str, old_name: str, new_name: str) -> bool:
        """Rename a volume."""
        pool = self.get_pool_by_name(pool_name)
        if not pool:
            return False
        
        old_path = Path(pool["path"]) / old_name
        new_path = Path(pool["path"]) / new_name
        
        try:
            if not old_path.exists():
                print(f"[ERROR] Volume {old_name} not found", flush=True)
                return False
            
            if new_path.exists():
                print(f"[ERROR] Volume {new_name} already exists", flush=True)
                return False
            
            old_path.rename(new_path)
            print(f"[INFO] Renamed volume {old_name} to {new_name}", flush=True)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to rename volume: {e}", flush=True)
            return False
    
    def delete_volume(self, pool_name: str, volume_name: str) -> bool:
        """Delete a volume."""
        pool = self.get_pool_by_name(pool_name)
        if not pool:
            return False
        
        volume_path = Path(pool["path"]) / volume_name
        
        try:
            if not volume_path.exists():
                print(f"[ERROR] Volume {volume_name} not found", flush=True)
                return False
            
            shutil.rmtree(volume_path)
            print(f"[INFO] Deleted volume {volume_name}", flush=True)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to delete volume: {e}", flush=True)
            return False
    
    def create_pool(self, path: str) -> bool:
        """Create a new empty pool."""
        try:
            pool_path = Path(path)
            pool_path.mkdir(parents=True, exist_ok=True)
            pool_path.chmod(0o777)
            print(f"[INFO] Created pool at {path}", flush=True)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to create pool: {e}", flush=True)
            return False
    
    def get_volume_detail(self, pool_name: str, volume_name: str) -> Optional[Dict]:
        """Get detailed information about a volume."""
        pool = self.get_pool_by_name(pool_name)
        if not pool:
            return None
        
        volume_path = Path(pool["path"]) / volume_name
        
        try:
            if not volume_path.exists():
                return None
            
            stat_info = volume_path.stat()
            size_gb = self._get_dir_size(volume_path) / (1024 ** 3)
            created_timestamp = int(stat_info.st_ctime)
            permissions = oct(stat_info.st_mode)[-3:]
            backups = self._find_backups(volume_name)
            locked = self.task_queue.is_volume_locked(pool_name, volume_name)
            
            return {
                "name": volume_name,
                "pool": pool_name,
                "size_gb": round(size_gb, 2),
                "created_timestamp": created_timestamp,
                "backups": backups,
                "permissions": permissions,
                "locked": locked
            }
        except Exception as e:
            print(f"[ERROR] Failed to get volume detail: {e}", flush=True)
            return None
    
    def _get_dir_size(self, path: Path) -> int:
        """Calculate directory size in bytes."""
        total_size = 0
        try:
            for entry in path.rglob('*'):
                if entry.is_file():
                    total_size += entry.stat().st_size
        except Exception:
            pass
        return total_size
    
    def _find_backups(self, volume_name: str) -> List[str]:
        """Find backups for a volume across backup pools."""
        backups = []
        
        for backup_pool in self.config.backup_pools:
            backup_path = Path(backup_pool.path)
            if backup_path.exists():
                # Look for backup files/dirs related to this volume
                for backup_item in backup_path.iterdir():
                    if volume_name in backup_item.name:
                        backups.append(backup_item.name)
        
        return backups


# Global volume service instance
_volume_service = None


def get_volume_service(config=None) -> VolumeService:
    """Get volume service instance."""
    global _volume_service
    if _volume_service is None and config:
        _volume_service = VolumeService(config)
    return _volume_service
