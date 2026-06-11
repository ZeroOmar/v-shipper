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
| `app/services/volume_service.py` | Volume discovery, disk stats, rename/delete |
| `app/services/migration_service.py` | rsync orchestration, lockfiles |
| `app/services/backup_service.py` | tar archiving, remote restore via staging |
| `app/services/task_queue.py` | Sequential queue, progress tracking, crash recovery, per-task log capture |
| `app/templates/index.html` | SPA shell |
| `app/static/main.js` | All client logic — polling, modals, progress |
| `app/static/style.css` | Styling |
| `run_dev.sh` | Dev server launcher (includes real config) |

## Architecture patterns

- **Sequential ops only** — one migration/backup at a time; tasks queue in memory
- **Lockfiles** at `/tmp/locks/<pool>_<volume>.lock` — cooperative exclusive locks, always wrap in try/finally
- **Progress** stored in-memory, polled by frontend every 2s via `GET /api/task/<id>/progress`
- **Task persistence** to `/tmp/vshipper_tasks.json` — incomplete tasks → marked failed on restart
- **Remote pools** are rsync daemon targets (not SSH, not mounted FS); local pools are direct paths
- **Log to stdout** with `print(..., flush=True)` — prefix `[TASK:id]` for task logs; a stdout interceptor in `task_queue.py` captures these into an in-memory per-task buffer, retrievable via `GET /api/task/<id>/logs`

## Adding a new operation

1. Model in `app/models.py`
2. Service logic in `app/services/`
3. Endpoint in `app/api/routes.py` (follow auth pattern: check `session.get("authenticated")`)
4. HTML/JS in `index.html` + `main.js` (follow modal + poll pattern)
5. Lock the volume, update progress, clean up in finally

## Config structure

```yaml
docker_hosts:
  - name: local
    pool: /path/to/volumes
    pool_type: local          # or remote (rsync daemon)
    remote_host: host:port    # remote only
    rsync_module: module      # remote only

backup_pools:
  - name: backup
    pool: /path/to/backups
    pool_type: local

tmp_dir: /tmp                 # base dir for locks, task state, and staging (default: /tmp)
staging_dir: /tmp/staging     # override staging path; defaults to {tmp_dir}/staging

web_ui:
  port: 8000
  admin_user: admin
  admin_password: YWRtaW4=   # base64("admin")
```

## Known issues / vibe-code debt

- Frontend JS in `main.js` is long and procedural — no modules, no component abstraction
- No tests exist despite `tests/` stubs referenced in older docs
- No input validation on volume/pool names (path traversal risk at boundaries)
- Per-task log buffer is in-memory only — logs are lost on server restart
