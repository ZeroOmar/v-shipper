"""Migration service for volume migrations."""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from app.services.task_queue import get_task_queue
from app.services.remote_api_client import client_for_pool, RemoteApiError
from app.validation import safe_join


class MigrationService:
    """Service for managing volume migrations."""
    
    def __init__(self, config, volume_service):
        self.config = config
        self.volume_service = volume_service
        self.task_queue = get_task_queue()
    
    def migrate_volume(self, task_id: str, source_pool_name: str, source_volume_name: str,
                      dest_pool_name: str, verify: bool = True, delete_source: bool = False,
                      conflict_resolution: Optional[str] = None, rename_dest: Optional[str] = None) -> bool:
        """Migrate volume from source to destination pool."""

        source_pool = self.volume_service.get_pool_by_name(source_pool_name)
        dest_pool = self.volume_service.get_pool_by_name(dest_pool_name)

        if not source_pool or not dest_pool:
            error_msg = "Source or destination pool not found"
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False

        # Effective destination volume name (may differ from source when renaming)
        effective_dest = rename_dest if conflict_resolution == 'rename' and rename_dest else source_volume_name

        lock_file = self.task_queue.create_lockfile(source_pool_name, source_volume_name)

        try:
            self.task_queue.start_task(task_id)

            if source_pool.get("pool_type") == "remote":
                source_path = self.volume_service._build_rsync_target(
                    source_pool, source_volume_name, trailing_slash=True
                )
            else:
                source_path = str(safe_join(source_pool['path'], source_volume_name)) + "/"

            if dest_pool.get("pool_type") == "remote":
                dest_path = self.volume_service._build_rsync_target(
                    dest_pool, effective_dest, trailing_slash=False
                )
            else:
                dest_path = str(safe_join(dest_pool['path'], effective_dest))

            # Update progress
            self.task_queue.update_progress(task_id, {
                "current_operation": f"Migrating {source_volume_name} from {source_pool_name} to {dest_pool_name}",
                "progress_percent": 10
            })

            # Execute rsync (--delete when overwriting to make it a complete replacement)
            overwrite = conflict_resolution == 'overwrite'
            rsync_success, rsync_error = self._rsync_volume(
                task_id, source_path, dest_path, source_pool, dest_pool, overwrite=overwrite
            )
            if not rsync_success:
                error_msg = rsync_error or "Rsync failed"
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
                self._cleanup_partial_destination(dest_path, dest_pool)
                return False
            
            self.task_queue.update_progress(task_id, {
                "current_operation": "Verifying migration",
                "progress_percent": 85
            })
            
            # Verify if requested
            if verify:
                if not self._verify_migration(source_path, dest_path, source_pool, dest_pool):
                    error_msg = "Migration verification failed"
                    self.task_queue.complete_task(task_id, success=False, error=error_msg)
                    return False
            
            self.task_queue.update_progress(task_id, {
                "current_operation": "Migration complete",
                "progress_percent": 95
            })
            
            # Delete source if requested
            if delete_source:
                self.task_queue.update_progress(task_id, {
                    "current_operation": "Deleting source volume",
                    "progress_percent": 98
                })
                self.volume_service.delete_volume(source_pool_name, source_volume_name)
            
            self.task_queue.complete_task(task_id, success=True)
            return True
        
        except Exception as e:
            error_msg = f"Migration error: {str(e)}"
            print(f"[ERROR] Migration failed: {e}", flush=True)
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False
        
        finally:
            self.task_queue.remove_lockfile(lock_file)
    
    def _rsync_volume(self, task_id: str, source_path: str, dest_path: str,
                     source_pool: dict, dest_pool: dict, overwrite: bool = False) -> tuple[bool, str]:

        try:
            rsync_cmd = [
                "rsync",
                "-av",
                "--perms",
                "--group",
                "--owner",
                "--progress",
                "--no-whole-file",
                "--inplace",
            ]
            if overwrite:
                rsync_cmd.append("--delete")

            rsync_cmd.extend([source_path, dest_path])
            
            print(f"[TASK:{task_id}] Running rsync: {' '.join(rsync_cmd)}", flush=True)
            
            # Run rsync
            process = subprocess.Popen(
                rsync_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Monitor progress
            for line in process.stdout:
                line = line.strip()
                if line:
                    print(f"[TASK:{task_id}] {line}", flush=True)
                    
                    # Extract progress percentage if available
                    if "%" in line:
                        try:
                            percent = int(line.split("%")[0].split()[-1])
                            self.task_queue.update_progress(task_id, {
                                "progress_percent": min(80, 10 + int(percent * 0.7)),
                                "current_operation": line
                            })
                        except (ValueError, IndexError):
                            pass
            
            return_code = process.wait()
            
            if return_code != 0:
                stderr = process.stderr.read()
                error_msg = f"Rsync failed with code {return_code}: {stderr.strip()}"
                print(f"[ERROR] {error_msg}", flush=True)
                return False, error_msg
            
            return True, ""
        
        except Exception as e:
            error_msg = f"Rsync execution failed: {e}"
            print(f"[ERROR] {error_msg}", flush=True)
            return False, error_msg

    def _cleanup_partial_destination(self, dest_path: str, dest_pool: dict):
        """Remove partially copied destination data on failure."""
        try:
            dest = Path(dest_path)
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
                print(f"[INFO] Cleaned up partial destination: {dest_path}", flush=True)
        except Exception as e:
            print(f"[ERROR] Failed to clean up partial destination {dest_path}: {e}", flush=True)

    def _get_remote_total_bytes(self, pool: dict, rsync_path: str) -> Optional[int]:
        """Return total byte size of a remote volume, using API when available."""
        api = client_for_pool(pool)
        if api:
            try:
                # Parse the volume name from the rsync path (last non-empty component)
                vol_name = rsync_path.rstrip("/").rsplit("/", 1)[-1]
                entries = api.ls()
                for entry in entries:
                    if entry["name"].rstrip("/") == vol_name and entry["is_dir"]:
                        # API ls gives stat size of dir entry, not recursive — fall through
                        break
                # API doesn't expose recursive byte counts; fall back to rsync listing
            except RemoteApiError:
                pass
        # rsync recursive listing: sum all file sizes
        success, stdout, _ = self.volume_service._run_rsync_list(rsync_path, recursive=True)
        if not success:
            return None
        total = 0
        for line in stdout.splitlines():
            p = self.volume_service._parse_rsync_list_line(line)
            if p and not p["is_dir"]:
                total += p["size"]
        return total

    def _verify_migration(self, source_path: str, dest_path: str,
                          source_pool: dict = None, dest_pool: dict = None) -> bool:
        """Verify that migration was successful by comparing byte totals."""
        try:
            if source_pool and source_pool.get("pool_type") == "remote":
                source_bytes = self._get_remote_total_bytes(source_pool, source_path)
                if source_bytes is None:
                    print("[ERROR] Verification failed: could not measure source bytes", flush=True)
                    return False
            else:
                result = subprocess.run(
                    ["du", "-sb", source_path], capture_output=True, text=True
                )
                source_bytes = int(result.stdout.split()[0]) if result.returncode == 0 else -1

            if dest_pool and dest_pool.get("pool_type") == "remote":
                dest_bytes = self._get_remote_total_bytes(dest_pool, dest_path.rstrip("/") + "/")
                if dest_bytes is None:
                    print("[ERROR] Verification failed: could not measure dest bytes", flush=True)
                    return False
            else:
                result = subprocess.run(
                    ["du", "-sb", dest_path], capture_output=True, text=True
                )
                dest_bytes = int(result.stdout.split()[0]) if result.returncode == 0 else -1

            if source_bytes < 0 or dest_bytes < 0 or source_bytes != dest_bytes:
                print(
                    f"[ERROR] Verification failed: source={source_bytes} bytes, dest={dest_bytes} bytes",
                    flush=True,
                )
                return False

            print(f"[INFO] Verification passed: {source_bytes} bytes in both locations", flush=True)
            return True

        except Exception as e:
            print(f"[ERROR] Verification error: {e}", flush=True)
            return False


# Global migration service instance
_migration_service = None


def get_migration_service(config=None, volume_service=None) -> MigrationService:
    """Get migration service instance."""
    global _migration_service
    if _migration_service is None and config and volume_service:
        _migration_service = MigrationService(config, volume_service)
    return _migration_service
