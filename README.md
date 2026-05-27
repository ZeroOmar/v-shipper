# v-shipper: Docker Volume Migration Application

A stateless, containerized Python + FastAPI application for managing and migrating Docker volumes across multiple pools with a modern web UI.

## Features

- 🎯 **Web UI** - Modern, responsive dashboard with login authentication
- 📁 **Pool Management** - Support for local and remote mounted pools
- 🚀 **Volume Migration** - Rsync-based migration with permission preservation
- 💾 **Backup Operations** - Archive volumes to backup pools with verification
- 🔒 **Exclusive Locks** - Prevent concurrent operations on same volume
- 📊 **Disk Usage Stats** - Real-time pool utilization and available space
- 📈 **Progress Tracking** - Real-time progress updates for long-running operations with background size calculation
- 🐳 **Containerized** - Alpine Linux, minimal attack surface (~350-400MB)
- 📝 **Stateless** - No database, all state in-memory, clean restart
- 📤 **Logs to stdout** - All logs sent to stdout for Docker log collection
- 🔄 **Crash Recovery** - Persists task state and cleans up orphaned temp directories

## Requirements

- Docker & Docker Compose
- Python 3.11+ (for local development)
- `rsync` available on all hosts (for migrations)
- `tar` available (for backup/restore)

## Configuration

Configuration is provided via the `VOLUME_MANAGER_CONFIG` environment variable as multiline YAML:

```yaml
docker_hosts:
  - name: host1
    ip: 10.0.0.1
    pool: /var/lib/docker/volumes
    pool_type: local              # or 'remote' for NFS/mounted remote paths

backup_pools:
  - name: backup1
    path: /mnt/backups

web_ui:
  port: 8000
  admin_user: admin
  admin_password: YWRtaW4=       # base64 encoded password
```

**Note**: `pool_type: remote` is for NFS or other remote-mounted filesystems. SSH/remote access is no longer supported. Mount remote volumes using standard filesystem mount options and mark them as `remote` for UI labeling.

### Base64 Encoding

To encode secrets for the configuration:

```bash
# Encode password
echo -n "mysecurepassword" | base64

# Example: admin password "admin" encodes to "YWRtaW4="
```

## Quick Start

### Docker Compose (Local Development)

1. Clone repository
2. Create local directories for testing:
   ```bash
   mkdir -p test_volumes/host1 test_volumes/host2 test_backups
   ```

3. Start the application:
   ```bash
   docker-compose up
   ```

4. Access the web UI:
   - **URL**: http://localhost:8000
   - **Username**: admin
   - **Password**: admin (from docker-compose.yml)

### Local Development (Python)

1. Create virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. Set configuration:
   ```bash
   export VOLUME_MANAGER_CONFIG='
   docker_hosts:
     - name: local
       ip: localhost
       pool: ./test_volumes/host1
       pool_type: local
   backup_pools:
     - name: backup
       path: ./test_backups
   web_ui:
     port: 8000
     admin_user: admin
     admin_password: YWRtaW4=
   '
   ```

3. Run development server:
   ```bash
   # Using run_dev.sh to exclude volume directories from auto-reload
   bash run_dev.sh
   ```

4. Access at http://localhost:8000

### Docker (Production)

```bash
# Build image
docker build -t v-shipper:latest .

# Run container
docker run -d \
  --name v-shipper \
  -p 80:80 \
  -e "VOLUME_MANAGER_CONFIG=$(cat <<'EOF'
docker_hosts:
  - name: prod-pool
    ip: localhost
    pool: /mnt/docker_volumes
    pool_type: local
backup_pools:
  - name: prod-backup
    path: /mnt/backups
web_ui:
  port: 80
  admin_user: admin
  admin_password: $(echo -n "yourpassword" | base64)
EOF
)" \
  -v /var/lib/docker/volumes:/mnt/docker_volumes:ro \
  -v /mnt/backups:/mnt/backups:rw \
  v-shipper:latest
```

## API Endpoints

All endpoints require authentication via login.

### Authentication
- `POST /api/login` - Login with username/password
- `POST /api/logout` - Logout

### Pools
- `GET /api/pools` - List all pools with disk stats
- `GET /api/refresh` - Re-read pools from disk

### Volumes
- `GET /api/volumes?pool=<name>` - List volumes in pool
- `GET /api/volume/<pool>/<name>` - Get volume details
- `POST /api/rename` - Rename volume
- `POST /api/delete` - Delete volume

### Operations
- `POST /api/migrate` - Start volume migration
- `POST /api/backup` - Start volume backup
- `POST /api/pool/create` - Create new empty pool

### Tasks
- `GET /api/task/<task_id>/progress` - Get operation progress
- `GET /api/task/<task_id>/logs` - Get operation logs

### Health
- `GET /api/health` - Health check

## Usage

### Web UI Workflow

1. **Login** - Enter admin credentials
2. **View Pools** - See all pools with disk usage statistics
3. **Explore Volumes** - Click "View Volumes" to see volumes in a pool
4. **Migrate** - Select source volume, destination pool, verify settings
5. **Backup** - Create archive backup in backup pool
6. **Monitor Progress** - Real-time progress updates during operations
7. **Manage** - Rename, delete, or view detailed volume info

### Command Line (API)

```bash
# Login
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
  -d '{
    "source_pool":"local",
    "source_volume":"myvolume",
    "dest_pool":"backup",
    "verify":true,
    "delete_source":false
  }'

# Monitor progress
curl -b cookies.txt http://localhost/api/task/[task-id]/progress
```

## Security Considerations

### Development vs Production

**Development** (docker-compose):
- Credentials in plaintext (for testing only)
- No HTTPS
- File-based reload on all changes

