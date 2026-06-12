"""Backup service for volume backups."""

import datetime
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
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
        remote_source_staging = None
        remote_staging_archive = None
        lock_file = self.task_queue.create_lockfile(source_pool_name, source_volume_name)

        try:
            self.task_queue.start_task(task_id)

            # For remote source pools, pull volume to a local staging dir before archiving
            if source_pool.get("pool_type") == "remote":
                remote_source_staging = Path(self.config.tmp_dir) / f".backup_stage_{task_id}"
                remote_source_staging.mkdir(parents=True, exist_ok=True)

                self.task_queue.update_progress(task_id, {
                    "current_operation": f"Pulling {source_volume_name} from remote pool",
                    "progress_percent": 5
                })

                remote_src = self.volume_service._build_rsync_target(
                    source_pool, source_volume_name, trailing_slash=False
                )
                ok = self._stream_rsync(
                    task_id,
                    ["rsync", "-avz", "--progress", remote_src, str(remote_source_staging) + "/"],
                    progress_range=(5, 30),
                    label=f"Pulling {source_volume_name} from remote",
                )
                if not ok:
                    error_msg = f"Failed to pull volume from remote pool"
                    self.task_queue.complete_task(task_id, success=False, error=error_msg)
                    return False

                source_path = str(remote_source_staging / source_volume_name)
                print(f"[TASK:{task_id}] Pulled remote volume to {source_path}", flush=True)
            else:
                source_path = f"{source_pool['path']}/{source_volume_name}"

            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            archive_name = f"{source_pool_name}_{source_volume_name}_{timestamp}.tar.gz"
            
            # For remote backup pools, create archive in staging directory first
            if backup_pool.get('pool_type') == 'remote':
                staging_dir = Path(self.config.staging_dir)
                staging_dir.mkdir(parents=True, exist_ok=True)
                remote_staging_archive = staging_dir / archive_name
                archive_path = str(remote_staging_archive)
                remote_backup_pool = backup_pool
            else:
                # For local backup pools, create directly in pool directory
                backup_path = backup_pool['path']
                if backup_path.endswith('/'):
                    archive_path = f"{backup_path}{archive_name}"
                else:
                    archive_path = f"{backup_path}/{archive_name}"
                remote_backup_pool = None
            
            # Update progress
            self.task_queue.update_progress(task_id, {
                "current_operation": f"Archiving {source_volume_name}",
                "progress_percent": 10
            })
            
            # Create backup archive
            if not self._create_archive(task_id, source_path, archive_path):
                error_msg = "Archive creation failed"
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
                return False
            
            # If remote backup pool, transfer archive via rsync
            if remote_backup_pool:
                self.task_queue.update_progress(task_id, {
                    "current_operation": f"Transferring backup to remote pool",
                    "progress_percent": 75
                })
                
                if not self._transfer_to_remote_backup(task_id, archive_path, remote_backup_pool, archive_name):
                    error_msg = "Failed to transfer backup to remote pool"
                    self.task_queue.complete_task(task_id, success=False, error=error_msg)
                    try:
                        Path(archive_path).unlink()
                    except:
                        pass
                    return False
            
            self.task_queue.update_progress(task_id, {
                "current_operation": "Verifying backup",
                "progress_percent": 85
            })
            
            # Verify if requested (verify local copy for remote backups)
            if verify:
                if not self._verify_backup(archive_path):
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
            if remote_source_staging and remote_source_staging.exists():
                shutil.rmtree(remote_source_staging, ignore_errors=True)
            if remote_staging_archive and remote_staging_archive.exists():
                remote_staging_archive.unlink(missing_ok=True)

    def _create_archive(self, task_id: str, source_path: str, backup_path: str) -> bool:
        """Create tar.gz archive of volume."""
        
        try:
            print(f"[TASK:{task_id}] Creating archive: {backup_path}", flush=True)
            
            process = subprocess.Popen(
                ["tar", "-czf", backup_path, "-C", str(Path(source_path).parent), Path(source_path).name],
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
            result = subprocess.run(["tar", "-tzf", backup_path], capture_output=True)
            
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
    def restore_backup(self, task_id: str, backup_pool_name: str, backup_file: str,
                       dest_pool_name: str, dest_volume_name: str,
                       conflict_resolution: Optional[str] = None) -> bool:
        """Restore a backup archive into a destination pool."""
        backup_pool = self.volume_service.get_backup_pool_by_name(backup_pool_name)
        dest_pool = self.volume_service.get_pool_by_name(dest_pool_name)

        if not backup_pool or not dest_pool:
            error_msg = "Backup pool or destination pool not found"
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False

        lock_file = self.task_queue.create_lockfile(dest_pool_name, dest_volume_name)
        pulled_backup_path = None
        restore_staging = None

        try:
            # Start immediately so progress is visible during the download phase
            self.task_queue.start_task(task_id)

            # ── Phase 1: Download backup file if source pool is remote ──────────
            if backup_pool.get('pool_type') == 'remote':
                staging_dir = Path(self.config.staging_dir)
                staging_dir.mkdir(parents=True, exist_ok=True)
                backup_path = staging_dir / backup_file
                pulled_backup_path = backup_path

                self.task_queue.update_progress(task_id, {
                    "current_operation": f"Downloading {backup_file} from remote pool",
                    "progress_percent": 5,
                })
                remote_target = self.volume_service._build_rsync_target(
                    backup_pool, backup_file, trailing_slash=False
                )
                ok = self._stream_rsync(
                    task_id,
                    ["rsync", "-avz", "--progress", remote_target, str(backup_path)],
                    progress_range=(5, 45),
                    label=f"Downloading {backup_file}",
                )
                if not ok:
                    self.task_queue.complete_task(
                        task_id, success=False,
                        error=f"Failed to download {backup_file} from remote pool"
                    )
                    return False
                print(f"[TASK:{task_id}] Download complete: {backup_path}", flush=True)
            else:
                backup_path = Path(backup_pool['path']) / backup_file

            if not backup_path.exists():
                self.task_queue.complete_task(task_id, success=False, error="Backup file not found")
                return False

            # ── Phase 2: Extract archive ─────────────────────────────────────────
            is_remote_dest = dest_pool.get('pool_type') == 'remote'

            if is_remote_dest:
                restore_staging = Path(self.config.tmp_dir) / f".restore_stage_{task_id}"
                restore_staging.mkdir(parents=True, exist_ok=True)
                temp_extract_dir = restore_staging
                dest_path = None
            else:
                temp_extract_dir = Path(dest_pool['path']) / f".restore_temp_{int(time.time())}"
                if temp_extract_dir.exists():
                    shutil.rmtree(temp_extract_dir)
                temp_extract_dir.mkdir(parents=True, exist_ok=True)
                dest_path = Path(dest_pool['path']) / dest_volume_name
                if conflict_resolution == 'overwrite' and dest_path.exists():
                    shutil.rmtree(dest_path)

            self.task_queue.update_progress(task_id, {
                "current_operation": f"Extracting {backup_file}",
                "progress_percent": 50,
            })
            print(f"[TASK:{task_id}] Extracting archive to {temp_extract_dir}", flush=True)

            process = subprocess.Popen(
                ["tar", "-xzf", str(backup_path), "-C", str(temp_extract_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            _, stderr = process.communicate()
            if process.returncode != 0:
                error_msg = f"Restore failed: {stderr.strip()}"
                print(f"[TASK:{task_id}] {error_msg}", flush=True)
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
                shutil.rmtree(temp_extract_dir, ignore_errors=True)
                return False

            extracted_items = [i for i in temp_extract_dir.iterdir() if i.name not in ('.', '..')]
            if not extracted_items:
                error_msg = "Restore failed: backup archive contained no files"
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
                shutil.rmtree(temp_extract_dir, ignore_errors=True)
                return False

            source_vol = extracted_items[0]

            # ── Phase 3: Place volume at destination ─────────────────────────────
            self.task_queue.update_progress(task_id, {
                "current_operation": f"Restoring {dest_volume_name} to {dest_pool_name}",
                "progress_percent": 70,
            })

            if is_remote_dest:
                remote_dest = self.volume_service._build_rsync_target(
                    dest_pool, dest_volume_name, trailing_slash=True
                )
                src = str(source_vol) + ("/" if source_vol.is_dir() else "")
                rsync_cmd = ["rsync", "-avz", "--progress"]
                if conflict_resolution == 'overwrite':
                    rsync_cmd.append("--delete")
                ok = self._stream_rsync(
                    task_id,
                    rsync_cmd + [src, remote_dest],
                    progress_range=(70, 90),
                    label=f"Uploading {dest_volume_name} to {dest_pool_name}",
                )
                shutil.rmtree(temp_extract_dir, ignore_errors=True)
                if not ok:
                    self.task_queue.complete_task(
                        task_id, success=False,
                        error="Failed to transfer restore to remote pool"
                    )
                    return False
            else:
                assert dest_path is not None
                if conflict_resolution == 'merge' and dest_path.exists():
                    if source_vol.is_dir():
                        shutil.copytree(str(source_vol), str(dest_path), dirs_exist_ok=True)
                    else:
                        shutil.copy2(str(source_vol), str(dest_path))
                    shutil.rmtree(str(source_vol), ignore_errors=True)
                else:
                    if source_vol.is_dir():
                        source_vol.rename(dest_path)
                    else:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        source_vol.rename(dest_path)
                shutil.rmtree(temp_extract_dir, ignore_errors=True)

            # ── Phase 4: Verify ───────────────────────────────────────────────────
            self.task_queue.update_progress(task_id, {
                "current_operation": "Verifying restore",
                "progress_percent": 93,
            })
            if not self._verify_backup(str(backup_path)):
                self.task_queue.complete_task(task_id, success=False, error="Restore verification failed")
                return False

            self.task_queue.update_progress(task_id, {"current_operation": "Restore complete", "progress_percent": 100})
            self.task_queue.complete_task(task_id, success=True)
            return True

        except Exception as e:
            error_msg = f"Restore error: {e}"
            print(f"[TASK:{task_id}] {error_msg}", flush=True)
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False
        finally:
            self.task_queue.remove_lockfile(lock_file)
            if restore_staging and restore_staging.exists():
                shutil.rmtree(restore_staging, ignore_errors=True)
            if pulled_backup_path and pulled_backup_path.exists():
                pulled_backup_path.unlink(missing_ok=True)

    def _transfer_to_remote_backup(self, task_id: str, local_archive_path: str,
                                   remote_pool: dict, archive_name: str) -> bool:
        """Transfer backup archive to remote rsync daemon pool."""
        remote_host = remote_pool.get('remote_host')
        rsync_module = remote_pool.get('rsync_module')
        if not remote_host or not rsync_module:
            print(f"[TASK:{task_id}] Remote pool missing remote_host or rsync_module", flush=True)
            return False
        target = f"rsync://{remote_host}/{rsync_module}/{archive_name}"
        ok = self._stream_rsync(
            task_id,
            ["rsync", "-avz", "--progress", local_archive_path, target],
            progress_range=(75, 95),
            label=f"Uploading {archive_name} to remote",
        )
        if ok:
            print(f"[TASK:{task_id}] Archive uploaded successfully", flush=True)
        return ok

    def _stream_rsync(self, task_id: str, cmd: list, progress_range: tuple = (0, 100),
                      label: str = "Transferring") -> bool:
        """Run rsync, streaming output to the task log with live progress updates."""
        start_pct, end_pct = progress_range
        print(f"[TASK:{task_id}] {label}: {' '.join(cmd)}", flush=True)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr so all output is logged
                text=True,
                bufsize=1,
            )
            while True:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                line = line.rstrip()
                if not line:
                    continue
                print(f"[TASK:{task_id}] {line}", flush=True)
                if "%" in line:
                    try:
                        pct = int(line.split("%")[0].split()[-1])
                        mapped = start_pct + int(pct * (end_pct - start_pct) / 100)
                        self.task_queue.update_progress(task_id, {
                            "progress_percent": min(mapped, end_pct),
                            "current_operation": f"{label}: {pct}%",
                        })
                    except (ValueError, IndexError):
                        pass
            proc.wait()
            if proc.returncode != 0:
                print(f"[TASK:{task_id}] rsync exited with code {proc.returncode}", flush=True)
            return proc.returncode == 0
        except Exception as e:
            print(f"[TASK:{task_id}] rsync error: {e}", flush=True)
            return False

# Global backup service instance
_backup_service = None


def get_backup_service(config=None, volume_service=None) -> BackupService:
    """Get backup service instance."""
    global _backup_service
    if _backup_service is None and config and volume_service:
        _backup_service = BackupService(config, volume_service)
    return _backup_service
