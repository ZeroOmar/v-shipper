# Changelog

All notable changes to v-shipper are documented in this file.

## 0.8.0

v-shipper and v-helper now version **independently** — this release ends the shared version line used through `0.7.0`.

### Added

- **Bulk actions** — volume and backup lists now have a checkbox on every row. Selecting one or more reveals a toolbar (with "Select all" / "Clear") offering the same actions as the single-item views, minus rename: Backup, Migrate, Delete, and Permissions for volumes; Restore and Delete for backups. Each bulk dialog mirrors its single-item dialog, with the "Source Volume" / "Volume" / "Backup File" field shown as the list of selected items. New `/api/bulk/{backup,migrate,delete,permissions,restore}` endpoints back the feature. A bulk action runs exactly like a scheduled backup: every item runs sequentially under one summary task, the per-item sub-tasks are grouped in the summary's detail view, and a single completion notification covers the whole batch. Bulk migrate/restore offer a "skip / overwrite / merge" choice for destinations that already exist; bulk restore derives each destination volume name from its archive filename.

### Changed

- **Version checks no longer compare v-shipper against v-helper** — instead, the UI checks each component against its own latest GitHub release. v-shipper flags itself "out of date" when a newer v-shipper tag exists, and flags a connected v-helper when a newer v-helper tag exists, looking the two repositories' tags up independently. This replaces the old model where the two shared a version line and were compared against each other.
- **Tasks now run strictly one at a time** — the task queue gained a real single-worker FIFO. Previously every triggered operation spawned its own thread and ran immediately, so a task started while another was running could overlap it. Now a task triggered while another runs waits in the list as "pending" until its turn. Manual operations, scheduled backups, and bulk actions all flow through the same queue.
- **Backup archives are written world-accessible (`0o777`)** on the backup pool, so a backup can be read or managed regardless of the user that owns the pool. For remote backup pools the mode is applied to the staging copy and carried to the remote by `rsync -a`.

### Fixed

- **Toasts appeared behind a dialog or the settings overlay** — the toast container shared a `z-index` with the settings overlay, so its blur painted over notifications. The toast container now sits above both modals and the settings overlay.

## 0.7.0

Coordinated release with v-helper `0.7.0` (shared version line). No functional change to v-helper this release; the bump keeps the two version lines aligned so connected helpers are not flagged "out of date".

### Added

- **Permissions notifications** — permission/ownership changes (chmod/chown) now fire task-completion notifications like every other operation. A new "Permissions" topic is available in the notification settings, and the permissions task records the applied mode/owner so they appear in the notification's parameters block.
- **Re-apply permissions even when unchanged** — the Edit Permissions modal gained two checkboxes, "Apply chmod even if unchanged" and "Apply chown even if unchanged". By default only changed fields are sent; checking these forces a recursive `chmod -R` / `chown -R` even when the displayed top-level value already matches, which is the way to fix permissions/ownership that have drifted on nested files.

### Fixed

- **Edit Schedule checkboxes misaligned** — the "Stop / Start container(s)" checkboxes in the schedule form inherited the form's column layout, stacking the box above its label and centering it. They now render inline, box-then-label, left-aligned.

## 0.6.0

Coordinated release with v-helper `0.6.0` (shared version line). Remote container control requires v-helper `0.6.0`+.

### Added

- **Stop / start containers around volume operations** — migrate, backup, and permission changes now offer optional "Stop container(s) before" and "Start container(s) after" checkboxes; rename and delete offer "Stop container(s) before". The checkboxes appear only when the volume is in use by a running container, and default to unchecked. Stopping lets an operation run against a quiesced volume (no torn reads / busy targets); the start step restarts only the containers that were actually stopped, and runs even if the operation fails so containers come back up. A failed stop aborts the operation rather than proceeding on a live volume. Works on both local pools (Docker socket) and remote pools (via the v-helper control API).
- **Scheduled backups can stop / start containers** — backup schedules gained `stop_containers_before` / `start_containers_after` flags, applied to every volume in the schedule on each run.
- **`container_stop_timeout` pool config option** (default `120`) — the grace period, in seconds, given to a container to shut down cleanly before it is killed. Generous by default so slow-to-stop containers aren't killed mid-flush; the remote HTTP timeout is sized above it so a slow-but-successful stop isn't misread as a failure.

### Changed

