Launch the v-shipper dev server and open the UI.

```bash
bash run_dev.sh
```

The server starts on http://localhost:8000. Login: admin / admin.

Test volumes are at `/Users/zero/Files/Repos/_temp/test_volumes/` (host1, host2) and backups at `_temp/test_backups/`.

To create test volumes for migration testing:
```bash
mkdir -p /Users/zero/Files/Repos/_temp/test_volumes/host1/myvolume/_data
echo "test" > /Users/zero/Files/Repos/_temp/test_volumes/host1/myvolume/_data/file.txt
```

After starting, verify the API is up:
```bash
curl -s http://localhost:8000/api/health
```
