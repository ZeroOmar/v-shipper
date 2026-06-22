"""Docker service for Docker operations."""

import docker
from typing import List, Optional, Dict


def host_path_for_volume(pool: dict, volume_name: str) -> str:
    """Host-namespace path for a volume folder.

    Docker reports mount sources in the host's namespace, which may differ from the
    path v-shipper sees. ``docker_host_path`` is the host base directory for the pool;
    when unset we assume the pool path is already the host path (identity).
    """
    base = (pool.get("docker_host_path") or pool.get("path") or "").rstrip("/")
    return f"{base}/{volume_name}"


class DockerService:
    """Service for Docker operations."""
    
    def __init__(self):
        try:
            self.client = docker.from_env()
        except Exception as e:
            print(f"[WARNING] Failed to connect to Docker daemon: {e}", flush=True)
            self.client = None
    
    def get_volumes(self) -> List[dict]:
        """Get list of Docker volumes."""
        if not self.client:
            return []
        
        try:
            volumes = []
            for volume in self.client.volumes.list():
                volumes.append({
                    "name": volume.name,
                    "driver": volume.driver,
                    "mountpoint": volume.attrs.get("Mountpoint")
                })
            return volumes
        except Exception as e:
            print(f"[ERROR] Failed to list Docker volumes: {e}", flush=True)
            return []
    
    def get_containers(self) -> List[dict]:
        """Get list of Docker containers."""
        if not self.client:
            return []
        
        try:
            containers = []
            for container in self.client.containers.list(all=True):
                containers.append({
                    "id": container.id,
                    "name": container.name,
                    "status": container.status
                })
            return containers
        except Exception as e:
            print(f"[ERROR] Failed to list Docker containers: {e}", flush=True)
            return []
    
    def get_volume_container_map(self, pool: dict, volume_names: List[str]) -> Dict[str, List[dict]]:
        """Map each volume to the containers using it: {volume: [{name, status}]}.

        A container uses a volume if any of its mount sources equals the volume's host
        path or sits under it (covers sub-folder bind mounts and local-driver volumes
        whose mountpoint lives there). One pass over the container list — the list API
        already includes ``Mounts``, so no per-container inspect is needed. Returns an
        empty map if the socket is unavailable, so callers never break.
        """
        result: Dict[str, List[dict]] = {name: [] for name in volume_names}
        if not self.client:
            return result

        try:
            # Snapshot each container's mount sources once.
            containers = []
            for container in self.client.containers.list(all=True):
                sources = [
                    m.get("Source") for m in (container.attrs.get("Mounts") or [])
                    if m.get("Source")
                ]
                containers.append({"name": container.name, "status": container.status, "sources": sources})

            for name in volume_names:
                host_path = host_path_for_volume(pool, name)
                prefix = host_path.rstrip("/") + "/"
                for c in containers:
                    if any(s == host_path or s.startswith(prefix) for s in c["sources"]):
                        result[name].append({"name": c["name"], "status": c["status"]})
        except Exception as e:
            print(f"[ERROR] Failed to map containers to volumes: {e}", flush=True)

        return result

    def is_healthy(self) -> bool:
        """Check if Docker daemon is accessible."""
        if not self.client:
            return False
        
        try:
            self.client.ping()
            return True
        except Exception:
            return False


# Global Docker service instance
_docker_service = None


def get_docker_service() -> DockerService:
    """Get Docker service instance."""
    global _docker_service
    if _docker_service is None:
        _docker_service = DockerService()
    return _docker_service