- **Container-usage tooltip repositioned** — the badge tooltip is now `position: fixed` and placed by JS relative to the badge, so it escapes the volume row's `overflow` and the scrollable volumes pane that previously clipped it. Toggled via a `show-tip` class instead of CSS `:hover`/`.open`.

## 0.5.2

### Fixed

- **Container detection missed bind-backed named volumes** — the container-usage scan only looked at each mount's `Source`, which for a Docker named volume is the managed mountpoint (`/var/lib/docker/volumes/<v>/_data`), not the `driver_opts: device` path. So a volume defined as `driver: local` + `o: bind` + `device: /host/path` was never matched and its containers went undetected — only raw bind mounts showed up. Named volumes are now resolved to their real host path (device option, falling back to mountpoint) before matching.
- **Matching now considers both the host path and the pool path** — container mounts are matched against both `docker_host_path/<volume>` (the real host base, e.g. a named volume's device base) and the pool's own `path/<volume>`, so setups that reference volumes via different paths (e.g. a `/var/...` named-volume device and a `/mnt/...` bind mount of the same folder) are both detected. To match named-volume devices, set `docker_host_path` to the host base those devices live under.

## 0.5.1

### Changed

- **Volume & backup action buttons are now icons** — the per-row action buttons (Migrate, Backup, Rename, Permissions, Delete, Restore) use emoji icons with the label shown on hover, instead of text. As more actions were added the text buttons crowded the row; the compact icons fit comfortably.

### Fixed

- **Docker socket connection failed with "Not supported URL scheme http+docker"** — the docker SDK (`docker==7.0.0`) is incompatible with `requests>=2.32`, so `docker.from_env()` raised on every call and the container-usage feature silently returned nothing. Bumped to `docker==7.1.0`, which restores the `http+docker://` transport adapter.
- **Noisy `CryptographyDeprecationWarning` (TripleDES) on startup** — paramiko (pulled in transitively by the docker SDK) logs a deprecation warning on import. It is now filtered at package import in `app/__init__.py`.

## 0.5.0

### Added

- **Volume permissions (chmod / chown)** — a per-volume **Permissions** button opens a modal pre-filled with the folder's current owner, group, and octal mode. Editing the permission runs `chmod -R`, editing the user/group runs `chown -R`, or both — as a tracked task whose verbose logs show the exact command and its output. Works on local pools (direct subprocess) and remote pools (via v-helper). New endpoints: `GET /api/permissions` (read current owner/group/mode) and `POST /api/permissions` (apply). Permission/owner inputs are validated at the boundary (`validate_mode`, `validate_owner_token`).
- **Docker socket support — "which containers use this volume"** — when a docker pool sets `docker_socket: true`, each volume row shows an aggregate status badge (running/mixed/stopped dot + container count) with a hover/click tooltip listing every container and its status. Matching is host-path-prefix based: a volume's host path (`docker_host_path/<name>`, falling back to the pool path) is matched against container mount sources, covering sub-folder bind mounts and local-driver volumes. Local pools query v-shipper's own Docker socket; remote pools query the remote socket via v-helper's `GET /docker/users`. New endpoint: `GET /api/containers?pool=`. New optional pool config keys: `docker_socket`, `docker_host_path`.
- **Running-container warning** — opening the Migrate, Rename, or Delete modal for a volume with running containers shows a warning banner naming them (informational — the action stays available).

### Changed

- **Backup pool viewer grouping** — backups are now grouped by **app (volume) name** first, with a sub-group per **source pool**, instead of a flat group per pool/volume pair. Makes it obvious when the same app has been backed up from multiple pools.

### Fixed

- **Backup list overflow on mobile** — long backup filenames pushed the Restore/Delete buttons out of the row. The name now wraps, the buttons keep their size, and narrow screens stack the name above a right-aligned button row.

## 0.4.5

### Added

- **v-helper version reporting and mismatch warnings** — v-shipper now reads each connected v-helper's version (via v-helper's new `GET /version`) and flags version drift: a v-helper older than v-shipper gets an "out of date" pill on its pool card, and a v-helper newer than v-shipper shows an "out of date" pill in the v-shipper header. v-shipper and v-helper now share a single version line and bump together on each release.

### Changed

