"""Docker service for Docker operations."""

import docker
from typing import List, Optional


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
