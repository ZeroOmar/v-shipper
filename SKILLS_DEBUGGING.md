---
description: Skills for debugging and troubleshooting v-shipper
---

# Skill: Debugging v-shipper

## Common Issues and Solutions

### Uvicorn Auto-reload Restarts During Operations

**Symptom**: Application restarts when rsync/tar writes files (Python files, especially)

**Debug Steps**:
1. Check if using uvicorn with `--reload`: `ps aux | grep uvicorn`
2. Look for "WatchFiles detected changes" in logs
3. Identify which files are triggering reload

**Common Causes**:
- Using `--reload` which watches all directories
- rsync/tar writes `.py` files and triggers uvicorn hot-reload
- Volume directories are in working directory root

**Fix**:
- Use provided `run_dev.sh` script which uses `--reload-dirs app` to only watch the app source directory
- OR move volume directories outside the project root
- In production, do not use `--reload` flag
```bash
# Development
bash run_dev.sh

# Production
python -m uvicorn app.app:app --port 8000
```

### Task State Lost After Restart

**Symptom**: Task progress shows 404 after container restarts, or tasks reset to pending

**Debug Steps**:
1. Check if `/tmp/vshipper_tasks.json` exists: `docker exec <container> ls -la /tmp/vshipper_tasks.json`
2. Verify task persistence is saving: Check container logs for "Saved tasks"
3. Test task creation and restart flow

**Common Causes**:
- `/tmp` is ephemeral in some containers (tasks file is deleted)
- Task state not saved on progress updates
- Application not loading persisted tasks on startup

**Fix**:
- Task state is now persisted to `/tmp/vshipper_tasks.json` and automatically marked as `failed` if the app restarts
- To make it more durable, mount a volume:
```bash
docker run -v /persistent/path:/tmp v-shipper:latest
```

### Orphaned Restore Temp Directories

**Symptom**: `.restore_temp_*` directories remain in pools after crash

**Debug Steps**:
1. Check for orphaned dirs: `docker exec <container> find /pools -name '.restore_temp_*' -type d`
2. Verify cleanup on startup: Look for "Cleaned up orphaned restore directory" in logs
3. Check if backup pool path is correct in config

**Common Causes**:
- Container crashed during restore operation
- Restore temp directory created but cleanup code not reached
- Config points to wrong pool path, cleanup doesn't find orphans

**Fix**:
- Application now automatically cleans up all `.restore_temp_*` directories on startup
- Verify pool paths in config match actual mounted volumes
- Check logs on startup for "Cleaned up orphaned restore directory" messages

### Rsync Failures

**Symptom**: Migration stops midway or reports permission denied

**Debug Steps**:
1. Check disk space: `docker exec <container> df -h /pools`
2. Check file permissions: `ls -la` on both source and destination
3. Verify rsync installed in image: `docker exec <container> rsync --version`
4. Check task error: `GET /api/task/<task_id>/progress` should show error message

**Common Causes**:
- Source/destination ownership mismatch
- Destination pool full or read-only
- SELinux/AppArmor permissions blocking rsync
- Rsync not in Alpine image

**Fix**:
```dockerfile
# In Dockerfile, ensure rsync and tar are installed
RUN apk add --no-cache rsync tar
```

### Volume Discovery Issues

**Symptom**: No volumes shown in UI, or "Pool not found" error

**Debug Steps**:
1. Verify pool directories exist: `docker exec <container> ls -la /pools/`
2. Check config parsing: Look for YAML parse errors in startup logs
3. Verify `VOLUME_MANAGER_CONFIG` env variable is set correctly (multiline YAML)
4. Test API: `curl http://localhost/api/pools`

**Common Causes**:
- YAML indentation error (use spaces, not tabs)
- Pool path doesn't exist in container
- Docker volume mount not specified in docker-compose.yml or Docker command
- Config env variable not formatted correctly

**Debug Config Loading** (`app/config.py`):
```python
# Add debug output at config load time
try:
    config = Config.from_env()
    print(f"Config loaded: {len(config.docker_hosts)} hosts, "
          f"{len(config.backup_pools)} backups", flush=True)
except Exception as e:
    print(f"Config load error: {e}", flush=True)
    raise
```

### Large Volume Loading Freezes UI

**Symptom**: Clicking on a pool with very large volumes locks up the browser

**Debug Steps**:
1. Check if volume sizes are being calculated: Look for "Calculating..." in UI
2. Verify background size calculation is running
3. Monitor CPU usage during load

**Common Causes**:
- First time calculating sizes for very large directories
- Background thread not started
- Frontend not polling for size updates

**Fix**:
- Sizes are now calculated asynchronously in the background
- Frontend shows "Calculating..." and polls every 2.5 seconds
- Large directories no longer freeze the UI on initial load
- Sizes are cached in memory for subsequent loads

### UI Not Loading or API 404

**Symptom**: Browser shows blank page or 404 on `/`

**Debug Steps**:
1. Check FastAPI app starting: `docker logs <container>` should show "listening on 0.0.0.0:8000"
2. Verify port exposed: `docker port <container>` should show mapping
3. Check templates exist: `docker exec <container> ls -la /app/templates/`
4. Check static files served: `curl http://localhost/static/main.js` should return JS
5. Browser console for JS errors: F12 → Console tab

**Common Causes**:
- Templates directory missing or not copied in Dockerfile
- FastAPI static files route not configured
- CORS issues if frontend on different port
- Port not exposed in docker-compose.yml

**Fix in Code** (`app/app.py`):
```python
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Serve index.html on root
@app.get("/", response_class=HTMLResponse)
async def root():
    with open("app/templates/index.html") as f:
        return f.read()
```

### Lockfile Issues

**Symptom**: Operations stuck "pending" or two operations run on same volume

