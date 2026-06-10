# Changelog

All notable changes to v-shipper are documented in this file.

## 0.0.8

### Fixed

- **Migration from remote source pool failed** — `source_path` was built from `pool['path']` which is `/` for remote pools, producing `//volume` (a nonexistent local path); now uses `_build_rsync_target` to construct a proper `rsync://` URL
- **Migration from local to remote destination failed** — `dest_path` had the same `pool['path']` bug, causing rsync to write to `//volume` on the local filesystem instead of the remote daemon; now uses `_build_rsync_target` for remote destinations
- **Migration verification reported 0 source files for remote pools** — `rsync --list-only` without `-r` only lists the top level of a volume directory (all directories, zero files); added `recursive=True` to listing calls used for verification and size calculation
- **Migration verification reported -1 dest files for remote destinations** — `find` was called on the rsync URL string instead of the local path; remote destinations are now verified via recursive rsync listing
- **Backup from remote source pool failed** — `tar` was given `//volume` as the source path (same `pool['path']` bug); remote source volumes are now pulled to a local staging dir via rsync first, then archived, and the staging dir is cleaned up in `finally`
- **Restore to remote destination crashed with "Read-only file system"** — `temp_extract_dir` was constructed as `Path('/') / '.restore_temp_...'` for remote pools; extraction now happens in `{tmp_dir}/.restore_stage_{task_id}/` and the result is rsynced to the remote pool
- **Restore to remote destination put files at pool root** — rsync target was the module root (`rsync://host/module/`) instead of the volume path; now uses `_build_rsync_target(pool, dest_volume_name, trailing_slash=True)`
- **Remote volume delete left an empty directory that still appeared in the listing** — previous approach used `--remove-source-files` (deleted file content only, left empty dirs, accumulated a local sink that was never cleaned); replaced with targeted `rsync --delete --force` to the module root using include/exclude filters so the volume directory itself is removed
- **Staging files accumulated and were never cleaned up** — remote source staging dirs (`.backup_stage_*`), remote staging archives (in `staging_dir`), downloaded backup archives, and restore staging dirs (`.restore_stage_*`) are now all tracked before `try` blocks and removed in `finally`
- **Remote docker host volume sizes never resolved** — `_list_remote_volumes` always set `size_loading=True` with no background refresh; now uses the same cache + background-thread pattern (`_start_remote_volume_size_refresh`) as local pools

### Changed

- **`tmp_dir` is now configurable** — added `tmp_dir` YAML config key; task queue lock files, task state JSON, and staging dir all derive from it (default: `/tmp`). Useful for local development where `/tmp` is inconvenient.
- **`TaskQueue` lazily initialized** — was eagerly constructed at import time (before config loaded), meaning `tmp_dir` had no effect on the paths it used; now initialized on first `get_task_queue(tmp_dir=...)` call from `app.py`
- **Existence check for remote migration destination** — local pools check `Path.exists()`; remote pools now check via `_run_rsync_list` to avoid false positives from treating the rsync URL as a local path

## 0.0.7

### Fixed
- **Logout never cleared session** — session dict was missing the `session_id` key, so `del sessions[session_id]` was a no-op on every logout; sessions now store their own ID and are correctly removed
- **`list_tasks` built and immediately discarded a result set** — the first task loop (before the sorted loop) was dead code producing a list that was never used; removed
- **`staging_dir` config ignored** — the `staging_dir` YAML key was parsed but never passed into `AppConfig`, so restore operations always used the hardcoded `/tmp/staging` regardless of config; now correctly read and forwarded
- **Remote backup restore used hardcoded staging path** — `backup_service.py` had `/tmp/staging` hardcoded; now uses `self.config.staging_dir`

### Security
- **Command injection via `shell=True`** — `_verify_migration`, `_create_archive`, `_verify_backup`, and `restore_backup` all passed f-strings with user-controlled paths to `subprocess` with `shell=True`; all replaced with list-form subprocess calls
- **Path traversal in rename/delete** — `rename_volume` and `delete_volume` constructed paths with user-supplied names without verifying the result stayed inside the pool directory; added `.resolve()` + `is_relative_to()` guards
- **`validate_auth` accepted base64-encoded passwords from clients** — a second check decoded the incoming password as base64 and compared, meaning any client knowing the encoding could bypass the real password check; removed

### Changed
- **`staging_dir` now configurable** — parsed from YAML config and passed through `AppConfig`; default remains `/tmp/staging`
- **Deprecated `@app.on_event` replaced** — startup/shutdown handlers migrated to FastAPI `lifespan` context manager
- **Session token no longer sent in GET query params** — `main.js` was appending `?session_id=...` to all GET requests, leaking the token into URLs and server logs; cookies handle auth automatically
- **Pool selector modals no longer make redundant API calls** — `loadPoolsForSelect` and `loadBackupPoolsForSelect` now read from `poolsCache` instead of fetching `/api/pools` a second time on every modal open

### Removed
- **Dead `ssh_service.py`** — SSH support was removed in 0.0.3 but the file remained; deleted
- **`import threading` / `import uuid` inside route functions** — moved to module level in `routes.py`

