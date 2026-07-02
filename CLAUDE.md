# v-shipper

Docker volume migration and backup tool. FastAPI + vanilla JS SPA. Stateless, containerized.

## Run dev server

```bash
bash run_dev.sh   # starts uvicorn on :8000, reloads only on app/ changes
# login: admin / admin
```

Test volumes live at `/Users/zero/Files/Repos/_temp/`. Staging dir for remote backup ops: `_temp/staging`.

## Tech stack

- Python 3.11 / FastAPI / uvicorn (ASGI)
- Alpine 3.20 Docker image (multi-platform via GHCR)
- Frontend: vanilla JS + HTML — no build step, no framework
- Config: single `VOLUME_MANAGER_CONFIG` env var (multiline YAML)

## Key files

| Path | Role |
|------|------|
| `app/app.py` | FastAPI setup, startup cleanup, static/template mounting |
| `app/config.py` | YAML config load, base64 password decode |
| `app/models.py` | Pydantic request/response models |
| `app/api/routes.py` | All REST endpoints |
| `app/services/volume_service.py` | Volume discovery, disk stats, rename/delete, permissions (chmod/chown) |
| `app/services/docker_service.py` | Docker socket client — maps volumes to the containers using them; stops/starts containers (local) |
| `app/services/container_control.py` | Stop/start the containers using a volume around an operation — dispatches local (docker_service) vs remote (v-helper API) |
| `app/services/remote_api_client.py` | HTTP client for the v-helper control API (fs/docker control + rsync-pull job start/poll) |
| `app/services/migration_service.py` | rsync orchestration, lockfiles; remote→remote via destination v-helper pull |
| `app/services/backup_service.py` | tar archiving, remote restore via staging |
| `app/services/task_queue.py` | Single-worker FIFO queue (serial execution), progress tracking, crash recovery, per-task log capture |
| `app/services/bulk_service.py` | Runs a set of single-item ops sequentially under one summary task (mirrors scheduled-backup grouping) |
| `app/services/scheduler_service.py` | APScheduler cron backup jobs, retention (local + remote), singleton |
| `app/templates/index.html` | SPA shell |
| `app/static/main.js` | All client logic — polling, modals, progress |
| `app/static/style.css` | Styling |
| `app/validation.py` | Shared input validators — names, paths, `safe_join`, cron, notification fields |
| `run_dev.sh` | Dev server launcher (includes real config) |

## Architecture patterns

- **Sequential ops only** — a single worker thread in `task_queue.py` drains an in-memory FIFO; endpoints/scheduler/bulk `submit()` a task and it waits as "pending" until its turn. One migration/backup/etc. runs at a time. Scheduled and bulk runs are one summary task whose sub-tasks run synchronously inside it (they do not re-enter the queue)
- **Lockfiles** at `{tmp_dir}/locks/<pool>_<volume>.lock` — cooperative exclusive locks, always wrap in try/finally
- **Progress** stored in-memory, polled by frontend every 2s via `GET /api/task/<id>/progress`
- **Task persistence** to `{config_dir}/vshipper_tasks.json` — incomplete tasks → marked failed on restart
- **Scheduler** in `scheduler_service.py` — APScheduler `BackgroundScheduler`, jobs persisted to `{config_dir}/vshipper_schedules.json`. Cron is interpreted in the container's `TZ` env var (falls back to host local zone, then UTC). Triggers are built via `make_cron_trigger` in `validation.py` (the shared parser used by both `validate_cron` and the scheduler) — it fixes crontab day-of-week numbering (`0`/`7`=Sun, `1`=Mon), which APScheduler's `from_crontab` gets wrong for numeric weekdays
- **Remote pools** are rsync daemon targets (not SSH, not mounted FS); local pools are direct paths
- **Remote→remote migration** — rsync refuses daemon-to-daemon transfers, so when both pools are remote and the destination has a v-helper API, `migration_service` calls `remote_api_client.rsync_pull` on the destination (it runs an rsync *client* pulling from the source's module) and polls `rsync_job_log` for progress/logs. Falls back to direct rsync (which surfaces the "cannot both be remote" error) when the destination has no v-helper or one too old to expose `/rsync/pull` (404)
- **Log to stdout** with `print(..., flush=True)` — prefix `[TASK:id]` for task logs; a stdout interceptor in `task_queue.py` captures these into an in-memory per-task buffer, retrievable via `GET /api/task/<id>/logs`

## Adding a new operation

1. Model in `app/models.py` — add `field_validator`s using the helpers in `app/validation.py` (`validate_name`, `validate_backup_file`, etc.)
2. Service logic in `app/services/` — build any filesystem paths with `safe_join` from `app/validation.py`
3. Endpoint in `app/api/routes.py` (follow auth pattern: check `session.get("authenticated")`; validate path/query params via `_validate_param` / `_validate_task_id`)
4. HTML/JS in `index.html` + `main.js` (modal + poll pattern; wire buttons via `data-action` + the delegated listener, never inline `onclick` with interpolated user data; escape names with `escapeHtml`)
5. Lock the volume, update progress, clean up in finally

## Input validation & safety

- **All inputs validated at the boundary** — request models, path/query params, and `VOLUME_MANAGER_CONFIG` (at startup) go through `app/validation.py`. Names are strict Docker-style (`[A-Za-z0-9][A-Za-z0-9_.-]`, ≤255, no `/` or `..`)
- **Path containment** — `safe_join(base, *parts)` resolves and asserts the result stays inside `base`; use it instead of f-string path building
- **Errors never crash the app** — global handlers in `app.py` return generic 500s (no traceback leak) and concise 422s; background-thread work is wrapped so a bad input marks the task failed
- **Frontend** — user-controlled strings are HTML-escaped and actions dispatched via `data-action` attributes, not interpolated `onclick`

## Config structure

```yaml
docker_hosts:
  - name: local
    pool: /path/to/volumes
    pool_type: local          # or remote (rsync daemon)
    remote_host: host:port    # remote only
    rsync_module: module      # remote only
    api_host: host:port       # optional v-helper control API
    api_key: secret           # required if api_host set
    docker_socket: true       # optional: report containers using each volume
    docker_host_path: /var/docker-volumes  # optional: host path volumes really live at (defaults to pool)
    container_stop_timeout: 120   # optional: grace period (s) before SIGKILL when stopping a container (default 120)

backup_pools:
  - name: backup
    pool: /path/to/backups
    pool_type: local

tmp_dir: /tmp                 # base dir for locks and staging (default: /tmp)
staging_dir: /tmp/staging     # override staging path; defaults to {tmp_dir}/staging
config_dir: /config           # persistent dir for config.yaml, tasks, schedules (default: /config)

web_ui:
  port: 8000
  admin_user: admin
  admin_password: YWRtaW4=   # base64("admin")
```

## Known issues / vibe-code debt

- Frontend JS in `main.js` is long and procedural — no modules, no component abstraction
- No tests exist despite `tests/` stubs referenced in older docs
- Per-task log buffer is in-memory only — logs are lost on server restart
