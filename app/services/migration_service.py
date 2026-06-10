"""Migration service for volume migrations."""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from app.services.task_queue import get_task_queue


class MigrationService:
    """Service for managing volume migrations."""
    
    def __init__(self, config, volume_service):
        self.config = config
        self.volume_service = volume_service
        self.task_queue = get_task_queue()
    
    def migrate_volume(self, task_id: str, source_pool_name: str, source_volume_name: str,
                      dest_pool_name: str, verify: bool = True, delete_source: bool = False) -> bool:
        """Migrate volume from source to destination pool."""
        
        # Get pool configurations
        source_pool = self.volume_service.get_pool_by_name(source_pool_name)
        dest_pool = self.volume_service.get_pool_by_name(dest_pool_name)
        
        if not source_pool or not dest_pool:
            error_msg = "Source or destination pool not found"
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False
        
        # Create lock file
        lock_file = self.task_queue.create_lockfile(source_pool_name, source_volume_name)
        
        try:
            self.task_queue.start_task(task_id)
            
            source_path = f"{source_pool['path']}/{source_volume_name}/"
            dest_path = f"{dest_pool['path']}/{source_volume_name}"

            # Prevent overwriting an existing destination volume
            if Path(dest_pool['path']).joinpath(source_volume_name).exists():
                error_msg = "Destination volume already exists"
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
                return False

            # Update progress
            self.task_queue.update_progress(task_id, {
                "current_operation": f"Migrating {source_volume_name} from {source_pool_name} to {dest_pool_name}",
                "progress_percent": 10
            })
            
            # Execute rsync
            rsync_success, rsync_error = self._rsync_volume(task_id, source_path, dest_path, source_pool, dest_pool)
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
                if not self._verify_migration(source_path, dest_path):
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
                     source_pool: dict, dest_pool: dict) -> tuple[bool, str]:
        
        try:
            # Build rsync command with permission preservation
            rsync_cmd = [
                "rsync",
                "-av",
                "--perms",
                "--group",
                "--owner",
                "--progress",
                "--no-whole-file",
                "--inplace"
            ]
            
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

    def _verify_migration(self, source_path: str, dest_path: str) -> bool:
        """Verify that migration was successful."""
        
        try:
            result = subprocess.run(["find", source_path, "-type", "f"], capture_output=True, text=True)
            source_count = len(result.stdout.splitlines()) if result.returncode == 0 else -1

            result = subprocess.run(["find", dest_path, "-type", "f"], capture_output=True, text=True)
            dest_count = len(result.stdout.splitlines()) if result.returncode == 0 else -1
            
            if source_count != dest_count or source_count < 0:
                print(f"[ERROR] Verification failed: source={source_count} files, dest={dest_count} files", flush=True)
                return False
            
            print(f"[INFO] Verification passed: {source_count} files found in both locations", flush=True)
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
