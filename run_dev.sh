#!/bin/bash
# Development server with excluded watch directories to prevent reload on volume changes

export VOLUME_MANAGER_CONFIG='
docker_hosts:
  - name: local
    pool: /Users/zero/Files/Repos/_temp/test_volumes/host1
    pool_type: local
  - name: local2
    pool: /Users/zero/Files/Repos/_temp/test_volumes/host2
    pool_type: local

backup_pools:
  - name: backup
    pool: /Users/zero/Files/Repos/_temp/test_backups
    pool_type: local
  - name: remotebackup
    pool: /
    pool_type: remote
    rsync_module: docker-backup
    remote_host: 10.0.13.21:30026

staging_dir: /Users/zero/Files/Repos/_temp/staging

web_ui:
  port: 8000
  admin_user: admin
  admin_password: YWRtaW4=
'

venv/bin/python -m uvicorn app.app:app \
  --reload \
  --reload-dir app \
  --port 8000 \
  "$@"
