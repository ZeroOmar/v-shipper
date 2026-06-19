"""HTTP client for the v-helper control API.

Used when a remote pool has ``api_host`` and ``api_key`` configured.
All methods raise ``RemoteApiError`` on non-2xx responses or connectivity
failures — callers can catch it and fall back to rsync-based behaviour.
"""

import json
import urllib.request
import urllib.error
from typing import Dict, List, Any, Optional


class RemoteApiError(Exception):
    """Raised when the v-helper API returns an error or is unreachable."""


class RemoteApiClient:
    """Thin client for the v-helper HTTP control API."""

    def __init__(self, api_host: str, api_key: str, timeout: int = 10):
        self._base = f"http://{api_host}"
        self._key = api_key
        self._timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict] = None) -> Any:
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
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
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

    def disk(self) -> Dict[str, int]:
        """Return disk usage: {total_bytes, used_bytes, free_bytes}."""
        return self._request("GET", "/fs/disk")

    def ls(self) -> List[Dict[str, Any]]:
        """List VOLUME entries: [{name, size_bytes, mtime_epoch, is_dir}]."""
        return self._request("GET", "/fs/ls")

    def mkdir(self, name: str) -> None:
        """Create a directory named *name* inside VOLUME."""
        self._request("POST", "/fs/mkdir", {"name": name})

    def rename(self, src: str, dst: str) -> None:
        """Rename *src* to *dst* inside VOLUME."""
        self._request("POST", "/fs/rename", {"src": src, "dst": dst})


def client_for_pool(pool: Dict[str, Any]) -> Optional[RemoteApiClient]:
    """Return a RemoteApiClient if the pool has api_host/api_key, else None."""
    api_host = pool.get("api_host")
    api_key = pool.get("api_key")
    if api_host and api_key:
        return RemoteApiClient(api_host, api_key)
    return None
