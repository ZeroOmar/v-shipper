Debug v-shipper issues. Check the most common failure points.

## Quick checks

```bash
# Is the server running?
curl -s http://localhost:8000/api/health

# Check all pools are reachable
curl -s -b 'session_id=<id>' http://localhost:8000/api/pools | python3 -m json.tool

# Check active lockfiles (stuck operations)
ls -la /tmp/locks/ 2>/dev/null || echo "No locks dir"

# Check task state file
cat /tmp/vshipper_tasks.json 2>/dev/null | python3 -m json.tool

# Check for orphaned restore dirs
find /Users/zero/Files/Repos/_temp -name '.restore_temp_*' -type d 2>/dev/null
```

## Common issues

**Server restarts mid-operation** — make sure you used `bash run_dev.sh` not `uvicorn --reload` without `--reload-dir app`. The dev script limits reloading to the `app/` directory only.

**Volume not showing** — check the pool path exists and is readable:
```bash
ls -la /Users/zero/Files/Repos/_temp/test_volumes/host1/
```

**Task stuck in pending** — stale lockfile. Delete it:
```bash
rm /tmp/locks/<pool>_<volume>.lock
```

**Login always fails** — decode the config password:
```bash
echo "YWRtaW4=" | base64 -d   # should print: admin
```

**Config parse error** — validate YAML:
```bash
python3 -c "import yaml, os; yaml.safe_load(os.environ.get('VOLUME_MANAGER_CONFIG', ''))"
```

## Log reading

All app logs go to stdout. Task logs are prefixed `[TASK:id]`. Look for `[ERROR]` prefix for failures.

When debugging a specific task, grep the uvicorn output:
```bash
grep "TASK:your-task-id" <logfile>
```