**Debug Steps**:
1. Check lockfiles: `docker exec <container> ls -la /tmp/locks/`
2. Verify lockfile cleanup: Check if stale locks remain after completed tasks
3. Check task queue status: Look for lock creation/removal in logs

**Common Causes**:
- Task crashed before removing lockfile
- Multiple task queue instances (shouldn't happen in single container)
- Lockfile path mismatch between check and creation

**Fix** (`app/services/task_queue.py`):
```python
# Ensure cleanup on exception
def migrate_volume(task_id, ...):
    lock_file = self.task_queue.create_lockfile(pool, volume)
    try:
        # Do work
    except Exception as e:
        print(f"Task failed: {e}")
        raise
    finally:
        self.task_queue.remove_lockfile(lock_file)
```

### High Memory Usage or Slow Progress

**Symptom**: Container runs out of memory or progress polling timeout

**Debug Steps**:
1. Monitor memory: `docker stats <container>`
2. Check for large file buffering: Rsync should stream, not buffer entire files
3. Verify rsync `--progress` flag and progress parsing

**Common Causes**:
- Large migration buffering entire file in memory
- Progress polling interval too aggressive
- Background size calculation processing too many directories

**Fix**:
```python
# Ensure rsync streams files, doesn't buffer
cmd = "rsync -av --perms --preserve-times --group --owner --progress " \
      "--no-whole-file --inplace <src> <dst>"

# Frontend: adjust poll interval
const POLL_INTERVAL = 2000;  // 2 seconds, not 100ms
```

### Docker Image Too Large

**Symptom**: Image size >400MB or build fails

**Debug Steps**:
1. Check layers: `docker history <image> --no-trunc`
2. Identify large packages: Look for pip caches, old layer remnants
3. Verify multi-stage build used

**Common Causes**:
- Pip cache not cleared in Dockerfile
- Too many dependencies installed
- Base image not Alpine

**Fix in Dockerfile**:
```dockerfile
FROM python:3.11-alpine as builder
RUN apk add --no-cache --virtual .build-deps gcc musl-dev
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt
RUN apk del .build-deps

FROM alpine:3.20
COPY --from=builder /root/.local /root/.local
```

### Authentication Not Working

**Symptom**: Login always fails or "Not authenticated" on all endpoints

**Debug Steps**:
1. Verify admin password in config: Decode base64, verify value
2. Check session handling: Look for session cookie in browser DevTools
3. Test login endpoint manually: `curl -X POST http://localhost/api/login -d '{"username":"admin","password":"admin"}'`
4. Check CORS in DevTools Network tab

**Common Causes**:
- Password base64 encoded incorrectly in config
- Session cookie not set or expired
- CORS blocking session credentials

**Fix** (`app/api/routes.py`):
```python
@app.post("/api/login")
async def login(request: LoginRequest, response: Response):
    # Decode password from config
    config_password = base64.b64decode(config.web_ui.admin_password).decode()
    
    if request.username == config.web_ui.admin_user and \
       request.password == config_password:
        session_id = str(uuid.uuid4())
        sessions[session_id] = {"authenticated": True, "user": request.username}
        response.set_cookie("session_id", session_id, httponly=True, samesite='Lax')
        return {"status": "ok", "session_id": session_id}
    else:
        raise HTTPException(status_code=401, detail="Invalid credentials")
```

### Toast Notifications Not Appearing

**Symptom**: Error/success messages don't show, or show in wrong place

**Debug Steps**:
1. Check browser console for JS errors: F12 → Console
2. Verify toast container exists: `document.getElementById('toastContainer')`
3. Check CSS: Verify `.toast-container` and `.toast` styles are loaded
4. Monitor with DevTools: See if toast elements are created and removed

**Common Causes**:
- Toast container not in HTML
- CSS not loaded
- JavaScript error before toast creation
- Toast container hidden or off-screen

**Fix** (`app/templates/index.html`):
```html
<div id="toastContainer" class="toast-container"></div>
```

**And in CSS** (`app/static/style.css`):
```css
.toast-container {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 2000;
}
```

## Debugging Checklist
- [ ] Check Docker logs: `docker logs -f <container>`
- [ ] Verify mounts: `docker exec <container> ls -la /pools/`
- [ ] Decode base64 secrets: `echo <base64> | base64 -d`
- [ ] Check disk space: `df -h` inside and outside container
- [ ] Inspect config: `echo "$VOLUME_MANAGER_CONFIG"`
- [ ] Monitor resources: `docker stats <container>`
- [ ] Check browser console: F12 → Console tab for JS errors
- [ ] Test API manually: `curl http://localhost/api/pools`
- [ ] Verify lockfiles cleaned up: `ls -la /tmp/locks/`
- [ ] Check task persistence: `cat /tmp/vshipper_tasks.json`
- [ ] Verify orphaned temp dirs cleaned: Search for `.restore_temp_*`

## Logging Best Practices
- Always log to stdout: `print(..., flush=True)` or Python logging with stdout handler
- Include `[TASK:task_id]` prefix for task logs
- Use `[ERROR]`, `[WARNING]`, `[INFO]` prefixes for clarity
- Frontend console: `console.log()`, `console.error()`, `console.warn()`
- Include context: Task ID, pool name, volume name in log messages
- Use structured format for parsing: `"[TASK:task-123] Status: running (45%)"
- Log at operation start/end/error, not every iteration
- Example:
  ```python
  print(f"[TASK:{task_id}] Starting migration from {src_pool}/{src_vol} to {dst_pool}", flush=True)
  print(f"[TASK:{task_id}] Progress: 45% (1.2GB/2.4GB)", flush=True)
  print(f"[TASK:{task_id}] Completed with verification OK", flush=True)
  ```
