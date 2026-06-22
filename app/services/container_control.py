"""Stop/start the Docker containers that use a volume, around an operation.

Used by the volume operations (migrate/backup/permissions/rename/delete) and
scheduled backups to quiesce containers before touching a volume and restart
them afterward. Dispatches local pools to the Docker SDK (``docker_service``)
and remote pools to the v-helper control API (``remote_api_client``), reusing
the same volume→container mapping that powers the in-use badges.

``stop_running_containers`` raises on failure so the caller aborts the
operation; ``start_containers`` is best-effort and never raises, so containers
come back up even when the operation itself failed.
"""

from typing import Dict, List

from app.services.docker_service import get_docker_service
from app.services.remote_api_client import client_for_pool


def _is_remote(pool_cfg: dict) -> bool:
    return pool_cfg.get("pool_type") == "remote"


def _containers_using(pool_cfg: dict, volume_name: str) -> List[Dict]:
    """Return [{name, status}] for containers using *volume_name* in this pool."""
    if _is_remote(pool_cfg):
        api = client_for_pool(pool_cfg)
        if not api:
            return []
        return api.docker_users().get(volume_name, [])
    return get_docker_service().get_volume_container_map(pool_cfg, [volume_name]).get(volume_name, [])


def stop_running_containers(pool_cfg: dict, volume_name: str, task_id: str) -> List[str]:
    """Stop containers currently RUNNING on *volume_name*. Returns names stopped.

    No-op (returns []) when the pool has no docker_socket. Raises on any stop
    failure so the caller can abort the operation.
    """
    if not pool_cfg or not pool_cfg.get("docker_socket"):
        return []

    running = [c["name"] for c in _containers_using(pool_cfg, volume_name) if c.get("status") == "running"]
    if not running:
        return []

    timeout = int(pool_cfg.get("container_stop_timeout", 120) or 120)
    stopped: List[str] = []
    is_remote = _is_remote(pool_cfg)
    for name in running:
        print(f"[TASK:{task_id}] Stopping container '{name}' (waiting up to {timeout}s for graceful shutdown)", flush=True)
        if is_remote:
            client_for_pool(pool_cfg).stop_container(name, timeout=timeout)
        else:
            get_docker_service().stop_container(name, timeout=timeout)
        stopped.append(name)
        print(f"[TASK:{task_id}] Stopped container '{name}'", flush=True)
    return stopped


def start_containers(pool_cfg: dict, names: List[str], task_id: str) -> None:
    """Start each container in *names* back up. Best-effort: logs and swallows
    failures so a single container that won't start can't break the task."""
    if not names:
        return
    is_remote = _is_remote(pool_cfg)
    for name in names:
        try:
            print(f"[TASK:{task_id}] Starting container '{name}'", flush=True)
            if is_remote:
                client_for_pool(pool_cfg).start_container(name)
            else:
                get_docker_service().start_container(name)
        except Exception as e:
            print(f"[TASK:{task_id}] Warning: failed to start container '{name}': {e}", flush=True)
