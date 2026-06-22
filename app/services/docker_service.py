"""Docker service for Docker operations."""

import docker
from typing import List, Optional, Dict


def candidate_bases(pool: dict) -> List[str]:
    """Base directories a volume folder may appear under in the Docker namespace.

    Docker reports mount sources / volume devices in the host's namespace, which may
    differ from the path v-shipper sees. We match against both ``docker_host_path``
    (the real host base, e.g. a named volume's ``driver_opts: device`` base) and the
    pool's own path (for containers that bind the path v-shipper sees directly). ``/``
    and empty bases are skipped so we never match every path.
    """
    bases: List[str] = []
    for b in (pool.get("docker_host_path"), pool.get("path")):
        if b and b != "/":
            b = b.rstrip("/")
            if b and b not in bases:
                bases.append(b)
    return bases


def host_path_for_volume(pool: dict, volume_name: str) -> str:
    """Primary host-namespace path for a volume folder (docker_host_path or pool path)."""
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
    
    def _volume_real_paths(self) -> Dict[str, str]:
        """Map each Docker named volume to its real host path.

        For local-driver bind volumes (``driver_opts: {o: bind, device: /path}``) the
        real path is ``Options.device``; otherwise it's the managed ``Mountpoint``. This
        is what lets us match named volumes — a container's volume mount reports
        ``Source`` as the managed mountpoint (``/var/lib/docker/volumes/<v>/_data``), not
        the device path, so without this resolution bind-backed named volumes never match.
        """
        real: Dict[str, str] = {}
        try:
            for v in self.client.volumes.list():
                attrs = v.attrs or {}
                opts = attrs.get("Options") or {}
                real[v.name] = opts.get("device") or attrs.get("Mountpoint")
        except Exception as e:
            print(f"[WARNING] Failed to list Docker volumes for mapping: {e}", flush=True)
        return real

    def get_volume_container_map(self, pool: dict, volume_names: List[str]) -> Dict[str, List[dict]]:
        """Map each volume to the containers using it: {volume: [{name, status}]}.

        A container uses a volume if any of its mount host-paths equals the volume's host
        path or sits under it (covers sub-folder bind mounts and bind-backed named
        volumes). Bind mounts use ``Source`` directly; named-volume mounts are resolved
        to their real device/mountpoint via ``_volume_real_paths``. Returns an empty map
        if the socket is unavailable, so callers never break.
        """
        result: Dict[str, List[dict]] = {name: [] for name in volume_names}
        if not self.client:
            return result

        try:
            bases = candidate_bases(pool)
            if not bases:
                return result
            vol_real = self._volume_real_paths()

            # Snapshot each container's effective mount host-paths once.
            containers = []
            for container in self.client.containers.list(all=True):
                sources = []
                for m in (container.attrs.get("Mounts") or []):
                    if m.get("Type") == "volume":
                        src = vol_real.get(m.get("Name")) or m.get("Source")
                    else:
                        src = m.get("Source")
                    if src:
                        sources.append(src)
                containers.append({"name": container.name, "status": container.status, "sources": sources})

            for name in volume_names:
                cands = [f"{b}/{name}" for b in bases]
                for c in containers:
                    if any(s == cand or s.startswith(cand + "/") for s in c["sources"] for cand in cands):
                        result[name].append({"name": c["name"], "status": c["status"]})
        except Exception as e:
            print(f"[ERROR] Failed to map containers to volumes: {e}", flush=True)

        return result

    def stop_container(self, name: str, timeout: int = 120) -> None:
        """Stop a container by name, waiting up to *timeout* seconds for a clean exit."""
        if not self.client:
            raise RuntimeError("Docker daemon unavailable")
        self.client.containers.get(name).stop(timeout=timeout)

    def start_container(self, name: str) -> None:
        """Start a container by name."""
        if not self.client:
            raise RuntimeError("Docker daemon unavailable")
        self.client.containers.get(name).start()

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
