"""Evidence for a named output file without parsing its format."""
import hashlib
import os
from pathlib import Path
import stat

from .common import AutomationError


def _stamp(info):
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev


def inspect_file(path, with_sha256=False):
    file = Path(path).expanduser().resolve()
    try:
        info = file.stat()
    except FileNotFoundError:
        return {"path": str(file), "exists": False}
    if not stat.S_ISREG(info.st_mode):
        raise AutomationError("Expected a regular file.")
    result = {"path": str(file), "exists": True, "bytes": info.st_size, "modified_at": info.st_mtime}
    if with_sha256:
        sha256 = hashlib.sha256()
        with file.open("rb") as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(info):
                raise AutomationError("Output changed before hashing.")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                sha256.update(block)
        if _stamp(file.stat()) != _stamp(info):
            raise AutomationError("Output changed while hashing.")
        result["sha256"] = sha256.hexdigest()
    return result
