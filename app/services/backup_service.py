"""Backup service for volume backups."""

import subprocess
import time
from pathlib import Path
from app.services.task_queue import get_task_queue


class BackupService:
    """Service for managing volume backups."""
    
    def __init__(self, config, volume_service):
        self.config = config
        self.volume_service = volume_service
        self.task_queue = get_task_queue()
    
    def backup_volume(self, task_id: str, source_pool_name: str, source_volume_name: str,
                     backup_pool_name: str, verify: bool = True) -> bool:
        """Backup a volume to a backup pool."""
        
        # Get pool configurations
        source_pool = self.volume_service.get_pool_by_name(source_pool_name)
        backup_pool = self.volume_service.get_backup_pool_by_name(backup_pool_name)
        
        if not source_pool or not backup_pool:
            error_msg = "Source pool or backup pool not found"
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False
        
        # Create lock file
        lock_file = self.task_queue.create_lockfile(source_pool_name, source_volume_name)
        
        try:
            self.task_queue.start_task(task_id)
            
            source_path = f"{source_pool['path']}/{source_volume_name}"
            backup_path = f"{backup_pool['path']}/{source_volume_name}_backup_{int(time.time())}.tar.gz"
            
            # Update progress
            self.task_queue.update_progress(task_id, {
                "current_operation": f"Archiving {source_volume_name}",
                "progress_percent": 10
            })
            
            # Create backup archive
            if not self._create_archive(task_id, source_path, backup_path):
                error_msg = "Archive creation failed"
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
                return False
            
            self.task_queue.update_progress(task_id, {
                "current_operation": "Verifying backup",
                "progress_percent": 85
            })
            
            # Verify if requested
            if verify:
                if not self._verify_backup(backup_path):
                    error_msg = "Backup verification failed"
                    self.task_queue.complete_task(task_id, success=False, error=error_msg)
                    return False
            
            self.task_queue.update_progress(task_id, {
                "current_operation": "Backup complete",
                "progress_percent": 100
            })
            
            self.task_queue.complete_task(task_id, success=True)
            return True
        
        except Exception as e:
            error_msg = f"Backup error: {str(e)}"
            print(f"[ERROR] Backup failed: {e}", flush=True)
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False
        
        finally:
            self.task_queue.remove_lockfile(lock_file)
    
    def _create_archive(self, task_id: str, source_path: str, backup_path: str) -> bool:
        """Create tar.gz archive of volume."""
        
        try:
            print(f"[TASK:{task_id}] Creating archive: {backup_path}", flush=True)
            
            # Run tar command
            cmd = f"tar -czf {backup_path} -C {Path(source_path).parent} {Path(source_path).name}"
            
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            return_code = process.wait()
            
            if return_code != 0:
                stderr = process.stderr.read()
                print(f"[ERROR] Archive creation failed: {stderr}", flush=True)
                return False
            
            print(f"[TASK:{task_id}] Archive created successfully", flush=True)
            return True
        
        except Exception as e:
            print(f"[ERROR] Archive creation error: {e}", flush=True)
            return False
    
    def _verify_backup(self, backup_path: str) -> bool:
        """Verify backup archive integrity."""
        
        try:
            # Verify tar file integrity
            cmd = f"tar -tzf {backup_path} > /dev/null 2>&1"
            result = subprocess.run(cmd, shell=True)
            
            if result.returncode != 0:
                print(f"[ERROR] Backup archive is corrupt", flush=True)
                return False
            
            # Get archive size
            archive_size = Path(backup_path).stat().st_size / (1024 ** 2)  # Convert to MB
            print(f"[INFO] Backup verified: {archive_size:.2f} MB", flush=True)
            return True
        
        except Exception as e:
            print(f"[ERROR] Backup verification error: {e}", flush=True)
            return False


# Global backup service instance
_backup_service = None


def get_backup_service(config=None, volume_service=None) -> BackupService:
    """Get backup service instance."""
    global _backup_service
    if _backup_service is None and config and volume_service:
        _backup_service = BackupService(config, volume_service)
    return _backup_service
