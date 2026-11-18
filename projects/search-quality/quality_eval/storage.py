import contextlib
import os
from pathlib import Path

from quality_input.common import InputError, digest, read_json, write_json
from quality_input.pipeline import utc_now


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    write_json(temp, value)
    os.replace(temp, path)


def checked_write(path, value):
    atomic_json(path, {**value, "checksum": digest(value)})


def checked_read(path):
    value = read_json(path)
    if not isinstance(value, dict):
        raise InputError(f"Invalid checkpoint: {Path(path).name}")
    checksum = value.pop("checksum", None)
    if checksum != digest(value):
        raise InputError(f"Checkpoint checksum mismatch: {Path(path).name}")
    return value


@contextlib.contextmanager
def run_directory(output, identity, resume=False):
    root = Path(output).resolve()
    if not resume:
        root.mkdir(parents=True, exist_ok=False)
    elif not root.is_dir():
        raise InputError("Resume requires an existing run directory")
    lock = root / ".run.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise InputError("Run directory is locked by another process; inspect before removing a stale lock") from exc
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        manifest_path = root / "manifest.json"
        if resume:
            old = checked_read(manifest_path)
            if old["identity"] != identity:
                raise InputError("Resume configuration/data/prompts changed; use a new output directory")
        else:
            checked_write(manifest_path, {"identity": identity, "created_at": utc_now()})
        (root / "tasks").mkdir(exist_ok=True)
        yield root
    finally:
        lock.unlink(missing_ok=True)