- **Remote volume sizes are measured via v-helper when available** — `_get_remote_size` (used by both the volume-listing size refresh and migration verification) now calls v-helper's `GET /fs/size` for an exact on-filesystem byte count, falling back to rsync `--list-only` only when v-helper is absent or unreachable. This removes a redundant rsync round-trip on v-helper pools.
- **App version is now a single source of truth** — `app/__init__.py:__version__`; `HealthResponse` reads from it instead of a hardcoded literal.

### Fixed

- **Migration verification no longer fails over a few stray bytes** — source (local) and destination (remote) byte totals were measured differently: the local walk followed symlinks and counted regular files, while the rsync `--list-only` path counted symlinks as their target-string length, so verification could fail with e.g. "source has 162,535,613 bytes, dest has 162,535,708 bytes". Both sides now sum regular-file bytes only and exclude symlinks — the local `_get_dir_size` and v-helper's `/fs/size` use identical logic — so totals match. Requires v-helper `0.4.5+` for the API path; older or absent v-helper falls back to rsync.

## 0.4.4

### Fixed

- **Rename and create logs now appear in the UI task log viewer** — `rename_volume` and `create_volume` run as queued tasks but never received a `task_id`, so all their log lines (remote API errors, "already exists", path-traversal rejections, success messages) were printed without the `[TASK:id]` prefix and were invisible in the log viewer; the parameter is now threaded through from both routes.
- **Migration rsync failures now appear in the UI task log viewer** — `_rsync_volume` printed rsync failure output with a bare `[ERROR]` prefix; the multiline rsync stderr is now prefixed per-line with `[TASK:id]` so every line reaches the log buffer.
- **Multiline remote-delete errors now fully visible** — `delete_volume` logged multiline rsync/remote-API errors in a single print, so only the first line carried the `[TASK:id]` prefix and continuation lines were dropped from the viewer; every line is now prefixed.
- **Notification errors now attributed to their task** — post-completion notification failures were logged without the `[TASK:id]` prefix and never appeared against the task.
- **Size cache cleared after local volume deletion** — deleting a volume on a local pool left a stale entry in the size cache (remote deletes already evicted it).

### Changed

- **Unified delete primitive** — local volume deletion now uses `rm -rf` (new shared `rm_rf` helper) instead of `shutil.rmtree`/`unlink`, matching v-helper's `/fs/rm` so local and remote deletes behave identically and tolerate nested trees and mixed ownership/permissions.
- **Task logging centralized** — added a `task_log` helper that prefixes every line of a (possibly multiline) message with `[TASK:id]`; delete, rename, and create now share it.

## 0.4.3

### Fixed

- **Delete logs now appear in the UI task log viewer** — `delete_volume` had no `task_id` parameter, so all log lines (including rsync errors) were printed without the `[TASK:id]` prefix and were invisible in the log viewer. The parameter is now threaded through from the delete route and from the migration service (source delete + partial-destination cleanup).
- **v-helper used for remote volume deletion** — when a remote pool has `api_host` configured, `delete_volume` now calls `POST /fs/rm` on the v-helper API instead of the rsync `--delete` workaround. The rsync workaround runs as the rsync daemon user and fails with `Permission denied` on files owned by Docker container users; the API runs with the correct permissions. Requires v-helper `0.3.0+`. The rsync workaround is kept as a fallback for remote pools without v-helper.

## 0.4.2

### Fixed

- **Backup and restore verification logs now appear in the UI task log viewer** — `_verify_backup` was printing with `[ERROR]`/`[INFO]` prefixes instead of `[TASK:{task_id}]`, so verification results, archive corruption messages, and size output were invisible in the log viewer; all log lines now carry the task prefix

### Changed

- **README updated** — added backup schedules, Telegram notifications, and v-helper integration to the features list; added all missing API endpoints (volume create, notifications, refresh); updated the project structure tree; improved the configuration and troubleshooting sections

## 0.4.1

### Fixed

