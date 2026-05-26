---
description: Skills for debugging and troubleshooting v-shipper
---

# Skill: Debugging v-shipper

## Common Issues and Solutions

### SSH Connection Issues

**Symptom**: Remote pool migration fails with "Connection refused" or timeout

**Debug Steps**:
1. Check SSH credentials in config: decode base64 key, verify username/IP
2. Verify SSH service running on remote host: `ssh -i <key> user@host "echo ok"`
3. Check firewall/security groups allow port 22 from container IP
4. Check logs: `docker logs <container>` for paramiko errors
5. Test SSH tunnel manually: `ssh -i <decoded_key> admin@10.0.0.2`

**Common Causes**:
- SSH key permission too open (should be 600)
- Key file corrupt or invalid base64 encoding
- SSH user doesn't have read permissions on remote pool path
- SSH port not 22 (modify `ssh_service.py` SSH port constant)

**Fix in Code** (`app/services/ssh_service.py`):
```python
# Ensure SSH key file has correct permissions
os.chmod(key_file_path, 0o600)

# Add retry logic for transient failures
retry_count = 3
for attempt in range(retry_count):
    try:
        ssh.connect(...)
        break
    except Exception as e:
        if attempt < retry_count - 1:
            time.sleep(2 ** attempt)
        else:
            raise
```

### Rsync Failures

**Symptom**: Migration stops midway or reports permission denied

**Debug Steps**:
1. Check disk space: `docker exec <container> df -h /config/docker_pools`
2. Check file permissions: `ls -la` on both source and destination
3. Verify rsync installed in image: `docker exec <container> rsync --version`
4. Check rsync logs: Look for "Permission denied" or "No such file" in `/api/task/<task_id>/logs`

**Common Causes**:
- Source/destination ownership mismatch (rsync with `--owner` may fail)
- Destination pool full or read-only
- SELinux/AppArmor permissions blocking rsync
- Rsync not in Alpine image (must add to `requirements.txt` or Dockerfile)

**Fix**:
```dockerfile
# In Dockerfile, ensure rsync is installed
RUN apk add --no-cache rsync openssh-client
```

### Volume Discovery Issues

**Symptom**: No volumes shown in UI, or "Pool not found" error

**Debug Steps**:
1. Verify pool directories exist: `docker exec <container> ls -la /config/docker_pools/`
2. Check config parsing: Look for YAML parse errors in startup logs
3. Verify `VOLUME_MANAGER_CONFIG` env variable is set correctly (multiline YAML)
4. Check `/tmp/config.yaml` inside container: `docker exec <container> cat /tmp/config.yaml`

**Common Causes**:
- YAML indentation error (use spaces, not tabs)
- Pool path doesn't exist in container
- Docker volume mount not specified in `docker-compose.yml` or Docker command
- Config env variable not base64 multiline formatted correctly

**Debug Config Loading** (`app/config.py`):
```python
import sys
# Add debug output at config load time
try:
    config = Config.from_env()
    print(f"Config loaded: {len(config.docker_hosts)} hosts, "
          f"{len(config.backup_pools)} backups", file=sys.stderr)
except Exception as e:
    print(f"Config load error: {e}", file=sys.stderr)
    raise
```

### UI Not Loading or API 404

**Symptom**: Browser shows blank page or 404 on `/`

**Debug Steps**:
1. Check FastAPI app starting: `docker logs <container>` should show "Uvicorn running on 0.0.0.0:80"
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
3. Check task queue status: Look at in-memory state in logs

**Common Causes**:
- Task crashed before removing lockfile
- Multiple task queue instances (shouldn't happen in single container)
- Lockfile path mismatch between check and creation

**Fix** (`app/services/task_queue.py`):
```python
# Ensure cleanup on exception
def execute_task(task):
    lock_file = None
    try:
        lock_file = create_lockfile(task["volume"])
        # Do work
    except Exception as e:
        print(f"Task failed: {e}")
        raise
    finally:
        if lock_file and os.path.exists(lock_file):
            os.remove(lock_file)
```

### High Memory Usage or Slow Progress

**Symptom**: Container runs out of memory or progress polling timeout

**Debug Steps**:
1. Monitor memory: `docker stats <container>`
2. Check for large file in memory: Rsync should stream, not buffer entire files
3. Verify rsync `--progress` flag and progress parsing

**Common Causes**:
- Large migration buffering entire file in memory
- Progress polling interval too aggressive
- Memory leak in SSH connection pooling

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
3. Test login endpoint manually: `curl -X POST http://localhost/api/login -d '{"user":"admin","password":"pass"}'`

**Common Causes**:
- Password base64 encoded incorrectly in config
- Session cookie not set or expired
- CORS blocking session credentials

**Fix** (`app/api/routes.py`):
```python
@app.post("/api/login")
async def login(request: LoginRequest):
    # Decode password from config
    config_password = base64.b64decode(config.web_ui.admin_password).decode()
    
    if request.username == config.web_ui.admin_user and \
       request.password == config_password:
        session["authenticated"] = True
        return {"status": "ok"}
    else:
        raise HTTPException(status_code=401, detail="Invalid credentials")
```

## Debugging Checklist
- [ ] Check Docker logs: `docker logs -f <container>`
- [ ] Verify mounts: `docker exec <container> ls -la /config/`
- [ ] Decode base64 secrets: `echo <base64> | base64 -d`
- [ ] Test SSH manually: `ssh -i <key> user@host`
- [ ] Check disk space: `df -h` inside and outside container
- [ ] Inspect config: `docker exec <container> cat /tmp/config.yaml`
- [ ] Monitor resources: `docker stats <container>`
- [ ] Check browser console: F12 → Console tab for JS errors
- [ ] Test API manually: `curl http://localhost/api/pools`
- [ ] Verify lockfiles cleaned up: `ls -la /tmp/locks/`

## Logging Best Practices
- Always log to stdout: `print()` or `logging.StreamHandler(sys.stdout)`
- Include context: Task ID, pool name, volume name in log messages
- Use structured format for parsing: `"[TASK:task-123] Status: running (45%)"
- Log at operation start/end/error, not every iteration
- Example:
  ```python
  print(f"[TASK:{task_id}] Starting migration from {src_pool}/{src_vol} to {dst_pool}", flush=True)
  print(f"[TASK:{task_id}] Progress: 45% (1.2GB/2.4GB)", flush=True)
  print(f"[TASK:{task_id}] Completed with verification OK", flush=True)
  ```
