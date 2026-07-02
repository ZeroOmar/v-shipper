"""Migration service for volume migrations."""

import subprocess
import time
from pathlib import Path
from typing import Optional
from app.services.task_queue import get_task_queue
from app.services import container_control
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
                      conflict_resolution: Optional[str] = None, rename_dest: Optional[str] = None,
                      stop_containers_before: bool = False, start_containers_after: bool = False) -> bool:
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
        stopped_containers = []

        try:
            self.task_queue.start_task(task_id)

            if stop_containers_before:
                stopped_containers = container_control.stop_running_containers(
                    source_pool, source_volume_name, task_id
                )

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
            rsync_success, rsync_error = None, None

            # Native rsync cannot transfer daemon-to-daemon. When both pools are
            # remote, have the destination v-helper pull from the source's rsync
            # module instead. Without a v-helper on the destination (or with one
            # too old to expose the endpoint) we fall through to the direct rsync
            # below, which surfaces the clear "cannot both be remote" error.
            both_remote = (source_pool.get("pool_type") == "remote"
                           and dest_pool.get("pool_type") == "remote")
            if both_remote:
                dest_api = client_for_pool(dest_pool)
                if dest_api is not None:
                    try:
                        rsync_success, rsync_error = self._rsync_pull_via_helper(
                            task_id, source_pool, source_volume_name, effective_dest,
                            dest_api, overwrite=overwrite
                        )
                    except RemoteApiError as e:
                        if "404" in str(e):
                            print(f"[TASK:{task_id}] Destination v-helper has no rsync-pull "
                                  f"endpoint (too old); falling back to direct rsync", flush=True)
                        else:
                            rsync_success, rsync_error = False, f"Remote pull failed: {e}"

            if rsync_success is None:
                rsync_success, rsync_error = self._rsync_volume(
                    task_id, source_path, dest_path, source_pool, dest_pool, overwrite=overwrite
                )

            # A cancel during the transfer leaves a partially-migrated destination
            # volume — remove it and record the task as cancelled, not failed. This
            # matches the on-failure cleanup below; for an overwrite the volume was
            # being replaced in place, so the pre-existing copy is already partial.
            if self.task_queue.is_cancelled(task_id):
                print(f"[TASK:{task_id}] Migration cancelled — cleaning up partial destination", flush=True)
                if overwrite:
                    print(f"[TASK:{task_id}] Note: destination was overwritten in place; removing the partially-updated volume", flush=True)
                self._cleanup_partial_destination(task_id, dest_pool_name, effective_dest)
                self.task_queue.finalize_cancelled(task_id)
                return False

            if not rsync_success:
                error_msg = rsync_error or "Rsync failed"
                self.task_queue.complete_task(task_id, success=False, error=error_msg)
                self._cleanup_partial_destination(task_id, dest_pool_name, effective_dest)
                return False
            
            self.task_queue.update_progress(task_id, {
                "current_operation": "Verifying migration",
                "progress_percent": 85
            })
            
            # Verify if requested
            if verify:
                if not self._verify_migration(task_id, source_path, dest_path, source_pool, dest_pool,
                                              source_volume_name, effective_dest):
                    error_msg = "Migration verification failed"
                    self.task_queue.complete_task(task_id, success=False, error=error_msg)
                    self._cleanup_partial_destination(task_id, dest_pool_name, effective_dest)
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
                print(f"[TASK:{task_id}] Deleting source: {source_pool_name}/{source_volume_name}", flush=True)
                deleted = self.volume_service.delete_volume(source_pool_name, source_volume_name, task_id=task_id)
                if deleted:
                    print(f"[TASK:{task_id}] Source deleted successfully", flush=True)
                else:
                    print(f"[TASK:{task_id}] Warning: source delete returned failure — manual cleanup may be needed", flush=True)
            
            self.task_queue.complete_task(task_id, success=True)
            return True
        
        except Exception as e:
            error_msg = f"Migration error: {str(e)}"
            print(f"[TASK:{task_id}] Migration failed: {e}", flush=True)
            self.task_queue.complete_task(task_id, success=False, error=error_msg)
            return False
        
        finally:
            if start_containers_after and stopped_containers:
                container_control.start_containers(source_pool, stopped_containers, task_id)
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
            self.task_queue.register_process(task_id, process)

            try:
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
            finally:
                self.task_queue.unregister_process(task_id, process)

            # A cancel terminates rsync mid-transfer, so a non-zero code here may
            # just be the SIGTERM/SIGKILL — let the caller detect cancellation.
            if self.task_queue.is_cancelled(task_id):
                return False, "Cancelled by user"

            if return_code != 0:
                stderr = process.stderr.read()
                error_msg = f"Rsync failed with code {return_code}: {stderr.strip()}"
                for line in (error_msg.splitlines() or [""]):
                    print(f"[TASK:{task_id}] [ERROR] {line}", flush=True)
                return False, error_msg

            return True, ""

        except Exception as e:
            error_msg = f"Rsync execution failed: {e}"
            print(f"[TASK:{task_id}] [ERROR] {error_msg}", flush=True)
            return False, error_msg

    def _rsync_pull_via_helper(self, task_id: str, source_pool: dict, source_volume_name: str,
                               dest_volume_name: str, dest_api, overwrite: bool = False) -> tuple[bool, str]:
        """Drive a remote→remote migration by having the destination v-helper
        pull from the source's rsync module, streaming its progress/log into
        this task. Mirrors ``_rsync_volume``'s (success, error) contract.

        ``RemoteApiError`` from *starting* the pull propagates to the caller (so
        a 404 against an old v-helper can trigger fallback); a failure once the
        job is running is returned as ``(False, error)``.
        """
        source_host = source_pool.get("remote_host")
        source_module = source_pool.get("rsync_module")
        print(f"[TASK:{task_id}] Remote→remote: destination v-helper will pull "
              f"rsync://{source_host}/{source_module}/{source_volume_name}/ → {dest_volume_name}",
              flush=True)

        job_id = dest_api.rsync_pull(
            source_host, source_module, source_volume_name, dest_volume_name,
            delete=overwrite,
        )

        offset = 0
        while True:
            # If the user cancelled, stop the remote pull job on the destination
            # v-helper (best-effort — tolerates an older v-helper without the
            # endpoint) and let the caller clean up the partial destination.
            if self.task_queue.is_cancelled(task_id):
                print(f"[TASK:{task_id}] Cancelling remote pull job {job_id} on destination v-helper", flush=True)
                try:
                    dest_api.rsync_cancel(job_id)
                except RemoteApiError as e:
                    print(f"[TASK:{task_id}] Could not cancel remote pull job (continuing cleanup): {e}", flush=True)
                return False, "Cancelled by user"

            try:
                status = dest_api.rsync_job_log(job_id, offset)
            except RemoteApiError as e:
                error_msg = f"Lost contact with destination v-helper during pull: {e}"
                print(f"[TASK:{task_id}] [ERROR] {error_msg}", flush=True)
                return False, error_msg

            for line in status.get("lines", []):
                print(f"[TASK:{task_id}] {line}", flush=True)
            offset = status.get("next_offset", offset)

            percent = status.get("percent")
            if isinstance(percent, int):
                self.task_queue.update_progress(task_id, {
                    "progress_percent": min(80, 10 + int(percent * 0.7)),
                    "current_operation": f"Pulling {source_volume_name} → {dest_volume_name} ({percent}%)",
                })

            state = status.get("state")
            if state == "done":
                return True, ""
            if state == "failed":
                error_msg = status.get("error") or "Remote pull failed"
                for line in (error_msg.splitlines() or [""]):
                    print(f"[TASK:{task_id}] [ERROR] {line}", flush=True)
                return False, error_msg

            time.sleep(2)

    def _cleanup_partial_destination(self, task_id: str, dest_pool_name: str, dest_volume_name: str):
        """Remove partial destination volume on failure (local or remote)."""
        try:
            print(f"[TASK:{task_id}] Cleaning up partial destination: {dest_pool_name}/{dest_volume_name}", flush=True)
            deleted = self.volume_service.delete_volume(dest_pool_name, dest_volume_name, task_id=task_id)
            if deleted:
                print(f"[TASK:{task_id}] Partial destination removed", flush=True)
            else:
                print(f"[TASK:{task_id}] Warning: could not remove partial destination {dest_pool_name}/{dest_volume_name}", flush=True)
        except Exception as e:
            print(f"[TASK:{task_id}] Failed to clean up partial destination {dest_pool_name}/{dest_volume_name}: {e}", flush=True)

    def _verify_migration(self, task_id: str, source_path: str, dest_path: str,
                          source_pool: dict = None, dest_pool: dict = None,
                          source_volume_name: str = None, dest_volume_name: str = None) -> bool:
        """Verify migration by comparing total file-content bytes in source and dest.

        Remote totals come from ``volume_service._get_remote_size`` (v-helper
        when available, else rsync); local totals from ``_get_dir_size``. Both
        sum regular-file bytes only and exclude symlinks, so the two sides are
        measured identically.
        """
        try:
            if source_pool and source_pool.get("pool_type") == "remote":
                source_bytes = self.volume_service._get_remote_size(source_pool, source_volume_name)
                if source_bytes is None:
                    print(f"[TASK:{task_id}] Verification failed: could not reach source at {source_path}", flush=True)
                    return False
            else:
                source_bytes = self.volume_service._get_dir_size(Path(source_path))

            if dest_pool and dest_pool.get("pool_type") == "remote":
                dest_bytes = self.volume_service._get_remote_size(dest_pool, dest_volume_name)
                if dest_bytes is None:
                    print(f"[TASK:{task_id}] Verification failed: could not reach dest at {dest_path}", flush=True)
                    return False
            else:
                dest_bytes = self.volume_service._get_dir_size(Path(dest_path))

            if source_bytes != dest_bytes:
                print(
                    f"[TASK:{task_id}] Verification failed: source has {source_bytes:,} bytes ({source_path}),"
                    f" dest has {dest_bytes:,} bytes ({dest_path})",
                    flush=True,
                )
                return False

            print(
                f"[TASK:{task_id}] Verification passed: {source_bytes:,} bytes"
                f" — source ({source_path}) matches dest ({dest_path})",
                flush=True,
            )
            return True

        except Exception as e:
            print(f"[TASK:{task_id}] Verification error for {source_path} → {dest_path}: {e}", flush=True)
            return False


# Global migration service instance
_migration_service = None


def get_migration_service(config=None, volume_service=None) -> MigrationService:
    """Get migration service instance."""
    global _migration_service
    if _migration_service is None and config and volume_service:
        _migration_service = MigrationService(config, volume_service)
    return _migration_service
