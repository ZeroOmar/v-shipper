#!/bin/bash
# Development server with excluded watch directories to prevent reload on volume changes

export VOLUME_MANAGER_CONFIG="${VOLUME_MANAGER_CONFIG:-}"

venv/bin/python -m uvicorn app.app:app \
  --reload \
  --reload-dirs app \
  --port 8000 \
  "$@"
