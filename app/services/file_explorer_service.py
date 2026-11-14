"""Volume file explorer — browse and manipulate the files *inside* a local volume.

Security model
--------------
v-shipper runs as root and the volumes it manages are written by other
containers, so a volume's contents are treated as hostile: they may contain
symlinks pointing at ``/etc``, and a container may swap a directory for a
symlink *while* an operation runs (TOCTOU). Validating a path string and then
handing it to a path-based syscall or a ``cp``/``rm`` subprocess is not enough —
the kernel would re-resolve the path and follow whatever is there by then.

So this module never passes a multi-component path to the kernel:

* Every request path is first validated by ``validate_rel_path`` (no ``..``,
  no absolute paths, no control chars) at the API boundary.
* The volume directory is opened relative to the pool dir with
  ``O_DIRECTORY|O_NOFOLLOW``; each further component is opened relative to the
  previous directory fd, again with ``O_DIRECTORY|O_NOFOLLOW``. A symlink (or
  non-directory) anywhere in the path fails with ELOOP/ENOTDIR → HTTP 400.
* Operations then act on ``(parent_fd, name)`` via ``dir_fd=`` syscalls, and
  recursive walks open every subdirectory the same way and verify it with
  ``samestat`` against the ``lstat`` taken just before. Once a directory fd is
  open, renaming/swapping path components cannot redirect it.
* No subprocesses at all — copy, delete and recursive chmod/chown are
  implemented here on top of those fds.
* Symlinks are listed (with their target text) and can be renamed / moved /
  deleted / copied *as links*, but are never traversed and never chmod'ed.
* chmod/chown skip regular files with more than one hard link (a hard link can
  alias a file outside the volume on the same filesystem) and skip device /
  FIFO / socket nodes (opening those can have side effects).
* Error messages only ever contain volume-relative paths.
"""

import errno
import grp
import os
import pwd
import stat
import time
from typing import Any, Dict, List, Optional, Tuple

from app.services.task_queue import get_task_queue
from app.services.volume_service import VolumeService, get_volume_service, task_log

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# Directory open: never follows a final-component symlink, fails on non-dirs
# (O_DIRECTORY is checked before any device driver open() would run).
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
# Regular-file open for reading / fchmod: no symlink follow, and O_NONBLOCK so a
# FIFO swapped in after the lstat can't hang the worker.
_O_FILE_RD = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
_O_FILE_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC

MAX_LIST_ENTRIES = 5000
MAX_ITEMS = 500
MAX_DEPTH = 256
MAX_REPORTED_ERRORS = 20
_COPY_CHUNK = 1024 * 1024
_PROGRESS_INTERVAL = 1.0

# chmod with AT_SYMLINK_NOFOLLOW is only a fallback (macOS/BSD dev hosts when
# the file itself is unreadable); Linux uses open(O_NOFOLLOW)+fchmod.
_CHMOD_NOFOLLOW = os.chmod in os.supports_follow_symlinks


