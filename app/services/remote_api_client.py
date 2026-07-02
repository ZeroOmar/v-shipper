"""HTTP client for the v-helper control API.

Used when a remote pool has ``api_host`` and ``api_key`` configured.
All methods raise ``RemoteApiError`` on non-2xx responses or connectivity
failures — callers can catch it and fall back to rsync-based behaviour.
"""

import json
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, List, Any, Optional


class RemoteApiError(Exception):
    """Raised when the v-helper API returns an error or is unreachable."""


class RemoteApiClient:
    """Thin client for the v-helper HTTP control API."""

    def __init__(self, api_host: str, api_key: str, timeout: int = 10):
        self._base = f"http://{api_host}"
        self._key = api_key
        self._timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict] = None,
                 timeout: Optional[int] = None) -> Any:
        url = self._base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "X-API-Key": self._key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("detail", str(exc))
            except Exception:
                detail = str(exc)
            raise RemoteApiError(f"v-helper API {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RemoteApiError(f"v-helper API unreachable: {exc}") from exc

    def health(self) -> bool:
        """Return True if the API is reachable and authenticated."""
        try:
            self._request("GET", "/health")
            return True
        except RemoteApiError:
            return False

    def version(self) -> Optional[str]:
        """Return the v-helper version string.

        Returns None if the endpoint is missing (a v-helper that predates the
        ``/version`` endpoint, i.e. an old one) or otherwise unavailable.
        """
        try:
            return self._request("GET", "/version").get("version")
        except RemoteApiError:
            return None

    def disk(self) -> Dict[str, int]:
        """Return disk usage: {total_bytes, used_bytes, free_bytes}."""
        return self._request("GET", "/fs/disk")

    def ls(self) -> List[Dict[str, Any]]:
        """List VOLUME entries: [{name, size_bytes, mtime_epoch, is_dir}]."""
        return self._request("GET", "/fs/ls")

    def size(self, name: str) -> int:
        """Return total bytes of regular files under *name* (recursive).

        Uses v-helper's real filesystem access with the same semantics as
        v-shipper's local ``_get_dir_size`` (symlinks excluded), so size
        comparisons across local and remote pools are like for like.
        """
        path = "/fs/size?name=" + urllib.parse.quote(name, safe="")
        return int(self._request("GET", path)["size_bytes"])

    def mkdir(self, name: str) -> None:
        """Create a directory named *name* inside VOLUME."""
        self._request("POST", "/fs/mkdir", {"name": name})

    def rename(self, src: str, dst: str) -> None:
        """Rename *src* to *dst* inside VOLUME."""
        self._request("POST", "/fs/rename", {"src": src, "dst": dst})

    def rm(self, name: str) -> None:
        """Delete the directory or file named *name* inside VOLUME."""
        self._request("POST", "/fs/rm", {"name": name})

    def stat(self, name: str) -> Dict[str, Any]:
        """Return current ownership/mode: {mode, uid, gid, user, group}."""
        path = "/fs/stat?name=" + urllib.parse.quote(name, safe="")
        return self._request("GET", path)

    def chmod(self, name: str, mode: str) -> Dict[str, Any]:
        """Run ``chmod -R <mode>`` on *name* inside VOLUME. Returns {ok, command, output}."""
        return self._request("POST", "/fs/chmod", {"name": name, "mode": mode})

    def chown(self, name: str, owner: str) -> Dict[str, Any]:
        """Run ``chown -R <owner>`` (a ``user:group`` spec) on *name*. Returns {ok, command, output}."""
        return self._request("POST", "/fs/chown", {"name": name, "owner": owner})

    def docker_users(self) -> Dict[str, Any]:
        """Map each remote volume to containers using it: {volume: [{name, status}]}.

        Returns {} if v-helper has no Docker access or predates this endpoint.
        The swallowed error is logged so a silently-empty container view is
        diagnosable (unreachable v-helper, bad api_key, missing endpoint).
        """
        try:
            return self._request("GET", "/docker/users")
        except RemoteApiError as e:
            print(f"[DOCKER] {self._base}/docker/users unavailable — returning no containers: {e}", flush=True)
            return {}

    def stop_container(self, name: str, timeout: int = 120) -> None:
        """Stop a container by name, applying *timeout* as the docker stop grace
        period on the remote (v-helper) so it matches local behaviour. The HTTP
        wait outlives that grace (timeout + headroom) so a slow-but-successful
        stop isn't read as a failure."""
        self._request("POST", "/docker/container/stop",
                      {"name": name, "timeout": timeout}, timeout=timeout + 30)

    def start_container(self, name: str) -> None:
        """Start a container by name."""
        self._request("POST", "/docker/container/start", {"name": name})

    def rsync_pull(self, source_host: str, source_module: str, source_volume: str,
                   dest: str, delete: bool = False,
                   bwlimit: Optional[int] = None) -> str:
        """Start a background rsync pull on this (destination) v-helper.

        The remote v-helper acts as an rsync client and pulls
        ``rsync://{source_host}/{source_module}/{source_volume}/`` into its own
        local ``dest`` volume. Used for remote→remote migrations, which native
        rsync cannot do daemon-to-daemon. Returns the remote job id to poll.

        Raises RemoteApiError (with "404" in the message) against a v-helper too
        old to expose this endpoint — callers fall back to plain rsync.
        """
        body = {
            "source_host": source_host,
            "source_module": source_module,
            "source_volume": source_volume,
            "dest": dest,
            "delete": delete,
        }
        if bwlimit is not None:
            body["bwlimit"] = bwlimit
        return self._request("POST", "/rsync/pull", body)["job_id"]

    def rsync_job_log(self, job_id: str, offset: int = 0) -> Dict[str, Any]:
        """Poll a pull job: {state, percent, returncode, error, lines, next_offset}.

        ``lines`` are the log lines from *offset* onward; ``next_offset`` is the
        offset to pass on the next poll to get only new lines.
        """
        return self._request("GET", f"/rsync/job/{job_id}/log?offset={int(offset)}")


def client_for_pool(pool: Dict[str, Any]) -> Optional[RemoteApiClient]:
    """Return a RemoteApiClient if the pool has api_host/api_key, else None."""
    api_host = pool.get("api_host")
    api_key = pool.get("api_key")
    if api_host and api_key:
        return RemoteApiClient(api_host, api_key)
    return None