- **Create/Rename buttons now visible on v-helper remote pools** — the `isLocalDocker` guard in `displayVolumes` treated all remote pools the same and hid both buttons; it now checks `has_helper` and shows them when the pool has the control API configured
- **Backup count now shown for remote pool volumes** — both the v-helper API and rsync fallback listing paths in `_list_remote_volumes` were hardcoding `backups=[]`; they now call `_find_backups` like the local path does
- **Migration verification no longer fails on macOS** — verification was using `du -sb` to measure local byte counts, but the `-b` (bytes) flag is Linux/GNU only and exits non-zero on macOS, causing every local↔remote migration to report verification failure; replaced with `_get_dir_size` which sums `st_size` per file — the same metric rsync reports as "total size"
- **Destination volume cleaned up when verification fails** — `_cleanup_partial_destination` was not called on verification failure, leaving a partial copy in the destination pool
- **Remote destination cleanup was a silent no-op** — `_cleanup_partial_destination` used `Path(dest_path).exists()` which is always `False` for rsync URIs, so cleanup never ran for remote destinations even on rsync failure; it now delegates to `delete_volume` which handles both local and remote correctly
- **Migration log lines now appear in the UI task log viewer** — verification results, cleanup progress, delete-source steps, and migration failure messages were printed without `[TASK:id]` prefix and were invisible in the log viewer; all now carry the prefix
- **Stale create-volume error message** — failure message still read "or the pool is remote" after remote pools gained create support via v-helper; updated to indicate that a remote pool needs `api_host` configured

## 0.4.0

### Added

