# v-shipper

Docker volume migration and backup tool with a web UI. Stateless, containerized, Alpine-based.

## Features

- **Web UI** — responsive dashboard with login, pool browser, and task history
- **Pool Management** — local pools and remote rsync daemon pools
- **Volume Migration** — rsync-based with permission preservation and optional verify + delete-source
- **Backup / Restore** — tar.gz archives to local or remote rsync backup pools
- **Exclusive Locks** — prevents concurrent operations on the same volume
- **Real-time Progress** — polling-based task progress with background size calculation
- **Crash Recovery** — persists task state; marks incomplete tasks failed on restart
- **Containerized** — Alpine Linux, ~350-400MB, multi-platform (amd64 + arm64)

## Configuration

All configuration is in a single `VOLUME_MANAGER_CONFIG` environment variable (multiline YAML):

```yaml
docker_hosts:
  - name: local-pool
    pool: /var/lib/docker/volumes
    pool_type: local

  - name: remote-nas
    pool: /            # placeholder; ignored for remote pools
    pool_type: remote
    remote_host: 10.0.0.5:873
    rsync_module: docker-volumes

backup_pools:
  - name: local-backup
    pool: /mnt/backups
    pool_type: local

  - name: remote-backup
    pool: /            # placeholder; ignored for remote pools
    pool_type: remote
    remote_host: 10.0.0.5:873
    rsync_module: docker-backup

tmp_dir: /tmp               # base dir for locks, task state, and staging (default: /tmp)
staging_dir: /tmp/staging   # override staging path (default: {tmp_dir}/staging)

web_ui:
  port: 8000
  admin_user: admin
  admin_password: YWRtaW4=  # base64("admin") — use a strong password in production
```

### Pool types

| `pool_type` | How it works |
|---|---|
| `local` | Direct filesystem access. `pool` is the absolute path. |
| `remote` | rsync daemon protocol. Requires `remote_host` (`host:port`) and `rsync_module`. `pool` is ignored. |

Remote pools must be accessible via the rsync daemon protocol (`rsync://host:port/module`). SSH is not supported. For NFS/CIFS-mounted paths, use `pool_type: local` with the mount path.

### Encoding the admin password

```bash
echo -n "yourpassword" | base64
```

## Quick start

### Local dev (Python)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Edit run_dev.sh with your pool paths, then:
bash run_dev.sh
```

Open http://localhost:8000 — default credentials: `admin` / `admin`.

### Docker Compose

```bash
docker-compose up
# UI at http://localhost:8000
```

### Docker (production)

```bash
docker run -d \
  --name v-shipper \
  -p 80:80 \
  -e VOLUME_MANAGER_CONFIG="$(cat config.yaml)" \
  -v /var/lib/docker/volumes:/mnt/docker_volumes:ro \
  -v /mnt/backups:/mnt/backups:rw \
  ghcr.io/<owner>/v-shipper:latest
```

## API

All endpoints require authentication (session cookie set at login).

### Auth
| Method | Path | Description |
|---|---|---|
| POST | `/api/login` | Login — returns session cookie |
| POST | `/api/logout` | Logout |

### Pools & volumes
| Method | Path | Description |
|---|---|---|
| GET | `/api/pools` | List all pools with disk stats |
| GET | `/api/volumes?pool=<name>` | List volumes in a pool |
| GET | `/api/volume/<pool>/<name>` | Volume detail |
| POST | `/api/rename` | Rename volume |
| POST | `/api/delete` | Delete volume (warns if no backup exists) |
| POST | `/api/pool/create` | Create new local pool directory |

### Operations
| Method | Path | Description |
|---|---|---|
| POST | `/api/migrate` | Start rsync migration |
| POST | `/api/backup` | Start backup to archive |
| POST | `/api/restore` | Restore from archive |

### Tasks
| Method | Path | Description |
|---|---|---|
| GET | `/api/task/<id>/progress` | Poll task progress |
| GET | `/api/tasks` | Task history |

### Utility
| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| POST | `/api/debug/cleanup` | Clear stale lock files and task state |

### Example — migrate via curl

```bash
# Login (saves session cookie)
curl -X POST http://localhost/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  -c cookies.txt

# List pools
curl -b cookies.txt http://localhost/api/pools

# Start migration
curl -X POST http://localhost/api/migrate \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -d '{"source_pool":"local-pool","source_volume":"myvolume","dest_pool":"remote-nas","verify":true,"delete_source":false}'

# Poll until done
curl -b cookies.txt http://localhost/api/task/<task_id>/progress
```

## Project structure

```
v-shipper/
├── app/
│   ├── app.py              # FastAPI entry point, lifespan, CORS
│   ├── config.py           # YAML config load, auth validation
│   ├── models.py           # Pydantic request/response models
│   ├── api/
│   │   └── routes.py       # All REST endpoints
│   └── services/
│       ├── volume_service.py    # Volume discovery, stats, rename/delete
│       ├── migration_service.py # rsync orchestration, lockfiles
│       ├── backup_service.py    # tar archiving, remote restore staging
│       ├── docker_service.py    # Docker SDK wrapper (minimal use)
│       └── task_queue.py        # Sequential queue, progress, crash recovery
├── app/templates/index.html     # SPA shell
├── app/static/
│   ├── main.js             # All client-side logic
│   └── style.css
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── run_dev.sh              # Dev server launcher (restricts reload to app/)
└── .github/workflows/
    └── docker-publish.yml  # Multi-platform build → GHCR
```

## Troubleshooting

**Login fails** — decode and verify the config password:
```bash
echo "YWRtaW4=" | base64 -d   # should print: admin
```

**Remote pool shows unreachable** — test the rsync daemon directly:
```bash
rsync --list-only rsync://host:873/module/
```

**Volumes not showing for remote docker host** — verify the rsync module exposes volume directories at the top level (not nested under a subdirectory).

**Task stuck pending / lock file stale** — use the 🧹 Cleanup button in the UI, or:
```bash
curl -X POST -b cookies.txt http://localhost/api/debug/cleanup
```

**Dev server restarts during operations** — always use `bash run_dev.sh` (limits file-watching to `app/`). Never run `uvicorn --reload` without `--reload-dir app` in dev.

## Releasing

The GitHub Actions workflow builds and pushes on tag:
```bash
git tag v0.0.7
git push origin v0.0.7
# → ghcr.io/<owner>/v-shipper:v0.0.7
```

## Limitations

- One operation at a time (sequential task queue by design)
- Single admin account (no RBAC)
- In-memory session store (cleared on restart)
- No backup rotation or scheduling
