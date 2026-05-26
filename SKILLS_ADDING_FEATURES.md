---
description: Skills for adding new features to v-shipper
---

# Skill: Adding Features to v-shipper

## When to Use
- Adding new volume operations (clone, snapshot, export, etc.)
- Adding new endpoints or UI screens
- Modifying migration/backup logic
- Extending authentication or authorization
- Adding scheduled tasks (cron-based backups)

## Workflow for Adding a New Feature

### 1. Data Model Definition
**File**: `app/models.py`
- Define request model (Pydantic BaseModel) for API input
- Define response model for API output
- Add to existing models or create new ones
- Example:
  ```python
  class VolumeCloneRequest(BaseModel):
      source_pool: str
      source_volume: str
      dest_pool: str
      dest_volume_name: str
  
  class TaskResponse(BaseModel):
      task_id: str
      status: str  # pending, running, completed, failed
      progress_percent: int
  ```

### 2. Service Layer Implementation
**Files**: `app/services/*.py`
- Implement business logic in appropriate service:
  - `volume_service.py` — volume metadata, discovery, rename/delete
  - `migration_service.py` — rsync, verification, lockfiles
  - `backup_service.py` — tar, checksums, archive operations
  - `ssh_service.py` — SSH-specific operations
  - `task_queue.py` — task scheduling and progress tracking
- Always:
  - Use lockfiles for exclusive operations (import from `migration_service`)
  - Log to stdout only (use `print()` or Python `logging` with StreamHandler)
  - Handle SSH tunneling via `ssh_service.execute_remote_command()` for remote pools
  - Return progress updates in standardized format

**Example**:
```python
# In app/services/volume_service.py
async def clone_volume(self, source_pool: str, source_volume: str, 
                       dest_pool: str, dest_name: str) -> str:
    """Clone a volume. Returns task_id."""
    task_id = self.task_queue.add_task(
        task_type="clone",
        source_pool=source_pool,
        source_volume=source_volume,
        dest_pool=dest_pool,
        dest_name=dest_name
    )
    
    def _clone_task():
        lock_file = self._create_lockfile(source_pool, source_volume)
        try:
            # Rsync clone logic
            cmd = f"rsync -av --perms --preserve-times {src_path} {dest_path}"
            # Execute and update progress
            self.task_queue.update_progress(task_id, {"status": "completed"})
        finally:
            self._remove_lockfile(lock_file)
    
    # Queue task (sequential)
    asyncio.create_task(_clone_task())
    return task_id
```

### 3. API Endpoint Definition
**File**: `app/api/routes.py`
- Add FastAPI route (GET, POST, etc.)
- Validate request model
- Call service layer
- Return response model
- Follow existing pattern (login check, error handling)

**Example**:
```python
@app.post("/api/clone", response_model=TaskResponse)
async def clone_volume(request: VolumeCloneRequest, session: dict = Depends(get_session)):
    """Start volume clone operation."""
    if not session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        task_id = await volume_service.clone_volume(
            request.source_pool, 
            request.source_volume,
            request.dest_pool, 
            request.dest_volume_name
        )
        return TaskResponse(task_id=task_id, status="pending", progress_percent=0)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
```

### 4. Frontend UI Update
**Files**: `app/templates/index.html` + `app/static/main.js`
- Add HTML button/form for new feature in `index.html`
- Add JavaScript handler in `main.js` to:
  - Collect user input
  - POST to new API endpoint
  - Poll `/api/task/<task_id>/progress` for updates
  - Display progress bar/modal
  - Show completion/error status

**Example (main.js)**:
```javascript
async function startClone() {
    const sourcePool = document.getElementById('sourcePool').value;
    const sourceVolume = document.getElementById('sourceVolume').value;
    const destPool = document.getElementById('destPool').value;
    const destName = document.getElementById('destName').value;
    
    const response = await fetch('/api/clone', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            source_pool: sourcePool,
            source_volume: sourceVolume,
            dest_pool: destPool,
            dest_volume_name: destName
        })
    });
    
    const data = await response.json();
    pollProgress(data.task_id);
}

function pollProgress(taskId) {
    const interval = setInterval(async () => {
        const response = await fetch(`/api/task/${taskId}/progress`);
        const data = await response.json();
        updateProgressUI(data);
        
        if (data.status === 'completed' || data.status === 'failed') {
            clearInterval(interval);
        }
    }, 1000);
}
```

### 5. Task Queue Integration
**File**: `app/services/task_queue.py`
- Register task type in queue if new operation type
- Ensure lockfile handling for exclusivity
- Update progress callback format for consistency

### 6. Testing
**Directory**: `tests/`
- Create unit test for service method
- Create integration test for full API flow
- Mock SSH/Docker operations if needed

**Example (test_volume_service.py)**:
```python
def test_clone_volume():
    result = volume_service.clone_volume("pool1", "vol1", "pool2", "vol1_clone")
    assert result is not None
    assert result.startswith("task_")
```

### 7. Update Documentation
- Update `.agents.md` with new endpoint/feature description
- Update README if user-facing behavior changed

## Common Patterns

### SSH Operations on Remote Pools
```python
# In service method
if pool_type == "remote":
    ssh_conn = ssh_service.connect(host, user, ssh_key)
    ssh_conn.exec_command(f"rsync -av /src /dst")
else:
    # Local operation
    subprocess.run(f"rsync -av /src /dst", shell=True)
```

### Lockfile Management
```python
lock_file = f"/tmp/locks/{pool}_{volume}.lock"
with open(lock_file, 'w') as f:
    f.write(str(os.getpid()))

try:
    # Do operation
    pass
finally:
    os.remove(lock_file)
```

### Progress Reporting
```python
progress = {
    "status": "running",
    "progress_percent": 45,
    "current_file": "large_file.tar.gz",
    "elapsed_seconds": 120,
    "estimated_remaining_seconds": 150
}
task_queue.update_progress(task_id, progress)
```

## Checklist
- [ ] Data models added to `app/models.py`
- [ ] Service logic in `app/services/`
- [ ] API endpoint in `app/api/routes.py`
- [ ] Frontend HTML/JS updated
- [ ] Task queue integration (if async)
- [ ] Lockfile handling (if exclusive operation)
- [ ] SSH tunnel support (if remote pool operation)
- [ ] Tests written
- [ ] Logs to stdout only
- [ ] Documentation updated
- [ ] Local docker-compose test passes
