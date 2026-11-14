"""Security tests for the volume file explorer.

The explorer runs as root against volumes written by other containers, so these
tests pin down the guarantee that it can never read, change or reveal anything
outside the volume being explored: traversal strings, symlinks (intermediate
and final), hard links, set-id modes, self-nesting and error-message leakage.
"""

import os
import stat
from types import SimpleNamespace

import pytest

from app.models import (
    BackupPool, DockerHost, FileMkdirRequest, FilePermissionsRequest,
    FileRenameRequest, FileTransferRequest,
)
from app.services import task_queue as tq_module
from app.services import volume_service as vs_module
from app.services.file_explorer_service import ExplorerError, FileExplorerService
from app.services.task_queue import TaskQueue
from app.validation import validate_entry_name, validate_explorer_mode, validate_rel_path


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A pool with one volume ``vol`` plus an ``outside`` dir next to the pool
    holding a sentinel file that must never be touched."""
    pool = tmp_path / "pool"
    vol = pool / "vol"
    (vol / "sub" / "deep").mkdir(parents=True)
    (vol / "sub" / "file.txt").write_text("inside")
    (vol / "a b ü.txt").write_text("unicode")

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("TOP SECRET")
    os.chmod(secret, 0o640)

    backups = tmp_path / "backups"
    backups.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    config = SimpleNamespace(
        docker_hosts=[
            DockerHost(name="local", pool=str(pool)),
            DockerHost(name="remote", pool="/unused", pool_type="remote",
                       remote_host="example.invalid", rsync_module="mod"),
        ],
        backup_pools=[BackupPool(name="backup", pool=str(backups))],
    )
    queue = TaskQueue(tmp_dir=str(state), config_dir=str(state))
    monkeypatch.setattr(tq_module, "_task_queue", queue)
    monkeypatch.setattr(vs_module, "_volume_service", None)
    svc = FileExplorerService(config)
    return SimpleNamespace(svc=svc, queue=queue, tmp=tmp_path, pool=pool, vol=vol,
                           outside=outside, secret=secret)


def _run(env, kind, *args):
    """Run a queued op synchronously and return the final task record."""
    task_id = env.queue.add_task(task_type=f"file_{kind}")
    if kind == "delete":
        env.svc.run_delete("local", "vol", *args, task_id)
    else:
        env.svc.run_transfer(kind, "local", "vol", *args, task_id)
    return env.queue.tasks[task_id]


def _assert_no_host_path(env, message):
    assert str(env.tmp) not in message
    assert str(os.path.realpath(env.tmp)) not in message


# ── Input validation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["..", "../x", "/etc", "a/../../b", "a/./b", "x\0y", "x\ny", "a\\b"])
def test_rel_path_rejects_traversal(bad):
    with pytest.raises(ValueError):
        validate_rel_path(bad)


def test_rel_path_normalises():
    assert validate_rel_path("") == ""
    assert validate_rel_path(None) == ""
    assert validate_rel_path("a//b/") == "a/b"
    assert validate_rel_path("dir with space/ü") == "dir with space/ü"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "x\0", "tab\t", "x" * 256])
def test_entry_name_rejects(bad):
    with pytest.raises(ValueError):
        validate_entry_name(bad)


@pytest.mark.parametrize("bad", ["4755", "2755", "6777", "799", "75", "abc"])
def test_mode_rejects_setid_and_garbage(bad):
    with pytest.raises(ValueError):
        validate_explorer_mode(bad)


def test_mode_allows_sticky():
    assert validate_explorer_mode("1777") == "1777"
    assert validate_explorer_mode("755") == "755"


def test_request_models_reject_traversal():
    with pytest.raises(ValueError):
        FileMkdirRequest(pool="local", volume="vol", parent="../outside", name="x")
    with pytest.raises(ValueError):
        FileRenameRequest(pool="local", volume="vol", path="sub", new_name="../x")
    with pytest.raises(ValueError):
        FileRenameRequest(pool="local", volume="vol", path="", new_name="x")
    with pytest.raises(ValueError):
        FileTransferRequest(pool="local", volume="vol", names=["sub/file.txt"], dest_dir="")
    with pytest.raises(ValueError):
        FileTransferRequest(pool="local", volume="vol", names=["a", "a"], dest_dir="")
    with pytest.raises(ValueError):
        FilePermissionsRequest(pool="local", volume="../pool", path="", mode="755")
    with pytest.raises(ValueError):
        FilePermissionsRequest(pool="local", volume="vol", path="", mode="4755")


# ── Pool scope ────────────────────────────────────────────────────────────────

def test_remote_and_backup_pools_rejected(env):
    for pool in ("remote", "backup"):
        with pytest.raises(ExplorerError) as ei:
            env.svc.list_dir(pool, "vol", "")
        assert ei.value.status == 400


def test_volume_that_is_a_symlink_is_rejected(env):
    os.symlink(env.outside, env.pool / "evilvol")
    with pytest.raises(ExplorerError) as ei:
        env.svc.list_dir("local", "evilvol", "")
    assert ei.value.status == 400


# ── Listing ──────────────────────────────────────────────────────────────────

def test_list_shows_symlink_without_following(env):
    os.symlink(env.outside, env.vol / "link_out")
    listing = env.svc.list_dir("local", "vol", "")
    by_name = {e["name"]: e for e in listing["entries"]}
    assert by_name["link_out"]["type"] == "symlink"
    assert by_name["sub"]["type"] == "dir"
    assert by_name["a b ü.txt"]["type"] == "file"
    assert listing["entries"][0]["type"] == "dir"  # folders first


@pytest.mark.parametrize("target_kind", ["outside", "inside"])
def test_intermediate_symlink_refused_everywhere(env, target_kind):
    target = env.outside if target_kind == "outside" else env.vol / "sub"
    os.symlink(target, env.vol / "lnk")
    svc = env.svc
    for call in (
        lambda: svc.list_dir("local", "vol", "lnk"),
        lambda: svc.mkdir("local", "vol", "lnk", "new"),
        lambda: svc.rename("local", "vol", "lnk/secret.txt", "x"),
        lambda: svc.set_permissions("local", "vol", "lnk/secret.txt", "777", None, False),
        lambda: svc.precheck_transfer("copy", "local", "vol", "", ["a b ü.txt"], "lnk"),
        lambda: svc.precheck_transfer("move", "local", "vol", "", ["a b ü.txt"], "lnk"),
        lambda: svc.precheck_delete("local", "vol", "lnk", ["secret.txt"]),
    ):
        with pytest.raises(ExplorerError) as ei:
            call()
        assert ei.value.status == 400
        _assert_no_host_path(env, ei.value.message)
    assert env.secret.read_text() == "TOP SECRET"
    assert stat.S_IMODE(env.secret.stat().st_mode) == 0o640
    assert not (env.outside / "new").exists()


# ── Final-component symlinks ─────────────────────────────────────────────────

def test_delete_symlink_removes_only_link(env):
    os.symlink(env.outside, env.vol / "link_dir")
    os.symlink(env.secret, env.vol / "link_file")
    task = _run(env, "delete", "", ["link_dir", "link_file"])
    assert task["status"] == "completed", task["error"]
    assert not os.path.lexists(env.vol / "link_dir")
    assert not os.path.lexists(env.vol / "link_file")
    assert env.secret.read_text() == "TOP SECRET"


def test_chmod_chown_refuse_symlink(env):
    os.symlink(env.secret, env.vol / "link_file")
    with pytest.raises(ExplorerError) as ei:
        env.svc.set_permissions("local", "vol", "link_file", "777", None, False)
    assert ei.value.status == 400
    assert stat.S_IMODE(env.secret.stat().st_mode) == 0o640


def test_copy_symlink_copies_link_not_target(env):
    os.symlink(env.secret, env.vol / "link_file")
    task = _run(env, "copy", "", ["link_file"], "sub")
    assert task["status"] == "completed", task["error"]
    copied = env.vol / "sub" / "link_file"
    assert copied.is_symlink()
    assert os.readlink(copied) == str(env.secret)


# ── Recursive ops over a tree containing an outward symlink ──────────────────

def test_recursive_ops_never_touch_outside(env):
    os.symlink(env.outside, env.vol / "sub" / "escape")

    res = env.svc.set_permissions("local", "vol", "sub", "777", None, True)
    assert res["skipped_symlinks"] == 1
    assert stat.S_IMODE((env.vol / "sub" / "file.txt").stat().st_mode) == 0o777
    assert stat.S_IMODE(env.secret.stat().st_mode) == 0o640
    assert stat.S_IMODE(env.outside.stat().st_mode) != 0o777

    task = _run(env, "copy", "", ["sub"], "sub/deep")  # dest inside source: refused
    assert task["status"] == "failed"
    (env.vol / "dst").mkdir()
    task = _run(env, "copy", "", ["sub"], "dst")
    assert task["status"] == "completed", task["error"]
    copied_escape = env.vol / "dst" / "sub" / "escape"
    assert copied_escape.is_symlink()  # link copied, outside dir not read
    assert not (env.vol / "dst" / "sub" / "escape").resolve().is_relative_to(env.vol)

    task = _run(env, "delete", "", ["sub", "dst"])
    assert task["status"] == "completed", task["error"]
    assert not (env.vol / "sub").exists()
    assert env.secret.read_text() == "TOP SECRET"


def test_hardlinked_file_not_chmodded(env):
    os.link(env.secret, env.vol / "sub" / "hl")
    res = env.svc.set_permissions("local", "vol", "sub", "777", None, True)
    assert res["skipped_hardlinks"] == 1
    assert stat.S_IMODE(env.secret.stat().st_mode) == 0o640
    with pytest.raises(ExplorerError):
        env.svc.set_permissions("local", "vol", "sub/hl", "777", None, False)
    assert stat.S_IMODE(env.secret.stat().st_mode) == 0o640


# ── Nesting, conflicts, root guards ──────────────────────────────────────────

def test_move_into_itself_refused(env):
    for dest in ("sub", "sub/deep"):
        with pytest.raises(ExplorerError) as ei:
            env.svc.precheck_transfer("move", "local", "vol", "", ["sub"], dest)
        assert ei.value.status == 400


def test_copy_onto_existing_name_conflicts(env):
    (env.vol / "sub" / "a b ü.txt").write_text("existing")
    with pytest.raises(ExplorerError) as ei:
        env.svc.precheck_transfer("copy", "local", "vol", "", ["a b ü.txt"], "sub")
    assert ei.value.status == 409
    assert (env.vol / "sub" / "a b ü.txt").read_text() == "existing"


def test_copy_and_move_happy_path(env):
    task = _run(env, "copy", "", ["a b ü.txt"], "sub/deep")
    assert task["status"] == "completed", task["error"]
    assert (env.vol / "sub" / "deep" / "a b ü.txt").read_text() == "unicode"
    task = _run(env, "move", "sub", ["file.txt"], "")
    assert task["status"] == "completed", task["error"]
    assert (env.vol / "file.txt").read_text() == "inside"
    assert not (env.vol / "sub" / "file.txt").exists()


def test_rename_mkdir_and_conflict(env):
    env.svc.mkdir("local", "vol", "sub", "new folder")
    assert (env.vol / "sub" / "new folder").is_dir()
    with pytest.raises(ExplorerError) as ei:
        env.svc.mkdir("local", "vol", "sub", "new folder")
    assert ei.value.status == 409
    env.svc.rename("local", "vol", "sub/new folder", "renamed")
    assert (env.vol / "sub" / "renamed").is_dir()
    with pytest.raises(ExplorerError) as ei:
        env.svc.rename("local", "vol", "sub/renamed", "file.txt")
    assert ei.value.status == 409


def test_locked_volume_refuses_immediate_ops(env):
    lock = env.queue.create_lockfile("local", "vol")
    try:
        with pytest.raises(ExplorerError) as ei:
            env.svc.mkdir("local", "vol", "", "x")
        assert ei.value.status == 409
    finally:
        env.queue.remove_lockfile(lock)


def test_errors_never_leak_host_paths(env):
    for call in (
        lambda: env.svc.list_dir("local", "vol", "missing/dir"),
        lambda: env.svc.list_dir("local", "nope", ""),
        lambda: env.svc.rename("local", "vol", "missing", "x"),
        lambda: env.svc.precheck_delete("local", "vol", "", ["missing"]),
    ):
        with pytest.raises(ExplorerError) as ei:
            call()
        assert ei.value.status == 404
        _assert_no_host_path(env, ei.value.message)


def test_cancel_mid_copy_removes_partial_copy(env, monkeypatch):
    for i in range(5):
        (env.vol / "sub" / f"f{i}").write_bytes(b"x" * 1000)
    calls = {"n": 0}

    def cancel_after_a_few(task_id):
        calls["n"] += 1
        return calls["n"] > 4

    monkeypatch.setattr(env.queue, "is_cancelled", cancel_after_a_few)
    (env.vol / "dst").mkdir()
    task = _run(env, "copy", "", ["sub"], "dst")
    assert task["status"] == "cancelled"
    assert not (env.vol / "dst" / "sub").exists()
    assert (env.vol / "sub" / "f4").exists()  # source untouched


def test_json_content_type_required():
    from fastapi import HTTPException
    from app.api.routes import _require_json

    def req(ctype):
        return SimpleNamespace(headers={"content-type": ctype} if ctype else {})

    _require_json(req("application/json"))
    _require_json(req("application/json; charset=utf-8"))
    for bad in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
        with pytest.raises(HTTPException) as ei:
            _require_json(req(bad))
        assert ei.value.status_code == 415
