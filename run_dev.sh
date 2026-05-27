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
  - name: remote1
    pool: /Volumes/docker-volumes/
    pool_type: remote

backup_pools:
  - name: backup
    path: /Users/zero/Files/Repos/_temp/test_backups
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