### Fixed (remote docker host volumes)
- **Remote docker host pools returned no volumes** — `_parse_rsync_list_line` detected directories by trailing slash (`name.endswith('/')`) which rsync daemons on some systems (NAS devices, older rsync versions) omit; switched to mode-string detection (`mode[0] == 'd'`) which is always present
- **Remote docker host volumes never resolved size** — `_list_remote_volumes` always set `size_loading=True` with no background calculation, causing the size polling loop to run indefinitely; now uses the same cache + background-thread pattern as local volumes via `_start_remote_volume_size_refresh`

## 0.0.5

### Fixed
- **Remote backup restore with trailing slash** — Fixed rsync error "Not a directory" when restoring backup files by using `trailing_slash=False` for file paths
- **Remote backup deletion** — Delete operations on remote backup pools now work correctly using `rsync --remove-source-files` to pull and remove files in a single operation
- **Rsync file path handling** — Corrected rsync target path construction for individual files vs directories (files should not have trailing slashes)

### Technical Details

#### Backend Changes
- `app/services/backup_service.py`:
  - Updated `restore_backup()` to use `trailing_slash=False` when building rsync target for backup files

- `app/services/volume_service.py`:
  - Updated `delete_volume()` to execute `rsync --remove-source-files` for remote pool deletion instead of failing
  - Now properly syncs remote files to `/tmp/.vshipper_delete_sink/` and removes the source copy

## 0.0.4

### Added
- **Remote rsync daemon support** — Full support for rsync daemon pools as remote backup/docker sources. Pools can now specify `pool_type: remote`, `remote_host`, and `rsync_module` for rsync daemon access
- **Remote backup restoration** — Backup files from remote pools are now automatically pulled to `/tmp/staging` via rsync before extraction
- **Remote pool file deletion** — Deleting backups from remote pools now uses rsync daemon protocol instead of treating them as local filesystem
- **Remote pool storage calculation** — Remote pools now calculate total storage by summing file sizes from rsync listing (shown in UI pool card)
- **Pool role metadata** — Added `role` field to PoolStats (docker vs backup) for proper UI button rendering based on pool function, not storage type

### Fixed
- **Configuration typo** — Fixed `rysnc_module` → `rsync_module` in run_dev.sh example config
- **Backup pool cleanup crash** — Fixed `'BackupPool' object has no attribute 'path'` error by using `.pool` attribute and skipping remote pools in orphaned directory cleanup
- **Backup pool UI buttons** — Fixed backup pools showing Migrate/Backup buttons instead of Restore button by checking pool `role` (docker vs backup) instead of `pool_type` (local vs remote)
- **Remote backup deletion** — Delete operations on remote backup pools no longer fail with "file not found" errors
- **Remote backup restore** — Restore operations now properly fetch backup files from remote rsync daemons before extraction

### Changed
- **Pool type vs role distinction** — `pool_type` now indicates storage type (local/remote), while `role` indicates pool function (docker/backup). UI button rendering now correctly uses `role` instead of `pool_type`
- **Remote pool staging** — Remote backup restore operations now stage files in `/tmp/staging` directory before extraction

### Technical Details

#### Backend Changes
- `app/services/volume_service.py`:
  - Added `_is_remote_pool()` helper
  - Added `_build_rsync_target()` for constructing rsync daemon URLs
  - Added `_run_rsync_list()` for remote directory listing
  - Added `_parse_rsync_list_line()` for parsing rsync output
  - Added `_list_remote_volumes()` for remote pool volume discovery
  - Added `_list_remote_backups()` for remote backup pool discovery
  - Added `_get_remote_size()` for remote file size calculation
  - Added `_get_remote_pool_total_size()` for pool storage totals
  - Updated `delete_volume()` to handle remote pool deletion via rsync
  - Updated `get_pool_stats()` to calculate and return total size for remote pools with reachability status

- `app/services/backup_service.py`:
  - Updated `restore_backup()` to detect remote backup pools and pull files to `/tmp/staging` via rsync before extraction

- `app/app.py`:
  - Updated `_cleanup_orphaned_restore_dirs()` to skip remote backup pools and use correct `.pool` attribute

- `app/static/main.js`:
  - Updated `displayPools()` to use pool `role` instead of `pool_type` for backup count labeling
  - Updated `loadVolumesForPool()` to use pool `role` for metadata caching
  - Updated `displayVolumes()` to use pool `role` for button logic (Migrate/Backup vs Restore)

#### Configuration Changes
Remote pools now support rsync daemon access:

```yaml
docker_hosts:
  - name: remote_host
    pool: /
    pool_type: remote
    remote_host: 10.0.13.21:30026
    rsync_module: docker-volumes

backup_pools:
  - name: remote_backup
    pool: /
    pool_type: remote
    remote_host: 10.0.13.21:30026
    rsync_module: docker-backup
```

## 0.0.3