- **v-helper control API integration** — remote pools can now declare `api_host` and `api_key` config fields to connect to a [v-helper](https://github.com/ZeroOmar/v-helper) `0.2.0+` sidecar. When configured, v-shipper uses v-helper's HTTP API for operations that rsync cannot perform: create volume (`POST /fs/mkdir`), rename volume (`POST /fs/rename`), real disk free space (`GET /fs/disk`), and accurate modification timestamps (`GET /fs/ls`). All rsync-based file transfers are unchanged
- **Create volume on remote pools** — creating a new volume directory on a remote pool now works when v-helper is configured (previously returned an error)
- **Rename volume on remote pools** — renaming a volume on a remote pool now works when v-helper is configured (previously silently failed)
- **Real disk free space for remote pools** — the pool stats card for a v-helper-enabled remote pool now shows actual `Used` + `Free` with a usage bar, instead of showing only total-used-bytes with 0 free
- **v-helper badge on pool cards** — remote pools with `api_host` configured show a "v-helper" pill badge in the pool sidebar so it's clear which pools have full capabilities

### Changed

- **Migration verification now compares bytes** — `_verify_migration` was comparing file counts (via rsync `--list-only`), which missed truncated files. It now compares total byte sizes (`du -sb` for local, recursive rsync file-size sum for remote), giving a more accurate integrity check

## 0.3.1

### Changed

- **Elapsed and remaining times now display as `hh:mm:ss.SSS`** — task durations were shown everywhere as a raw seconds count (e.g. `3661.1s`). The task history cards, the task detail panel, and the Telegram notification messages now format durations as `hh:mm:ss.SSS` (e.g. `01:01:01.100`). The default notification template's `{elapsed}` placeholder is now pre-formatted, so its trailing `s` suffix was dropped; the stored `elapsed_seconds` field is unchanged, so this is purely a presentation change

## 0.3.0

### Fixed

- **Backups no longer report success when files are silently dropped** — `tar` exits 1 for both harmless warnings (a file changed mid-read, a socket skipped) and fatal ones where files were *omitted* from the archive (permission denied, unreadable, vanished mid-run). The exit-1 path previously accepted any non-empty output file as success, so an incomplete archive was reported as "Completed successfully". Archive creation now inspects `tar`'s stderr and fails the backup when it sees errors that drop files (`Permission denied`, `Can't open`, `Cannot stat`, `Error exit delayed from previous errors`, etc.), while still tolerating genuinely benign warnings
- **Partial archives are cleaned up on failure** — when archive creation fails for a local backup pool, the incomplete `.tar.gz` is now deleted so it can't be mistaken for a good backup or restored later (the remote-pool path already cleaned up on transfer failure)

### Added

- **Scheduled-run drill-down view** — the detail page for a scheduled backup now lists every per-volume backup that run spawned as a mini task list; each entry opens its own task detail (logs, params, progress) with a "← Back" link that returns to the schedule run. Per-volume sub-tasks are correlated to their run via a new `parent_task_id` tag so multiple runs of the same schedule stay separate

### Changed

- **Scheduled per-volume backups are hidden from the main task list** — the individual `backup` tasks created by a schedule no longer clutter the task history; they live inside their parent scheduled-run's detail view. The scheduled-run summary task itself remains in the list

## 0.2.0

### Security

- **Comprehensive input validation at the API boundary** — all request models now enforce a strict Docker-style name policy (`[A-Za-z0-9][A-Za-z0-9_.-]`, max 255 chars, no path separators or `..`), length caps, and value ranges (retention 1–365, `conflict_resolution` restricted to `overwrite`/`merge`/`rename`); path and query parameters (`pool`, `volume_name`, `task_id`) are validated explicitly since they bypass model validation
- **Defense-in-depth path containment** — a shared `safe_join` helper resolves and confirms every constructed path stays inside its pool directory; applied across `volume_service`, `backup_service`, and `migration_service`, replacing the previously inconsistent inline checks and closing unguarded f-string path construction
- **rsync filter-rule injection closed** — volume and archive names are validated before being interpolated into remote rsync `--include`/`--filter` rules in `volume_service` and `scheduler_service`
- **Notification template injection closed** — user-supplied message templates are rendered via allowlist token substitution instead of `str.format()`, so attribute-traversal payloads like `{hostname.__class__...}` are left literal
- **Notification config hardening** — `server_url` (http/https only), Telegram token, chat ID, and thread ID formats are validated; SSRF and malformed-endpoint values are rejected
- **Startup config validation** — `VOLUME_MANAGER_CONFIG` is now validated on boot (names, `pool_type`, port range, absolute paths, required remote-pool fields, unique pool names) and refuses to start with a clear `Invalid VOLUME_MANAGER_CONFIG: <field> — <reason>` message instead of failing later deep in a service
- **Frontend XSS surface eliminated** — dynamic `onclick="fn('${name}')"` handlers were replaced with `data-action` attributes and a single delegated click listener; all user-controlled names are HTML-escaped in both visible text and attributes, so a volume named with quotes or markup can no longer break out into executable JS

### Added

- **Richer notification messages** — the default Telegram template now mirrors the task details view: full source → destination target, start/finish timestamps, duration, host, and a `{params_block}` listing every task parameter. New template variables include `{target}`, `{params_block}`, `{started_at}`, `{task_id}`, `{current_operation}`, and per-field aliases (`{source_pool}`, `{dest_volume}`, `{backup_file}`, etc.)
- **Create and rename volume now appear as tasks** — `POST /api/volume/create` and `POST /api/rename` create tracked tasks (with progress and history entries) and trigger notifications under new `create` and `rename` topics, consistent with backup/migrate/restore/delete
- **Global exception handlers** — unhandled errors return a generic 500 (no traceback leaked to clients) while logging full context server-side; request-validation failures return concise 422s with the offending field and reason

### Changed

- **Stricter name acceptance** — creating or renaming volumes/pools through the API now rejects names containing spaces, slashes, `..`, or control characters. Existing directories with such names continue to list and function, but cannot be created or renamed via the API
- **Notification dispatch is task-driven** — all operations create tasks and notifications are derived directly from the completed task, removing the separate non-task notification path

## 0.1.1

### Fixed

- **Tonal buttons unreadable in light mode** — `.btn.tonal` now uses surface-aware tokens (`--md-surface-container-highest` / `--md-on-surface`) so buttons like Rename, + New Volume, Run, Edit, Test, Cancel, Back and Back are legible on light backgrounds; the header retains its white-on-dark appearance via a scoped `.header-tools .btn.tonal` override
- **Scheduled task pill indistinguishable from Backup in light mode** — added `--md-tertiary-container` / `--md-on-tertiary-container` tokens (violet range) in both light and dark themes; the Scheduled pill now uses these tokens instead of the secondary-container color which was nearly identical to primary-container in light mode

## 0.1.0

### Fixed

- **Backup of deleted volumes creates empty archive** — backup tasks now check that the source volume directory exists before invoking `tar`; if the volume has been deleted the task fails immediately with a clear error (`"Source volume 'X' not found in pool 'Y' — it may have been deleted"`) and no archive file is created
- **Schedule edit form hides deleted volumes** — when a scheduled volume no longer exists in its pool the edit form now shows it inline with a red ⚠ not found badge (still checked); a warning banner at the top of the form explains that unchecking and saving will remove it from the schedule

## 0.0.13

### Added

- **Telegram notifications** — new Settings → Notifications section; each configuration defines a bot token, chat ID, optional message thread ID (for topic groups), which event topics to watch (`schedule`, `backup`, `migrate`, `restore`, `delete`, `rename`), whether to alert on failures only, an optional custom server URL (for self-hosted Bot API), and an optional message template; multiple configurations can coexist; persisted to `config_dir/vshipper_notifications.json`
- **Notification test button** — "Test" button on each notification card sends a test message immediately via `POST /api/notifications/{id}/test`
- **Notification REST API** — 6 new endpoints: `GET /api/notifications`, `POST /api/notifications`, `PUT /api/notifications/{id}`, `DELETE /api/notifications/{id}`, `POST /api/notifications/{id}/toggle`, `POST /api/notifications/{id}/test`

### Fixed

- **Backup falsely reported as failed when archive was created** — `tar` exit code 1 indicates non-fatal warnings (file changed, socket ignored, etc.), not a real failure; the archive is valid and present; now only exit code 2+ is treated as a hard failure; all tar stderr lines are logged to the task log for visibility
- **Scheduled backup summary always showed 0 failed** — `backup_volume()` returns `False` on failure but the return value was ignored; the result was always recorded as `ok`; now checks the return value and surfaces the sub-task error message in the summary log

## 0.0.12

### Added

- **Task type pill** — each task card in the Tasks panel now displays a colored pill badge showing the task type (`Backup`, `Scheduled`, `Migrate`, `Restore`, `Delete`, etc.) for instant visual differentiation; pill also appears in the task detail modal header
- **Create volume** — "+ New Volume" button on local Docker pool volume lists; creates a new directory with 777 permissions via `POST /api/volume/create`
- **Rename volume** — "Rename" button per volume in local Docker pools; opens an inline modal to enter a new name, calls the existing `POST /api/rename` endpoint
- **Backup archive grouping** — backup pool view now parses archive filenames (`{pool}_{volume}_{YYYYMMDD}_{HHMMSS}.tar.gz`) and groups archives by source volume; each group is displayed as a card matching the volume item style, with parsed timestamps and sizes; unparseable filenames fall back to a flat "Other" group
- **Task start/finish timestamps** — `started_at` and `completed_at` are now included in task API responses and displayed in the task detail panel

### Changed

- **Task list limit raised to 1000** — Tasks panel now holds up to 1000 entries paginated 100 per page with prev/next controls; previously capped at 10
- **All task parameters shown in detail** — task detail panel now renders every parameter from the task JSON, not just a fixed whitelist; unknown keys are auto-formatted as title case
- **Elapsed time is now decimal** — running task elapsed time shows sub-second precision (e.g. `0.3s`, `2.1s`) instead of integer seconds
- **Refresh also refreshes tasks** — clicking the 🔄 refresh button now also reloads the task history list
- **Pool stats** — local pool cards now show Used + Free space (not just usage %); remote pool cards show only Used space (Free N/A is hidden)

### Fixed

- **"Processing…" stuck on delete tasks** — completed delete tasks no longer show "Processing…" as the operation label; the operation line is now omitted when no current operation is set
- **"Started: —" in task detail** — task start and completion timestamps were missing from API responses; now correctly populated and formatted as `DD/MM/YYYY HH:MM:SS`

## 0.0.11

### Added

- **Backup scheduling** — new APScheduler-backed cron job system (`scheduler_service.py`); Settings → Schedules section lets you create jobs that define which volumes to back up, which backup pool to use, a cron expression, and a retention count; jobs run sequentially per volume, skip locked volumes, and produce a summary task plus individual sub-tasks visible in the Tasks panel
- **`config_dir` config option** — new YAML key (`default: /config`) that stores persistent config files (`config.yaml`, `vshipper_tasks.json`, `vshipper_schedules.json`) separately from ephemeral tmp data; locks and staging remain in `tmp_dir`
- **Remote pool retention** — backup retention now works for remote rsync daemon pools: archives are listed via `rsync --list-only` and each archive to delete is removed using the rsync filter+delete trick (sync empty dir with file-specific include/exclude and `--delete`) so only targeted archives are removed
- **Schedule REST API** — 6 new endpoints: `GET /api/schedules`, `POST /api/schedules`, `PUT /api/schedules/{id}`, `DELETE /api/schedules/{id}`, `POST /api/schedules/{id}/toggle`, `POST /api/schedules/{id}/run`

### Changed

- **Task and schedule state moved to `config_dir`** — `vshipper_tasks.json` and `vshipper_schedules.json` are now stored in the config directory instead of `tmp_dir`; recommended to mount a persistent volume at `config_dir` to survive container restarts

## 0.0.10

### Added

- **Settings page** — new ⚙️ Settings button in the header opens a full-screen overlay with a two-panel layout (left nav, right content); contains Appearance (theme selector), Maintenance (cleanup), and About (version + GitHub link) sections; replaces the header Cleanup and Theme toggle buttons
- **Mobile tab view** — on screens ≤768px the three panels (Pools / Volumes / Tasks) are now shown as a tabbed interface instead of stacking vertically; selecting a pool automatically switches to the Volumes tab

### Changed

- **Header layout** — brand anchors to the left with `flex: 1`; Settings, Refresh, user label, and Logout sit flush to the right; mobile header collapses to a single row (no second tools row)
- **Backup archive names prefixed with source pool** — archives are now named `{pool}_{volume}_{timestamp}.tar.gz` (e.g. `local_appwrite_20260611_143201.tar.gz`) so the origin pool is always clear in a shared backup destination
- **Restore modal default volume name** — strips the pool prefix from the archive filename so the suggested restore target is just the volume name (e.g. suggests `appwrite` not `local_appwrite`)

## 0.0.9

### Added

- **Task detail modal** — clicking any task card opens a detail view showing task parameters, elapsed time, and captured log output; live-polls while the task is running
- **Per-task log capture** — `TaskQueue` installs a stdout interceptor on startup that routes `[TASK:id]` prefixed print lines into an in-memory buffer per task; multiline messages (e.g. rsync error blocks) are fully captured as continuation lines in the same write call
- **`GET /api/task/{task_id}/logs` endpoint** — returns captured log lines for a task; used by the task detail modal
- **Conflict resolution on migrate/restore** — when the destination volume already exists, a modal prompts the user to choose: overwrite (rsync `--delete` to completely replace), merge (add/update files, keep extras), rename to a new volume name, or abort; closing the modal also aborts
- **Live rsync progress for backup/restore** — all rsync transfers in `backup_service.py` now stream output line-by-line; `--progress` flag added so per-file transfer rate and percentage appear in the task log and update the progress bar in real time

### Fixed

- **Remote pools showed "⚠ Unreachable"** — `list_pools` built pool dicts without `remote_host` and `rsync_module`, causing `_build_rsync_target` to raise `ValueError` that was silently caught as `reachable=False`; both fields are now forwarded for docker hosts and backup pools
- **Remote docker pool size showed 0 GB** — `_get_remote_pool_total_size` used a non-recursive rsync listing; volume pools have only directories at the module root so file sizes summed to zero; now uses `recursive=True`
- **Created date missing for remote volumes** — `_parse_rsync_list_line` always returned `None` for `created_timestamp`; now parses the date and time fields from rsync `--list-only` output
- **Multiline log messages truncated to first line** — `_TaskLogCapture.write()` split on newlines but only captured lines matching `[TASK:xxx]`; continuation lines within the same write call are now attributed to the same task
- **Restore timed out on large archives** — `communicate(timeout=600)` hard-killed rsync after 10 minutes even when transfer was still in progress; replaced with unbounded line-by-line streaming reads across all rsync calls in the backup service
- **Restore task showed "pending" during long remote downloads** — `start_task` was called only after the remote pull completed; now called at the start so the task shows as running and progress is visible from the first byte

### Changed

- **Backup archive filenames shortened** — removed the redundant word "backup": `volname_backup_YYYYMMDD_HHMMSS.tar.gz` → `volname_YYYYMMDD_HHMMSS.tar.gz`
- **Task card no longer shows error message** — error details are now shown in the task detail modal; the card shows only status chip, type, target, and progress bar
- **UI redesigned with Material Design 3** — CSS custom-property color token system (light/dark), themed scrollbars, 2-row mobile header (brand + user on row 1, tools on row 2)
- **Task history labels show pool context** — labels now read `source_pool/volume → dest_pool` instead of just the volume name
- **Dates displayed as DD/MM/YYYY** — replaced locale-dependent `toLocaleDateString()` with a consistent `formatDate()` helper throughout the UI
- **Favicon added** — Docker icon shown in browser tab

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
