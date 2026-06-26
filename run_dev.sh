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
  # - name: remote1
  #   pool: /
  #   pool_type: remote
  #   rsync_module: docker-volumes
  #   remote_host: 10.0.13.116:873
  #   api_host: 10.0.13.116:8888
  #   api_key: 24e88cb9-efdc-44a9-b3b8-8d9107f380e9

backup_pools:
  - name: backup
    pool: /Users/zero/Files/Repos/_temp/test_backups
    pool_type: local
  - name: remotebackup
    pool: /
    pool_type: remote
    rsync_module: docker-backup
    remote_host: 10.0.13.21:30026

tmp_dir: /Users/zero/Files/Repos/_temp/tmp
config_dir: /Users/zero/Files/Repos/_temp/config

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