### Added
- **Async volume size calculation** — Large directory sizes are now calculated in the background, preventing UI freeze when first listing volumes
- **Volume size caching** — Calculated sizes are cached in memory to speed up subsequent pool refreshes
- **Task persistence** — Task state is now saved to `/tmp/vshipper_tasks.json` and recovered on application restart
- **Crash recovery** — Tasks in progress when app restarts are marked as `failed` with a clear error message
- **Orphaned temp directory cleanup** — Application automatically removes `.restore_temp_*` directories from failed restore operations on startup
- **Toast notifications** — Error and success messages now appear as bottom-right toast notifications with proper styling and animations
- **Development server script** — Added `run_dev.sh` for local development that only watches `app/` directory to prevent auto-reload during volume operations
- **Task progress error handling** — Task progress polling now gracefully handles 404 responses when tasks are not found

### Changed
- **Simplified pool architecture** — Removed SSH support. Remote pools are now treated as mounted filesystems (NFS, CIFS, etc.) marked with `pool_type: remote` for UI labeling only
- **Configuration simplification** — Removed `ssh_user` and `ssh_key` from `DockerHost` model
- **Loading UX** — Volume listing now shows "Calculating..." with a spinner while directory sizes are being computed in the background
- **Progress polling** — Increased poll interval to 2 seconds to reduce server load
- **Deprecation warning suppression** — Paramiko CryptographyDeprecationWarning now filtered at import time

### Fixed
- **Uvicorn auto-reload crash** — Fixed issue where rsync/tar writing .py files would trigger auto-reload and restart the application during operations
- **Volume size freeze** — Large volumes no longer freeze the UI when first loading pools
- **404 on task progress after restart** — Task state now persists across application restarts
- **Lost restore state** — Restore operations that crash mid-way now properly clean up temporary directories

### Removed
- **SSH support** — Removed all SSH/Paramiko-based remote pool access. Use standard filesystem mounts instead
- **Remote pool SSH configuration** — Removed `ip`, `ssh_user`, and `ssh_key` from DockerHost model

## Technical Details

### Backend Changes
- `app/services/task_queue.py` — Added task persistence with `_load_tasks()` and `_save_tasks()` methods
- `app/services/volume_service.py` — Added async size caching with background thread pool
- `app/services/migration_service.py` — Removed SSH support, simplified to local file operations only
- `app/services/ssh_service.py` — Deprecated (kept for backward compatibility, no longer used)
- `app/app.py` — Added `_cleanup_orphaned_restore_dirs()` in startup event
- `app/models.py` — Removed `ssh_user` and `ssh_key` from DockerHost model, added `size_loading` to VolumeInfo
- `app/api/routes.py` — Removed SSH fields from pool info construction

### Frontend Changes
- `app/static/main.js` — Added `volumeSizePollInterval`, `startVolumeSizePolling()`, improved `loadVolumesForPool()` UX
- `app/static/main.js` — Refactored `showError()` and `showSuccess()` to use new toast system
- `app/static/main.js` — Added error handling for failed task progress requests
- `app/static/style.css` — Added `.toast-container` and `.toast` styles for bottom-right notifications
- `app/templates/index.html` — Added toast container markup
- `app/templates/index.html` — Updated configuration examples

### Development
- Added `run_dev.sh` script with `--reload-dirs app` to prevent auto-reload on volume changes
- Updated `README.md` with local development instructions
- Updated `SKILLS_DEBUGGING.md` with new debugging tips for task persistence and uvicorn reload issues
- Updated `SKILLS_ADDING_FEATURES.md` to remove SSH patterns and reflect local-only operations

### Configuration Changes

**Before** (with SSH):
```yaml
docker_hosts:
  - name: prod-host
    ip: 10.0.0.100
    pool: /mnt/docker_volumes
    pool_type: remote
    ssh_user: admin
    ssh_key: <base64_encoded_ssh_key>
```

**After** (local mounts only):
```yaml
docker_hosts:
  - name: prod-host
    pool: /mnt/docker_volumes
    pool_type: remote     # UI label; path must be mounted as filesystem
```

For remote access, mount the remote storage before starting the app:
```bash
mount -t nfs 10.0.0.100:/export/volumes /mnt/docker_volumes
```

## Migration Guide

### From SSH-based Remote Pools

If you were using SSH-based remote pools:

1. **Before**: Configure SSH credentials in v-shipper
2. **After**: Mount remote storage on the host running v-shipper, then reference the mount point

**Example**:
```bash
# Mount NFS volume
sudo mount -t nfs 10.0.0.1:/export/docker_volumes /mnt/remote_volumes

# Update config
docker_hosts:
  - name: nfs-pool
    pool: /mnt/remote_volumes
    pool_type: remote
```

### Updating Task Persistence

If upgrading an existing v-shipper instance:
- Running tasks are automatically recovered on restart
- Task state is stored in `/tmp/vshipper_tasks.json`
- For production deployments, consider mounting a persistent volume at `/tmp` to preserve task history across container restarts

```bash
docker run -v /persistent/data:/tmp v-shipper:latest
```

## Known Limitations

- Remote pools require NFS/CIFS or other mounted filesystem access (SSH no longer supported)
- Task persistence is stored in `/tmp`, which may be ephemeral in some container runtimes
- Orphaned `.restore_temp_*` directories are only cleaned on application startup
- Volume size calculation is single-threaded per pool