**Production** (deployment):
- Use strong passwords, base64 encoded
- Deploy behind reverse proxy with HTTPS (nginx, Traefik)
- Restrict access to management endpoints
- Run on isolated network
- Enable SELinux/AppArmor if available

### Remote Pools

Remote pools are accessed via standard filesystem mount (NFS, CIFS, etc.), not SSH. Mount remote storage before starting the application:

```bash
# Example: NFS mount
mount -t nfs 10.0.0.1:/export/volumes /mnt/remote_volumes

# Then reference in config
docker_hosts:
  - name: remote-nfs
    ip: 10.0.0.1
    pool: /mnt/remote_volumes
    pool_type: remote    # UI label only; works like local pools
```

## Performance Notes

### Async Volume Size Calculation

When listing volumes in a pool, if directory sizes are not yet cached, the API returns immediately with `size_loading: true` for those volumes. A background thread then calculates directory sizes in parallel. The frontend polls `/api/volumes` again while sizes are being computed, showing "Calculating..." in the UI until complete.

This prevents the initial pool load from freezing on large volumes.

### Development Mode

When running locally with `bash run_dev.sh`, the uvicorn server only watches changes in the `app/` directory. This prevents auto-reload when rsync/tar operations write files to volume directories.

### Crash Recovery

The application automatically:
1. **Persists task state** to `/tmp/vshipper_tasks.json` so progress can be recovered across restarts
2. **Marks incomplete tasks as failed** with a clear error message if the server was restarted mid-operation
3. **Cleans up orphaned `.restore_temp_*` directories** from failed restore operations on startup
- Keep logs securely stored

### Best Practices

1. **SSH Keys**: Store SSH private keys securely, use dedicated key for each host
2. **Passwords**: Use strong passwords, rotate regularly
3. **Backups**: Verify backup integrity, test restore procedures
4. **Monitoring**: Monitor logs for errors, set up alerts
5. **Access Control**: Limit container access to trusted networks
6. **Storage**: Use encrypted filesystems for sensitive volumes

## Troubleshooting

### Login Fails

Check credentials in config, verify password is correctly base64 encoded:
```bash
docker logs v-shipper | grep -i "login\|auth"
```

### Volume Discovery Issues

Verify pool directories exist and are mounted correctly:
```bash
docker exec v-shipper ls -la /mnt/pools/
```

### SSH Connection Errors

Test SSH connectivity manually:
```bash
ssh -i /path/to/key admin@host-ip
```

### Migration Hangs

Check disk space on destination pool:
```bash
docker exec v-shipper df -h /mnt/
```

See [SKILLS_DEBUGGING.md](SKILLS_DEBUGGING.md) for comprehensive troubleshooting guide.

## Development

### Project Structure

```
v-shipper/
├── app/
│   ├── app.py              # FastAPI entry point
│   ├── config.py           # Configuration loading
│   ├── models.py           # Pydantic data models
│   ├── api/
│   │   └── routes.py       # API endpoints
│   ├── services/
│   │   ├── volume_service.py
│   │   ├── migration_service.py
│   │   ├── backup_service.py
│   │   ├── ssh_service.py
│   │   ├── docker_service.py
│   │   └── task_queue.py
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── style.css
│       └── main.js
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .dockerignore
├── .agents.md               # Agent context for development
├── SKILLS_ADDING_FEATURES.md
├── SKILLS_DEBUGGING.md
└── README.md
```

### Local Development

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set config environment variable
export VOLUME_MANAGER_CONFIG='
docker_hosts:
  - name: local
    ip: localhost
    pool: ./test_volumes/host1
    pool_type: local
  - name: local2
    ip: localhost
    pool: ./test_volumes/host2
    pool_type: local
backup_pools:
  - name: backup
    path: ./test_backups
web_ui:
  port: 8000
  admin_user: admin
  admin_password: YWRtaW4=
'

# Run application
python3 -m uvicorn app.app:app --reload --port 8000
```

### Adding Features

See [SKILLS_ADDING_FEATURES.md](SKILLS_ADDING_FEATURES.md) for workflow on adding new features.

## GitHub Actions

The repository includes a GitHub Actions workflow (`.github/workflows/docker-publish.yml`) that:
- Builds multi-platform Docker images (linux/amd64, linux/arm64)
- Publishes to GitHub Container Registry (GHCR)
- Signs images with Cosign
- Runs on push to main and on version tags

### Releasing

```bash
# Create version tag
git tag v1.0.1
git push origin v1.0.1

# Image automatically published to: ghcr.io/owner/v-shipper:v1.0.1
```

## Performance Considerations

- **Sequential Operations**: One migration/backup at a time (by design)
- **Memory**: In-memory task queue, suitable for single-container deployment
- **Disk I/O**: Rsync streams data, doesn't buffer entire files
- **Network**: SSH adds latency for remote operations, typical 10-50ms

## Limitations

- Sequential operations only (no parallel migrations)
- Session memory lost on container restart
- SSH key in environment variable (use Docker Secrets for production)
- No built-in backup rotation (manual cleanup)
- No multi-user RBAC (single admin account)

## Future Enhancements

- [ ] Parallel operation support with resource limits
- [ ] Backup retention policies and auto-cleanup
- [ ] WebSocket for real-time progress (vs polling)
- [ ] Metrics export (Prometheus format)
- [ ] Multi-user RBAC system
- [ ] Volume snapshots and cloning
- [ ] Scheduled backups via APScheduler
- [ ] S3/object storage backup destination

## License

MIT

## Support

For issues and questions:
1. Check [SKILLS_DEBUGGING.md](SKILLS_DEBUGGING.md) for troubleshooting
2. Review `.agents.md` for architecture details
3. Check logs: `docker logs v-shipper`
4. Open GitHub issue with configuration (sanitized) and error logs

