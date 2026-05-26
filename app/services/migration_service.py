"""Migration service for volume migrations."""

import subprocess
import time
from pathlib import Path
from typing import Optional
from app.services.task_queue import get_task_queue
from app.services.ssh_service import get_ssh_service


class MigrationService:
    """Service for managing volume migrations."""
    
    def __init__(self, config, volume_service):
        self.config = config
        self.volume_service = volume_service
        self.task_queue = get_task_queue()
        self.ssh_service = get_ssh_service()
    
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
            
            source_path = f"{source_pool['path']}/{source_volume_name}"
            dest_path = f"{dest_pool['path']}/{source_volume_name}"
            
            # Update progress
            self.task_queue.update_progress(task_id, {
                "current_operation": f"Migrating {source_volume_name} from {source_pool_name} to {dest_pool_name}",
                "progress_percent": 10
            })
            
            # Execute rsync
            if not self._rsync_volume(task_id, source_path, dest_path, source_pool, dest_pool):
                error_msg = "Rsync failed"
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
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
            self.ssh_service.close_all()
    
    def _rsync_volume(self, task_id: str, source_path: str, dest_path: str,
                     source_pool: dict, dest_pool: dict) -> bool:
        """Execute rsync between source and destination."""
        
        try:
            # Build rsync command with permission preservation
            rsync_cmd = [
                "rsync",
                "-av",
                "--perms",
                "--preserve-times",
                "--group",
                "--owner",
                "--progress",
                "--no-whole-file",
                "--inplace"
            ]
            
            # Handle remote pools via SSH
            if source_pool.get("type") == "remote":
                ssh_user = source_pool.get("ssh_user")
                ssh_key = source_pool.get("ssh_key")
                host = source_pool.get("ip")
                
                # Connect via SSH for verification of source path
                ssh_conn = self.ssh_service.connect(host, ssh_user, ssh_key)
                source_path = f"{ssh_user}@{host}:{source_path}"
            
            if dest_pool.get("type") == "remote":
                ssh_user = dest_pool.get("ssh_user")
                ssh_key = dest_pool.get("ssh_key")
                host = dest_pool.get("ip")
                
                ssh_conn = self.ssh_service.connect(host, ssh_user, ssh_key)
                dest_path = f"{ssh_user}@{host}:{dest_path}"
            
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
                print(f"[ERROR] Rsync failed with code {return_code}: {stderr}", flush=True)
                return False
            
            return True
        
        except Exception as e:
            print(f"[ERROR] Rsync execution failed: {e}", flush=True)
            return False
    
    def _verify_migration(self, source_path: str, dest_path: str) -> bool:
        """Verify that migration was successful."""
        
        try:
            # Simple check: verify both paths exist and have same file count
            source_files = set()
            dest_files = set()
            
            # Count files in source
            result = subprocess.run(
                f"find {source_path} -type f | wc -l",
                shell=True,
                capture_output=True,
                text=True
            )
            source_count = int(result.stdout.strip()) if result.returncode == 0 else -1
            
            # Count files in destination
            result = subprocess.run(
                f"find {dest_path} -type f | wc -l",
                shell=True,
                capture_output=True,
                text=True
            )
            dest_count = int(result.stdout.strip()) if result.returncode == 0 else -1
            
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
