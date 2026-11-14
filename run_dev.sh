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

tmp_dir: /Users/zero/Files/Repos/_temp/tmp
config_dir: /Users/zero/Files/Repos/_temp/config

web_ui:
  port: 8000
  admin_user: admin
  admin_password_hash: $2b$12$MM5LacQsZrUmhxkvqnOKpOtiO2aruAfw2M.B4tmbt.xnYMFAZsp7e  # bcrypt("admin")
'

venv/bin/python -m uvicorn app.app:app \
  --reload \
  --reload-dir app \
  --port 8000 \
  "$@"
