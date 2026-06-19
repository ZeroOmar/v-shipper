"""Volume management service."""

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Any, List, Dict, Optional
from app.models import VolumeInfo, PoolStats
from app.services.task_queue import get_task_queue
from app.services.remote_api_client import RemoteApiClient, RemoteApiError, client_for_pool
from app.validation import validate_name, safe_join


class VolumeService:
    """Service for managing volumes in pools."""
    
    def __init__(self, config):
        self.config = config
        self.task_queue = get_task_queue()
        self.size_cache: Dict[str, Dict[str, float]] = {}
        self.size_lock = Lock()
        self.size_workers: Dict[str, Thread] = {}
    
    def get_pool_by_name(self, pool_name: str) -> Optional[Dict]:
        """Get pool configuration by name."""
        for host in self.config.docker_hosts:
            if host.name == pool_name:
                return {
                    "name": host.name,
                    "path": host.pool,
                    "type": host.pool_type,
                    "pool_type": host.pool_type,
                    "remote_host": host.remote_host,
                    "rsync_module": host.rsync_module,
                    "api_host": host.api_host,
                    "api_key": host.api_key,
                    "role": "docker",
                }

        for backup in self.config.backup_pools:
            if backup.name == pool_name:
                return {
                    "name": backup.name,
                    "path": backup.pool,
                    "type": "backup",
                    "pool_type": backup.pool_type,
                    "remote_host": backup.remote_host,
                    "rsync_module": backup.rsync_module,
                    "api_host": backup.api_host,
                    "api_key": backup.api_key,
                    "role": "backup",
                }

        return None
    
    def get_backup_pool_by_name(self, pool_name: str) -> Optional[Dict]:
        """Get backup pool configuration by name."""
        for backup in self.config.backup_pools:
            if backup.name == pool_name:
                return {
                    "name": backup.name,
                    "path": backup.pool,
                    "pool_type": backup.pool_type,
                    "remote_host": backup.remote_host,
                    "rsync_module": backup.rsync_module,
                    "api_host": backup.api_host,
                    "api_key": backup.api_key,
                    "role": "backup",
                }
        return None

    def _is_remote_pool(self, pool: Dict) -> bool:
        return pool.get("pool_type") == "remote"

    def _build_rsync_target(self, pool: Dict, volume_name: str = "", trailing_slash: bool = True) -> str:
        """Build an rsync daemon target path for a remote pool."""
        remote_host = pool.get("remote_host")
        rsync_module = pool.get("rsync_module")

        if not remote_host or not rsync_module:
            raise ValueError(f"Remote pool {pool.get('name')} missing remote_host or rsync_module")

        target = f"rsync://{remote_host}/{rsync_module}"
        if volume_name:
            # Reject names that could inject rsync filter/path syntax.
            volume_name = validate_name(volume_name, "volume_name")
            target = f"{target}/{volume_name}"
        if trailing_slash:
            target = f"{target.rstrip('/')}/"
        return target

    def _run_rsync_list(self, target: str, recursive: bool = False) -> tuple[bool, str, str]:
        """Run rsync --list-only for a remote target."""
        try:
            cmd = ["rsync", "--list-only"]
            if recursive:
                cmd.append("-r")
            cmd.append(target)
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(timeout=60)
            success = process.returncode == 0
            return success, stdout, stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "", "rsync list operation timed out"
        except Exception as e:
            return False, "", str(e)

    def _parse_rsync_list_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse a single rsync --list-only line into metadata."""
        parts = line.split()
        if len(parts) < 5:
            return None

        # Example line: drwxr-xr-x        4096 2026/06/08 11:11:09 dirname/
        try:
            mode = parts[0]
            size = int(parts[1].replace(',', ''))
            name = parts[-1]
            # Use mode string as primary check — trailing slash on dirs is unreliable
            # across rsync versions and NAS rsync daemons
            is_dir = mode[0] == 'd' if mode else name.endswith('/')
            ts = None
            if len(parts) >= 4:
                try:
                    ts = int(datetime.strptime(f"{parts[2]} {parts[3]}", "%Y/%m/%d %H:%M:%S").timestamp())
                except (ValueError, IndexError):
                    ts = None
            return {"name": name, "size": size, "is_dir": is_dir, "mode": mode, "created_timestamp": ts}
        except ValueError:
            return None

    def _list_remote_volumes(self, pool: Dict) -> tuple[List[VolumeInfo], List[str]]:
        """List volumes for a remote pool, preferring the v-helper API when available."""
        pool_name = pool.get("name", "")
        warnings = []

        api = client_for_pool(pool)
        if api:
            try:
                entries = api.ls()
                target = self._build_rsync_target(pool)
                volumes = []
                missing_sizes = []
                for entry in entries:
                    if not entry.get("is_dir"):
                        continue
                    volume_name = entry["name"].rstrip("/")
                    if not volume_name or volume_name == ".":
                        continue
                    cached_bytes = self._get_cached_size(pool_name, volume_name)
                    if cached_bytes is None:
                        size_loading = True
                        size_bytes = 0
                        missing_sizes.append(volume_name)
                    else:
                        size_loading = False
                        size_bytes = cached_bytes
                    mtime = entry.get("mtime_epoch")
                    volumes.append(VolumeInfo(
                        name=volume_name,
                        path=f"{target}{volume_name}/",
                        size_gb=size_bytes / (1024 ** 3),
                        size_bytes=size_bytes,
                        size_loading=size_loading,
                        created_timestamp=int(mtime) if mtime else None,
                        backups=self._find_backups(volume_name),
                    ))
                if missing_sizes:
                    self._start_remote_volume_size_refresh(pool_name, pool, missing_sizes)
                return sorted(volumes, key=lambda v: v.name), warnings
            except RemoteApiError as exc:
                warnings.append(f"v-helper API unavailable, falling back to rsync: {exc}")

        # Fallback: rsync daemon listing
        try:
            target = self._build_rsync_target(pool)
            success, stdout, stderr = self._run_rsync_list(target)
            if not success:
                warnings.append(f"Remote pool unreachable: {stderr}")
                return [], warnings

            volumes = []
            missing_sizes = []
            for line in stdout.splitlines():
                parsed = self._parse_rsync_list_line(line)
                if not parsed or not parsed["is_dir"]:
                    continue

                volume_name = parsed["name"].rstrip("/")
                if not volume_name or volume_name == ".":
                    continue
                if "/" in volume_name:
                    continue

                cached_bytes = self._get_cached_size(pool_name, volume_name)
                if cached_bytes is None:
                    size_loading = True
                    size_bytes = 0
                    missing_sizes.append(volume_name)
                else:
                    size_loading = False
                    size_bytes = cached_bytes

                volumes.append(VolumeInfo(
                    name=volume_name,
                    path=f"{target}{volume_name}/",
                    size_gb=size_bytes / (1024 ** 3),
                    size_bytes=size_bytes,
                    size_loading=size_loading,
                    created_timestamp=parsed.get("created_timestamp"),
                    backups=self._find_backups(volume_name),
                ))

            if missing_sizes:
                self._start_remote_volume_size_refresh(pool_name, pool, missing_sizes)

            return sorted(volumes, key=lambda v: v.name), warnings
        except Exception as e:
            warnings.append(f"Remote list failed: {e}")
            return [], warnings

    def _list_remote_backups(self, pool: Dict) -> tuple[List[VolumeInfo], List[str]]:
        """List backup archives for a remote backup pool."""
        warnings = []
        try:
            target = self._build_rsync_target(pool)
            success, stdout, stderr = self._run_rsync_list(target)
            if not success:
                warnings.append(f"Remote backup pool unreachable: {stderr}")
                return [], warnings

            volumes = []
            for line in stdout.splitlines():
                parsed = self._parse_rsync_list_line(line)
                if not parsed or parsed["is_dir"]:
                    continue

                file_name = parsed["name"]
                if not file_name.endswith((".tar.gz", ".tgz")):
                    continue

                volumes.append(VolumeInfo(
                    name=file_name,
                    path=f"{target}{file_name}",
                    size_gb=parsed["size"] / (1024 ** 3),
                    size_bytes=parsed["size"],
                    size_loading=False,
                    created_timestamp=parsed.get("created_timestamp"),
                    backups=[]
                ))
            return sorted(volumes, key=lambda v: v.name), warnings
        except Exception as e:
            warnings.append(f"Remote backup list failed: {e}")
            return [], warnings

    def _get_remote_size(self, pool: Dict, volume_name: str) -> Optional[int]:
        """Get total size of a remote volume or file via rsync listing."""
        try:
            target = self._build_rsync_target(pool, volume_name, trailing_slash=True)
            success, stdout, stderr = self._run_rsync_list(target, recursive=True)
            if not success:
                return None

            total_size = 0
            for line in stdout.splitlines():
                parsed = self._parse_rsync_list_line(line)
                if not parsed:
                    continue
                if parsed["is_dir"]:
                    continue
                total_size += parsed["size"]
            return total_size
        except Exception:
            return None

    def _get_remote_pool_total_size(self, pool: Dict) -> int:
        """Calculate total size of all files in a remote pool (recursive)."""
        try:
            target = self._build_rsync_target(pool)
            success, stdout, stderr = self._run_rsync_list(target, recursive=True)
            if not success:
                return 0

            total_size = 0
            for line in stdout.splitlines():
                parsed = self._parse_rsync_list_line(line)
                if not parsed or parsed["is_dir"]:
                    continue
                total_size += parsed["size"]
            return total_size
        except Exception:
            return 0

    def get_pool_stats(self, pool: Dict) -> PoolStats:
        """Get disk usage statistics for a pool."""
        pool_path = pool.get("path") or pool.get("pool")
        
        try:
            if pool.get("pool_type") == "remote":
                reachable = True
                error = None
                total_bytes = used_bytes = free_bytes = 0
                has_helper = bool(pool.get("api_host"))

                api = client_for_pool(pool)
                if api:
                    try:
                        disk = api.disk()
                        total_bytes = disk["total_bytes"]
                        used_bytes = disk["used_bytes"]
                        free_bytes = disk["free_bytes"]
                    except RemoteApiError as exc:
                        # API unreachable — fall back to rsync size estimation
                        has_helper = False
                        error = str(exc)
                        try:
                            target = self._build_rsync_target(pool)
                            success, _, stderr = self._run_rsync_list(target)
                            if not success:
                                reachable = False
                                error = stderr
                            else:
                                used_bytes = self._get_remote_pool_total_size(pool)
                                total_bytes = used_bytes
                        except Exception as exc2:
                            reachable = False
                            error = str(exc2)
                else:
                    try:
                        target = self._build_rsync_target(pool)
                        success, _, stderr = self._run_rsync_list(target)
                        if not success:
                            reachable = False
                            error = stderr
                        else:
                            used_bytes = self._get_remote_pool_total_size(pool)
                            total_bytes = used_bytes
                    except Exception as exc:
                        reachable = False
                        error = str(exc)

                total_gb = total_bytes / (1024 ** 3)
                used_gb = used_bytes / (1024 ** 3)
                free_gb = free_bytes / (1024 ** 3)
                usage_pct = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0
                return PoolStats(
                    name=pool["name"],
                    pool_type=pool.get("pool_type", "remote"),
                    role=pool.get("role", "docker"),
                    total_gb=round(total_gb, 2),
                    used_gb=round(used_gb, 2),
                    available_gb=round(free_gb, 2),
                    usage_percent=round(usage_pct, 2),
                    reachable=reachable,
                    has_helper=has_helper,
                    error=error,
                )

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
                pool_type=pool.get("pool_type", pool.get("type", "unknown")),
                role=pool.get("role", "docker"),
                total_gb=round(total_gb, 2),
                used_gb=round(used_gb, 2),
                available_gb=round(available_gb, 2),
                usage_percent=round(usage_percent, 2),
                reachable=True,
                error=None
            )
        except Exception as e:
            print(f"[ERROR] Failed to get pool stats for {pool['name']}: {e}", flush=True)
            return PoolStats(
                name=pool["name"],
                pool_type=pool.get("type", "unknown"),
                role=pool.get("role", "docker"),
                total_gb=0,
                used_gb=0,
                available_gb=0,
                usage_percent=0,
                reachable=False,
                error=str(e)
            )
    
    def list_volumes(self, pool_name: str) -> (List[VolumeInfo], List[str]):
        """List all volumes in a pool."""
        pool = self.get_pool_by_name(pool_name)
        
        if not pool:
            print(f"[ERROR] Pool {pool_name} not found", flush=True)
            return [], []

        if self._is_remote_pool(pool):
            if pool.get("role") == "backup":
                return self._list_remote_backups(pool)
            return self._list_remote_volumes(pool)

        pool_path = Path(pool["path"])
        volumes = []
        missing_sizes = []
        warnings = []
        
        try:
            if not pool_path.exists():
                print(f"[WARNING] Pool path {pool_path} does not exist", flush=True)
                return [], []
            
            for volume_item in pool_path.iterdir():
                if volume_item.name.startswith('.'):
                    continue

                if pool.get("type") == "backup":
                    if volume_item.is_file() and volume_item.name.endswith((".tar.gz", ".tgz")):
                        size_bytes = volume_item.stat().st_size
                        size_gb = size_bytes / (1024 ** 3)
                        stat_info = volume_item.stat()
                        created_timestamp = int(stat_info.st_ctime)
                        volumes.append(VolumeInfo(
                            name=volume_item.name,
                            path=str(volume_item),
                            size_gb=size_gb,
                            size_bytes=size_bytes,
                            size_loading=False,
                            created_timestamp=created_timestamp,
                            backups=[]
                        ))
                    else:
                        warnings.append(f"Ignored unsupported backup item: {volume_item.name}")
                else:
                    if volume_item.is_dir():
                        size_loading = False
                        size_bytes = 0
                        cached_bytes = self._get_cached_size(pool_name, volume_item.name)
                        if cached_bytes is None:
                            size_loading = True
                            missing_sizes.append(volume_item.name)
                        else:
                            size_bytes = cached_bytes

                        size_gb = size_bytes / (1024 ** 3)
                        stat_info = volume_item.stat()
                        created_timestamp = int(stat_info.st_ctime)
                        backups = self._find_backups(volume_item.name)

                        volumes.append(VolumeInfo(
                            name=volume_item.name,
                            path=str(volume_item),
                            size_gb=size_gb,
                            size_bytes=size_bytes,
                            size_loading=size_loading,
                            created_timestamp=created_timestamp,
                            backups=backups
                        ))
                    else:
                        warnings.append(f"Ignored non-volume item: {volume_item.name}")
        except Exception as e:
            print(f"[ERROR] Failed to list volumes in {pool_name}: {e}", flush=True)

        if missing_sizes:
            self._start_volume_size_refresh(pool_name, pool_path, missing_sizes)
        
        return sorted(volumes, key=lambda v: v.name), warnings
    
    def rename_volume(self, pool_name: str, old_name: str, new_name: str) -> bool:
        """Rename a volume."""
        pool = self.get_pool_by_name(pool_name)
        if not pool:
            return False

        if self._is_remote_pool(pool):
            api = client_for_pool(pool)
            if not api:
                print(f"[ERROR] Cannot rename volume in remote pool '{pool_name}' — no v-helper api_host configured", flush=True)
                return False
            try:
                api.rename(old_name, new_name)
                print(f"[INFO] Renamed remote volume {old_name} → {new_name} in {pool_name}", flush=True)
                with self.size_lock:
                    pool_sizes = self.size_cache.get(pool_name, {})
                    if old_name in pool_sizes:
                        pool_sizes[new_name] = pool_sizes.pop(old_name)
                return True
            except RemoteApiError as exc:
                print(f"[ERROR] Remote rename failed: {exc}", flush=True)
                return False

        try:
            old_path = safe_join(pool["path"], old_name)
            new_path = safe_join(pool["path"], new_name)
        except ValueError as e:
            print(f"[ERROR] Path traversal attempt in rename: {e}", flush=True)
            return False

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
    
    def create_volume(self, pool_name: str, volume_name: str) -> bool:
        """Create a new volume directory."""
        pool = self.get_pool_by_name(pool_name)
        if not pool:
            return False

        if self._is_remote_pool(pool):
            api = client_for_pool(pool)
            if not api:
                print(f"[ERROR] Cannot create volume in remote pool '{pool_name}' — no v-helper api_host configured", flush=True)
                return False
            try:
                api.mkdir(volume_name)
                print(f"[INFO] Created remote volume '{volume_name}' in '{pool_name}'", flush=True)
                return True
            except RemoteApiError as exc:
                print(f"[ERROR] Remote mkdir failed: {exc}", flush=True)
                return False

        try:
            new_path = safe_join(pool["path"], volume_name)
        except ValueError as e:
            print(f"[ERROR] Path traversal attempt in create_volume: {e}", flush=True)
            return False

        try:
            if new_path.exists():
                print(f"[ERROR] Volume '{volume_name}' already exists in '{pool_name}'", flush=True)
                return False
            new_path.mkdir(mode=0o777, parents=False, exist_ok=False)
            print(f"[INFO] Created volume '{volume_name}' in '{pool_name}'", flush=True)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to create volume '{volume_name}': {e}", flush=True)
            return False

    def delete_volume(self, pool_name: str, volume_name: str, task_id: str = None) -> bool:
        """Delete a volume or backup file."""
        pool = self.get_pool_by_name(pool_name)
        if not pool:
            return False

        def _log(level: str, msg: str):
            prefix = f"[TASK:{task_id}] " if task_id else ""
            print(f"{prefix}[{level}] {msg}", flush=True)

        # Reject names that could inject rsync filter syntax or path traversal.
        try:
            volume_name = validate_name(volume_name, "volume_name")
        except ValueError as e:
            _log("ERROR", f"Invalid volume name in delete: {e}")
            return False

        # Handle remote pools
        if self._is_remote_pool(pool):
            api = client_for_pool(pool)
            if api:
                try:
                    api.rm(volume_name)
                    _log("INFO", f"Deleted {volume_name} from remote pool {pool_name}")
                    with self.size_lock:
                        self.size_cache.get(pool_name, {}).pop(volume_name, None)
                    return True
                except RemoteApiError as exc:
                    _log("ERROR", f"Remote delete failed: {exc}")
                    return False

            # Fallback: rsync workaround (no v-helper). Sync an empty dir to the module
            # root with include/exclude filters so --delete targets only this volume.
            try:
                module_target = self._build_rsync_target(pool, trailing_slash=True)
                with tempfile.TemporaryDirectory() as empty_dir:
                    process = subprocess.Popen(
                        [
                            "rsync", "-r", "--delete", "--force",
                            "--include", f"/{volume_name}/",
                            "--include", f"/{volume_name}/**",
                            "--exclude", "*",
                            empty_dir + "/", module_target
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    stdout, stderr = process.communicate(timeout=60)

                if process.returncode == 0:
                    _log("INFO", f"Deleted {volume_name} from remote pool {pool_name}")
                    with self.size_lock:
                        self.size_cache.get(pool_name, {}).pop(volume_name, None)
                    return True
                else:
                    for line in stderr.strip().splitlines():
                        _log("WARNING", f"rsync: {line}")
                    return False
            except subprocess.TimeoutExpired:
                _log("ERROR", f"Delete operation timed out for {volume_name}")
                return False
            except Exception as e:
                _log("ERROR", f"Failed to delete remote volume: {e}")
                return False
        
        # Handle local pools normally
        try:
            volume_path = safe_join(pool["path"], volume_name)
        except ValueError as e:
            print(f"[ERROR] Path traversal attempt in delete: {e}", flush=True)
            return False

        try:
            if not volume_path.exists():
                print(f"[ERROR] Volume {volume_name} not found", flush=True)
                return False

            if volume_path.is_dir():
                shutil.rmtree(volume_path)
            else:
                volume_path.unlink()

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

        try:
            if self._is_remote_pool(pool):
                size_bytes = self._get_remote_size(pool, volume_name) or 0
                created_timestamp = None
                permissions = None
                backups = [] if pool.get("role") == "backup" else self._find_backups(volume_name)
                locked = self.task_queue.is_volume_locked(pool_name, volume_name)

                return {
                    "name": volume_name,
                    "pool": pool_name,
                    "size_gb": size_bytes / (1024 ** 3),
                    "size_bytes": size_bytes,
                    "created_timestamp": created_timestamp,
                    "backups": backups,
                    "permissions": permissions,
                    "locked": locked
                }

            volume_path = Path(pool["path"]) / volume_name
            if not volume_path.exists():
                return None
            
            stat_info = volume_path.stat()
            if volume_path.is_dir():
                size_bytes = self._get_dir_size(volume_path)
            else:
                size_bytes = volume_path.stat().st_size
            size_gb = size_bytes / (1024 ** 3)
            created_timestamp = int(stat_info.st_ctime)
            permissions = oct(stat_info.st_mode)[-3:]
            backups = [] if pool.get("type") == "backup" else self._find_backups(volume_name)
            locked = self.task_queue.is_volume_locked(pool_name, volume_name)
            
            return {
                "name": volume_name,
                "pool": pool_name,
                "size_gb": size_gb,
                "size_bytes": size_bytes,
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
            if backup_pool.pool_type == "remote":
                remote_pool = {
                    "name": backup_pool.name,
                    "path": backup_pool.pool,
                    "pool_type": backup_pool.pool_type,
                    "remote_host": backup_pool.remote_host,
                    "rsync_module": backup_pool.rsync_module,
                    "role": "backup"
                }
                try:
                    success, stdout, stderr = self._run_rsync_list(self._build_rsync_target(remote_pool))
                    if not success:
                        continue
                    for line in stdout.splitlines():
                        parsed = self._parse_rsync_list_line(line)
                        if not parsed or parsed["is_dir"]:
                            continue
                        if volume_name in parsed["name"]:
                            backups.append(parsed["name"])
                except Exception:
                    continue
            else:
                backup_path = Path(backup_pool.pool)
                if backup_path.exists():
                    # Look for backup files/dirs related to this volume
                    for backup_item in backup_path.iterdir():
                        if volume_name in backup_item.name:
                            backups.append(backup_item.name)
        
        return backups

    def _get_cached_size(self, pool_name: str, volume_name: str) -> Optional[int]:
        """Return cached size in bytes for a volume."""
        with self.size_lock:
            return self.size_cache.get(pool_name, {}).get(volume_name)

    def _cache_volume_size(self, pool_name: str, volume_name: str, size_bytes: int):
        """Cache calculated volume size in bytes."""
        with self.size_lock:
            if pool_name not in self.size_cache:
                self.size_cache[pool_name] = {}
            self.size_cache[pool_name][volume_name] = size_bytes

    def _start_volume_size_refresh(self, pool_name: str, pool_path: Path, volume_names: List[str]):
        """Start a background thread to compute missing volume sizes."""
        with self.size_lock:
            existing_worker = self.size_workers.get(pool_name)
            if existing_worker and existing_worker.is_alive():
                return

            def worker():
                self._refresh_volume_sizes(pool_name, pool_path, volume_names)

            thread = Thread(target=worker, daemon=True)
            self.size_workers[pool_name] = thread
            thread.start()

    def _start_remote_volume_size_refresh(self, pool_name: str, pool: Dict, volume_names: List[str]):
        """Start a background thread to compute sizes for remote docker host volumes via rsync."""
        with self.size_lock:
            existing_worker = self.size_workers.get(pool_name)
            if existing_worker and existing_worker.is_alive():
                return

            def worker():
                for volume_name in volume_names:
                    try:
                        size_bytes = self._get_remote_size(pool, volume_name) or 0
                    except Exception as e:
                        print(f"[WARNING] Failed to get remote size for {volume_name}: {e}", flush=True)
                        size_bytes = 0
                    self._cache_volume_size(pool_name, volume_name, size_bytes)

            thread = Thread(target=worker, daemon=True)
            self.size_workers[pool_name] = thread
            thread.start()

    def _refresh_volume_sizes(self, pool_name: str, pool_path: Path, volume_names: List[str]):
        """Compute sizes for a list of volumes in the background."""
        for volume_name in volume_names:
            volume_path = pool_path / volume_name
            try:
                if not volume_path.exists():
                    continue

                if volume_path.is_dir():
                    size_bytes = self._get_dir_size(volume_path)
                else:
                    size_bytes = volume_path.stat().st_size

                self._cache_volume_size(pool_name, volume_name, size_bytes)
            except Exception as e:
                print(f"[WARNING] Failed to refresh size for {volume_name}: {e}", flush=True)


# Global volume service instance
_volume_service = None


def get_volume_service(config=None) -> VolumeService:
    """Get volume service instance."""
    global _volume_service
    if _volume_service is None and config:
        _volume_service = VolumeService(config)
    return _volume_service