class ExplorerError(Exception):
    """A user-facing failure with the HTTP status the API should return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class VolumeLockedError(ExplorerError):
    def __init__(self):
        super().__init__(409, "Volume is busy (a task is running on it) — try again when it finishes")


class _Cancelled(Exception):
    pass


def _split(rel: str) -> List[str]:
    return [p for p in rel.split("/") if p] if rel else []


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def _display_name(name: str) -> Tuple[str, bool]:
    """Return (printable name, valid). Names that aren't valid UTF-8 arrive as
    surrogate-escaped strings; they can be shown but not operated on (the
    request validators reject them), and must not reach the JSON encoder raw."""
    try:
        name.encode("utf-8")
        return name, True
    except UnicodeEncodeError:
        return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace"), False


class _Fds:
    """Close every registered fd on exit, so no path through an operation leaks one."""

    def __init__(self):
        self._fds: List[int] = []

    def add(self, fd: int) -> int:
        self._fds.append(fd)
        return fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        while self._fds:
            try:
                os.close(self._fds.pop())
            except OSError:
                pass
        return False


class _CopyProgress:
    def __init__(self, task_queue, task_id: str, total: int):
        self.tq = task_queue
        self.task_id = task_id
        self.total = total
        self.done = 0
        self.skipped_special = 0
        self._last = 0.0

    def check_cancel(self):
        if self.tq.is_cancelled(self.task_id):
            raise _Cancelled()

    def add(self, n: int, label: str):
        self.done += n
        now = time.monotonic()
        if now - self._last >= _PROGRESS_INTERVAL:
            self._last = now
            pct = int(self.done * 100 / self.total) if self.total else 0
            self.tq.update_progress(self.task_id, {
                "progress_percent": min(pct, 99),
                "current_operation": f"Copying {label}",
            })


class FileExplorerService:
    def __init__(self, config):
        self.config = config
        self.task_queue = get_task_queue()
        self.volume_service: VolumeService = get_volume_service(config)

    # ── Error mapping ──────────────────────────────────────────────────────────

    @staticmethod
    def _os_error(exc: OSError, volume: str, rel: str) -> ExplorerError:
        where = f"{volume}/{rel}" if rel else f"{volume}/"
        code = exc.errno
        if code == errno.ENOENT:
            return ExplorerError(404, f"Not found: {where}")
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            return ExplorerError(409, f"Already exists: {where}")
        if code in (errno.ELOOP, errno.ENOTDIR):
            return ExplorerError(400, f"Not a folder inside the volume (symlinks are not followed): {where}")
        if code in (errno.EACCES, errno.EPERM):
            return ExplorerError(403, f"Permission denied: {where}")
        if code == errno.EISDIR:
            return ExplorerError(400, f"Is a folder: {where}")
        if code == errno.ENAMETOOLONG:
            return ExplorerError(400, f"Name too long: {where}")
        if code == errno.ENOSPC:
            return ExplorerError(507, "No space left on device")
        if code in (errno.EINVAL, errno.EXDEV, errno.EBUSY):
            return ExplorerError(400, f"Operation not allowed on {where}")
        name = errno.errorcode.get(code, "unknown")
        return ExplorerError(500, f"Filesystem error ({name}) on {where}")

    # ── fd-pinned traversal ────────────────────────────────────────────────────

    def _open_volume(self, pool_name: str, volume: str) -> int:
        pool = self.volume_service.get_pool_by_name(pool_name)
        if not pool:
            raise ExplorerError(404, "Pool not found")
        if pool.get("pool_type") == "remote" or pool.get("role") != "docker":
            raise ExplorerError(400, "The file explorer is only available for local Docker volume pools")
        # The pool path comes from trusted config; only the volume (and below)
        # is attacker-influenced, so that is where O_NOFOLLOW starts.
        try:
            pool_fd = os.open(pool["path"], os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
        except OSError:
            raise ExplorerError(500, "Pool directory is not accessible")
        try:
            return os.open(volume, _O_DIR, dir_fd=pool_fd)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ExplorerError(404, "Volume not found")
            raise self._os_error(e, volume, "")
        finally:
            os.close(pool_fd)

    def _open_dir(self, root_fd: int, parts: List[str], volume: str,
                  chain: Optional[List[os.stat_result]] = None) -> int:
        """Open ``parts`` below ``root_fd`` one component at a time, never
        following symlinks. Optionally records the fstat of every directory on
        the way (root included) into ``chain``."""
        fd = os.dup(root_fd)
        try:
            if chain is not None:
                chain.append(os.fstat(fd))
            for i, part in enumerate(parts):
                # Defence in depth — validate_rel_path already guarantees this.
                if part in ("", ".", "..") or "/" in part or "\0" in part:
                    raise ExplorerError(400, "Invalid path")
                try:
                    nfd = os.open(part, _O_DIR, dir_fd=fd)
                except OSError as e:
                    raise self._os_error(e, volume, _join(*parts[:i + 1]))
                os.close(fd)
                fd = nfd
                if chain is not None:
                    chain.append(os.fstat(fd))
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_subdir(self, dir_fd: int, name: str, expected: os.stat_result) -> int:
        fd = os.open(name, _O_DIR, dir_fd=dir_fd)
        if not os.path.samestat(expected, os.fstat(fd)):
            os.close(fd)
            raise OSError(errno.EBUSY, "changed during operation")
        return fd

    @staticmethod
    def _lstat(dir_fd: int, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)

    @staticmethod
    def _scan(dir_fd: int) -> List[Tuple[str, os.stat_result]]:
        out = []
        with os.scandir(dir_fd) as it:
            for entry in it:
                try:
                    out.append((entry.name, os.stat(entry.name, dir_fd=dir_fd, follow_symlinks=False)))
                except OSError:
                    pass  # vanished between readdir and lstat
        return out

    def _ensure_unlocked(self, pool: str, volume: str):
        if self.task_queue.is_volume_locked(pool, volume):
            raise VolumeLockedError()

    # ── Listing ────────────────────────────────────────────────────────────────

    def list_dir(self, pool: str, volume: str, path: str) -> Dict[str, Any]:
        users: Dict[int, str] = {}
        groups: Dict[int, str] = {}
        with _Fds() as fds:
            root = fds.add(self._open_volume(pool, volume))
            dfd = fds.add(self._open_dir(root, _split(path), volume))
            entries = []
            truncated = False
            try:
                scanned = self._scan(dfd)
            except OSError as e:
                raise self._os_error(e, volume, path)
            for name, st in scanned:
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                mode = st.st_mode
                if stat.S_ISLNK(mode):
                    kind = "symlink"
                elif stat.S_ISDIR(mode):
                    kind = "dir"
                elif stat.S_ISREG(mode):
                    kind = "file"
                else:
                    kind = "other"
                link_target = None
                if kind == "symlink":
                    try:
                        link_target = _display_name(os.readlink(name, dir_fd=dfd))[0]
                    except OSError:
                        link_target = "?"
                if st.st_uid not in users:
                    users[st.st_uid] = VolumeService._name_for_uid(st.st_uid)
                if st.st_gid not in groups:
                    groups[st.st_gid] = VolumeService._name_for_gid(st.st_gid)
                shown, valid = _display_name(name)
                entries.append({
                    "name": shown,
                    "valid_name": valid,
                    "type": kind,
                    "size": st.st_size if kind == "file" else None,
                    "mtime": int(st.st_mtime),
                    "mode": format(stat.S_IMODE(mode), "03o"),
                    "uid": st.st_uid,
                    "gid": st.st_gid,
                    "user": users[st.st_uid],
                    "group": groups[st.st_gid],
                    "nlink": st.st_nlink,
                    "link_target": link_target,
                })
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].casefold()))
        return {
            "pool": pool,
            "volume": volume,
            "path": path,
            "entries": entries,
            "truncated": truncated,
            "locked": self.task_queue.is_volume_locked(pool, volume),
        }

    # ── Immediate ops ──────────────────────────────────────────────────────────

    def mkdir(self, pool: str, volume: str, parent: str, name: str) -> None:
        self._ensure_unlocked(pool, volume)
        rel = _join(parent, name)
        with _Fds() as fds:
            root = fds.add(self._open_volume(pool, volume))
            pfd = fds.add(self._open_dir(root, _split(parent), volume))
            try:
                os.mkdir(name, 0o777, dir_fd=pfd)
            except OSError as e:
                raise self._os_error(e, volume, rel)
        print(f"[FILES] mkdir {pool}/{volume}:{rel}", flush=True)

    def rename(self, pool: str, volume: str, path: str, new_name: str) -> None:
        self._ensure_unlocked(pool, volume)
        parts = _split(path)
        if not parts:
            raise ExplorerError(400, "The volume root cannot be renamed here — use the volume Rename action")
        parent, name = parts[:-1], parts[-1]
        if name == new_name:
            return
        new_rel = _join(*parent, new_name)
        with _Fds() as fds:
            root = fds.add(self._open_volume(pool, volume))
            pfd = fds.add(self._open_dir(root, parent, volume))
            try:
                self._lstat(pfd, name)
            except OSError as e:
                raise self._os_error(e, volume, path)
            if self._exists(pfd, new_name):
                raise ExplorerError(409, f"Already exists: {volume}/{new_rel}")
            try:
                os.rename(name, new_name, src_dir_fd=pfd, dst_dir_fd=pfd)
            except OSError as e:
                raise self._os_error(e, volume, path)
        print(f"[FILES] rename {pool}/{volume}:{path} -> {new_rel}", flush=True)

    @staticmethod
    def _exists(dir_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _resolve_owner(owner_spec: Optional[str]) -> Optional[Tuple[int, int]]:
        if not owner_spec:
            return None
        user, group = owner_spec.split(":", 1)
        try:
            uid = int(user) if user.isdigit() else pwd.getpwnam(user).pw_uid
        except KeyError:
            raise ExplorerError(400, f"Unknown user '{user}'")
        try:
            gid = int(group) if group.isdigit() else grp.getgrnam(group).gr_gid
        except KeyError:
            raise ExplorerError(400, f"Unknown group '{group}'")
        return uid, gid

    def set_permissions(self, pool: str, volume: str, path: str, mode: Optional[str],
                        owner_spec: Optional[str], recursive: bool) -> Dict[str, Any]:
        """chmod and/or chown one entry, optionally recursively — fd-based, see module doc."""
        self._ensure_unlocked(pool, volume)
        if not mode and not owner_spec:
            raise ExplorerError(400, "No permission changes requested")
        mode_bits = int(mode, 8) if mode else None
        ids = self._resolve_owner(owner_spec)
        result = {"changed": 0, "skipped_hardlinks": 0, "skipped_special": 0,
                  "skipped_symlinks": 0, "errors": []}

        with _Fds() as fds:
            root = fds.add(self._open_volume(pool, volume))
            parts = _split(path)
            if not parts:
                target = fds.add(os.dup(root))
                is_dir = True
            else:
                pfd = fds.add(self._open_dir(root, parts[:-1], volume))
                name = parts[-1]
                try:
                    st = self._lstat(pfd, name)
                except OSError as e:
                    raise self._os_error(e, volume, path)
                if stat.S_ISLNK(st.st_mode):
                    raise ExplorerError(400, "Permissions of a symlink cannot be changed (it is never followed)")
                if stat.S_ISDIR(st.st_mode):
                    is_dir = True
                    try:
                        target = fds.add(self._open_subdir(pfd, name, st))
                    except OSError as e:
                        raise self._os_error(e, volume, path)
                elif stat.S_ISREG(st.st_mode):
                    is_dir = False
                    if st.st_nlink > 1:
                        raise ExplorerError(400, "This file has multiple hard links (it may alias a file "
                                                 "outside the volume), so its permissions are not changed")
                    self._apply_file(pfd, name, st, mode_bits, ids, volume, path, result)
                    if result["errors"]:
                        raise ExplorerError(403, result["errors"][0])
                else:
                    raise ExplorerError(400, "Device, FIFO and socket entries are not changed")

            if is_dir:
                if recursive:
                    self._apply_tree(target, path, mode_bits, ids, volume, result, 0)
                else:
                    self._apply_fd(target, mode_bits, ids, volume, path, result)

        print(f"[FILES] permissions {pool}/{volume}:{path or '/'} mode={mode or '-'} "
              f"owner={owner_spec or '-'} recursive={recursive} changed={result['changed']} "
              f"skipped_hardlinks={result['skipped_hardlinks']} errors={len(result['errors'])}", flush=True)
        return result

    @staticmethod
    def _record_error(result: Dict, message: str):
        if len(result["errors"]) < MAX_REPORTED_ERRORS:
            result["errors"].append(message)

    def _apply_fd(self, fd: int, mode_bits, ids, volume: str, rel: str, result: Dict):
        try:
            if ids:  # chown first: it clears set-id bits, chmod then sets the final mode
                os.fchown(fd, ids[0], ids[1])
            if mode_bits is not None:
                os.fchmod(fd, mode_bits)
            result["changed"] += 1
        except OSError as e:
            self._record_error(result, self._os_error(e, volume, rel).message)

    def _apply_file(self, dir_fd: int, name: str, st: os.stat_result, mode_bits, ids,
                    volume: str, rel: str, result: Dict):
        try:
            fd = os.open(name, _O_FILE_RD, dir_fd=dir_fd)
        except PermissionError:
            # Non-root dev host and an unreadable file: fall back to the
            # no-follow *at() calls where the platform supports them.
            if not _CHMOD_NOFOLLOW:
                self._record_error(result, f"Permission denied: {volume}/{rel}")
                return
            try:
                if ids:
                    os.chown(name, ids[0], ids[1], dir_fd=dir_fd, follow_symlinks=False)
                if mode_bits is not None:
                    os.chmod(name, mode_bits, dir_fd=dir_fd, follow_symlinks=False)
                result["changed"] += 1
            except OSError as e:
                self._record_error(result, self._os_error(e, volume, rel).message)
            return
        except OSError as e:
            self._record_error(result, self._os_error(e, volume, rel).message)
            return
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or not os.path.samestat(st, fst):
                self._record_error(result, f"Changed during operation, skipped: {volume}/{rel}")
                return
            self._apply_fd(fd, mode_bits, ids, volume, rel, result)
        finally:
            os.close(fd)

    def _apply_tree(self, dir_fd: int, rel: str, mode_bits, ids, volume: str,
                    result: Dict, depth: int):
        if depth > MAX_DEPTH:
            self._record_error(result, f"Too deeply nested, skipped: {volume}/{rel}")
            return
        self._apply_fd(dir_fd, mode_bits, ids, volume, rel, result)
        try:
            children = self._scan(dir_fd)
        except OSError as e:
            self._record_error(result, self._os_error(e, volume, rel).message)
            return
        for name, st in children:
            child_rel = _join(rel, name)
            m = st.st_mode
            if stat.S_ISDIR(m):
                try:
                    cfd = self._open_subdir(dir_fd, name, st)
                except OSError as e:
                    self._record_error(result, self._os_error(e, volume, child_rel).message)
                    continue
                try:
                    self._apply_tree(cfd, child_rel, mode_bits, ids, volume, result, depth + 1)
                finally:
                    os.close(cfd)
            elif stat.S_ISLNK(m):
                result["skipped_symlinks"] += 1
                if ids:  # lchown — changes the link itself, never its target
                    try:
                        os.chown(name, ids[0], ids[1], dir_fd=dir_fd, follow_symlinks=False)
                    except (OSError, NotImplementedError):
                        pass
            elif stat.S_ISREG(m):
                if st.st_nlink > 1:
                    result["skipped_hardlinks"] += 1
                    continue
                self._apply_file(dir_fd, name, st, mode_bits, ids, volume, child_rel, result)
            else:
                result["skipped_special"] += 1

    # ── Queued-op pre-checks (shared by the route and the worker) ──────────────

    @staticmethod
    def _validate_items(names: List[str]):
        if not names:
            raise ExplorerError(400, "No items selected")
        if len(names) > MAX_ITEMS:
            raise ExplorerError(400, f"At most {MAX_ITEMS} items per operation")
        if len(set(names)) != len(names):
            raise ExplorerError(400, "Duplicate items selected")

    def _check_transfer_item(self, op: str, pfd: int, name: str, dfd: int,
                             dest_chain: List[os.stat_result], volume: str,
                             parent: str, dest_dir: str) -> os.stat_result:
        rel = _join(parent, name)
        try:
            st = self._lstat(pfd, name)
        except OSError as e:
            raise self._os_error(e, volume, rel)
        if stat.S_ISDIR(st.st_mode) and any(os.path.samestat(st, c) for c in dest_chain):
            raise ExplorerError(400, f"Cannot {op} a folder into itself: {volume}/{rel}")
        if op == "move" and os.path.samestat(os.fstat(pfd), dest_chain[-1]):
            raise ExplorerError(400, f"Already in that folder: {volume}/{rel}")
        if self._exists(dfd, name):
            raise ExplorerError(409, f"Already exists: {volume}/{_join(dest_dir, name)}")
        return st

    def precheck_transfer(self, op: str, pool: str, volume: str, parent: str,
                          names: List[str], dest_dir: str) -> None:
        self._validate_items(names)
        with _Fds() as fds:
            root = fds.add(self._open_volume(pool, volume))
            pfd = fds.add(self._open_dir(root, _split(parent), volume))
            chain: List[os.stat_result] = []
            dfd = fds.add(self._open_dir(root, _split(dest_dir), volume, chain))
            for name in names:
                self._check_transfer_item(op, pfd, name, dfd, chain, volume, parent, dest_dir)

    def precheck_delete(self, pool: str, volume: str, parent: str, names: List[str]) -> None:
        self._validate_items(names)
        with _Fds() as fds:
            root = fds.add(self._open_volume(pool, volume))
            pfd = fds.add(self._open_dir(root, _split(parent), volume))
            for name in names:
                try:
                    self._lstat(pfd, name)
                except OSError as e:
                    raise self._os_error(e, volume, _join(parent, name))

    # ── Queued ops (run inside a task; finalize the task themselves) ───────────

    def _finish(self, task_id: str, verb: str, total: int, failures: List[str]):
        if failures:
            self.task_queue.complete_task(
                task_id, success=False,
                error=f"{len(failures)} of {total} item(s) failed to {verb}: {failures[0]}")
        else:
            self.task_queue.complete_task(task_id, success=True)

    def run_transfer(self, op: str, pool: str, volume: str, parent: str, names: List[str],
                     dest_dir: str, task_id: str) -> None:
        """Copy or move ``names`` (all in ``parent``) into ``dest_dir`` of the same volume."""
        tq = self.task_queue
        lock = tq.create_lockfile(pool, volume)
        failures: List[str] = []
        dest_label = f"{volume}/{dest_dir}" if dest_dir else f"{volume}/"
        try:
            self._validate_items(names)
            with _Fds() as fds:
                root = fds.add(self._open_volume(pool, volume))
                pfd = fds.add(self._open_dir(root, _split(parent), volume))
                chain: List[os.stat_result] = []
                dfd = fds.add(self._open_dir(root, _split(dest_dir), volume, chain))

                progress = None
                if op == "copy":
                    total = 0
                    for name in names:
                        total += self._tree_size(pfd, name)
                    progress = _CopyProgress(tq, task_id, total)
                    task_log(task_id, "INFO", f"Copying {len(names)} item(s) ({total} bytes) to {dest_label}")
                else:
                    task_log(task_id, "INFO", f"Moving {len(names)} item(s) to {dest_label}")

                for i, name in enumerate(names):
                    if tq.is_cancelled(task_id):
                        raise _Cancelled()
                    rel = _join(parent, name)
                    tq.update_progress(task_id, {"current_operation": f"{op.capitalize()} {rel}"})
                    try:
                        self._check_transfer_item(op, pfd, name, dfd, chain, volume, parent, dest_dir)
                        if op == "move":
                            try:
                                os.rename(name, name, src_dir_fd=pfd, dst_dir_fd=dfd)
                            except OSError as e:
                                raise self._os_error(e, volume, rel)
                            tq.update_progress(task_id, {"progress_percent": int((i + 1) * 100 / len(names))})
                        else:
                            self._copy_top(pfd, name, dfd, rel, volume, progress, task_id)
                        task_log(task_id, "INFO", f"{'Moved' if op == 'move' else 'Copied'} {volume}/{rel} → {dest_label}")
                    except ExplorerError as e:
                        failures.append(e.message)
                        task_log(task_id, "ERROR", e.message)
                if progress and progress.skipped_special:
                    task_log(task_id, "WARNING", f"Skipped {progress.skipped_special} device/FIFO/socket entr(y/ies)")
            self._finish(task_id, op, len(names), failures)
        except _Cancelled:
            task_log(task_id, "WARNING", "Cancelled — stopping before the next item")
            tq.finalize_cancelled(task_id)
        except ExplorerError as e:
            task_log(task_id, "ERROR", e.message)
            tq.complete_task(task_id, success=False, error=e.message)
        finally:
            tq.remove_lockfile(lock)

    def run_delete(self, pool: str, volume: str, parent: str, names: List[str], task_id: str) -> None:
        tq = self.task_queue
        lock = tq.create_lockfile(pool, volume)
        failures: List[str] = []
        try:
            self._validate_items(names)
            task_log(task_id, "INFO", f"Deleting {len(names)} item(s) from {volume}/{parent}")
            with _Fds() as fds:
                root = fds.add(self._open_volume(pool, volume))
                pfd = fds.add(self._open_dir(root, _split(parent), volume))

                def check():
                    if tq.is_cancelled(task_id):
                        raise _Cancelled()

                for i, name in enumerate(names):
                    check()
                    rel = _join(parent, name)
                    tq.update_progress(task_id, {"current_operation": f"Deleting {rel}",
                                                 "progress_percent": int(i * 100 / len(names))})
                    try:
                        self._rm_tree(pfd, name, volume, rel, check, 0)
                        task_log(task_id, "INFO", f"Deleted {volume}/{rel}")
                    except ExplorerError as e:
                        failures.append(e.message)
                        task_log(task_id, "ERROR", e.message)
            self._finish(task_id, "delete", len(names), failures)
        except _Cancelled:
            task_log(task_id, "WARNING", "Cancelled — some items may already be deleted")
            tq.finalize_cancelled(task_id)
        except ExplorerError as e:
            task_log(task_id, "ERROR", e.message)
            tq.complete_task(task_id, success=False, error=e.message)
        finally:
            tq.remove_lockfile(lock)

    # ── Tree primitives ────────────────────────────────────────────────────────

    def _rm_tree(self, parent_fd: int, name: str, volume: str, rel: str, check, depth: int):
        """Delete ``name`` under ``parent_fd``. Directories are walked through
        O_NOFOLLOW fds; symlinks are unlinked, never followed. ``check`` raises
        _Cancelled to stop between entries."""
        try:
            st = self._lstat(parent_fd, name)
        except OSError as e:
            raise self._os_error(e, volume, rel)
        if not stat.S_ISDIR(st.st_mode):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as e:
                raise self._os_error(e, volume, rel)
            return
        if depth > MAX_DEPTH:
            raise ExplorerError(400, f"Too deeply nested: {volume}/{rel}")
        try:
            fd = self._open_subdir(parent_fd, name, st)
        except OSError as e:
            raise self._os_error(e, volume, rel)
        try:
            try:
                children = self._scan(fd)
            except OSError as e:
                raise self._os_error(e, volume, rel)
            for child, _ in children:
                if check:
                    check()
                self._rm_tree(fd, child, volume, _join(rel, child), check, depth + 1)
        finally:
            os.close(fd)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as e:
            raise self._os_error(e, volume, rel)

    def _tree_size(self, parent_fd: int, name: str, depth: int = 0) -> int:
        """Sum regular-file bytes under ``name`` (for copy progress). Best-effort."""
        try:
            st = self._lstat(parent_fd, name)
        except OSError:
            return 0
        if stat.S_ISREG(st.st_mode):
            return st.st_size
        if not stat.S_ISDIR(st.st_mode) or depth > MAX_DEPTH:
            return 0
        try:
            fd = self._open_subdir(parent_fd, name, st)
        except OSError:
            return 0
        try:
            return sum(self._tree_size(fd, child, depth + 1) for child, _ in self._scan(fd))
        except OSError:
            return 0
        finally:
            os.close(fd)

    def _copy_top(self, src_pfd: int, name: str, dst_pfd: int, rel: str, volume: str,
                  progress: _CopyProgress, task_id: str):
        """Copy one selected item; on failure or cancel remove the partial copy."""
        try:
            self._copy_entry(src_pfd, name, dst_pfd, rel, volume, progress, 0)
        except BaseException as exc:
            if self._exists(dst_pfd, name):
                try:
                    self._rm_tree(dst_pfd, name, volume, name, None, 0)
                    task_log(task_id, "INFO", f"Removed partial copy of {volume}/{rel}")
                except ExplorerError as e:
                    task_log(task_id, "WARNING", f"Could not remove partial copy: {e.message}")
            if isinstance(exc, OSError):
                raise self._os_error(exc, volume, rel)
            raise

    def _copy_entry(self, src_pfd: int, name: str, dst_pfd: int, rel: str, volume: str,
                    progress: _CopyProgress, depth: int):
        progress.check_cancel()
        try:
            st = self._lstat(src_pfd, name)
        except OSError as e:
            raise self._os_error(e, volume, rel)
        m = st.st_mode
        if stat.S_ISLNK(m):
            os.symlink(os.readlink(name, dir_fd=src_pfd), name, dir_fd=dst_pfd)
            try:
                os.chown(name, st.st_uid, st.st_gid, dir_fd=dst_pfd, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
        elif stat.S_ISREG(m):
            self._copy_file(src_pfd, name, dst_pfd, st, rel, volume, progress)
        elif stat.S_ISDIR(m):
            if depth > MAX_DEPTH:
                raise ExplorerError(400, f"Too deeply nested: {volume}/{rel}")
            sfd = self._open_subdir(src_pfd, name, st)
            try:
                os.mkdir(name, 0o700, dir_fd=dst_pfd)
                dfd = os.open(name, _O_DIR, dir_fd=dst_pfd)
                try:
                    for child, _ in self._scan(sfd):
                        self._copy_entry(sfd, child, dfd, _join(rel, child), volume, progress, depth + 1)
                    self._copy_meta(dfd, st)
                finally:
                    os.close(dfd)
            finally:
                os.close(sfd)
        else:
            progress.skipped_special += 1

    def _copy_file(self, src_pfd: int, name: str, dst_pfd: int, st: os.stat_result,
                   rel: str, volume: str, progress: _CopyProgress):
        sfd = os.open(name, _O_FILE_RD, dir_fd=src_pfd)
        try:
            fst = os.fstat(sfd)
            if not stat.S_ISREG(fst.st_mode) or not os.path.samestat(st, fst):
                raise ExplorerError(409, f"Changed during copy: {volume}/{rel}")
            dfd = os.open(name, _O_FILE_NEW, 0o600, dir_fd=dst_pfd)
            try:
                while True:
                    chunk = os.read(sfd, _COPY_CHUNK)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(dfd, view)
                        view = view[written:]
                    progress.add(len(chunk), rel)
                    progress.check_cancel()
                self._copy_meta(dfd, fst)
            finally:
                os.close(dfd)
        finally:
            os.close(sfd)

    @staticmethod
    def _copy_meta(fd: int, st: os.stat_result):
        """Preserve owner, mode and timestamps like ``cp -a`` (no xattrs/ACLs)."""
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
        except PermissionError:
            pass  # non-root (dev host): keep the copier's ownership
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
        try:
            os.utime(fd, ns=(st.st_atime_ns, st.st_mtime_ns))
        except (OSError, NotImplementedError):
            pass


_file_explorer_service: Optional[FileExplorerService] = None


def get_file_explorer_service(config=None) -> FileExplorerService:
    global _file_explorer_service
    if _file_explorer_service is None and config:
        _file_explorer_service = FileExplorerService(config)
    return _file_explorer_service
