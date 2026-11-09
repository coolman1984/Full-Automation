from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


class AutomationError(RuntimeError):
    pass


def data_directory() -> Path:
    override = os.environ.get("FULL_AUTOMATION_DATA")
    base = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "FullAutomation"
    base = base.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        base.chmod(0o700)
    return base


def slug(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\w][\w.-]{0,79}", value, re.UNICODE) or value in {".", ".."}:
        raise AutomationError("Use a name of 1–80 letters, numbers, underscores, dots or hyphens; no paths.")
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *("COM"+str(i) for i in range(1,10)), *("LPT"+str(i) for i in range(1,10))}:
        raise AutomationError("Reserved Windows file name.")
    return value


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def read_json(path: Path, limit: int = 8_000_000):
    if path.stat().st_size > limit:
        raise AutomationError("JSON file exceeds the configured size limit.")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


SECRET_KEY = re.compile(r"(?:password|passwd|secret|token|authorization|api[_-]?key|cookie)", re.I)
TOKEN = re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._~+/-]{8,})", re.I)
ASSIGNMENT = re.compile(r'(?i)((?:password|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;}]+)')


def redact(value):
    """Best-effort log redaction, not a guarantee that arbitrary prose is secret-free."""
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return ASSIGNMENT.sub(r"\1[REDACTED]", TOKEN.sub("[REDACTED]", value))
    return value


class Journal:
    MAX_BYTES = 10 * 1024 * 1024
    KEEP = 3

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def _rotate(self):
        # actions.jsonl -> actions.1.jsonl -> ... -> actions.3.jsonl; the oldest is dropped.
        try:
            if self.path.stat().st_size < self.MAX_BYTES:
                return
        except FileNotFoundError:
            return
        for index in range(self.KEEP, 0, -1):
            older = self.path.with_name(f"{self.path.stem}.{index}{self.path.suffix}")
            newer = self.path if index == 1 else self.path.with_name(f"{self.path.stem}.{index-1}{self.path.suffix}")
            if newer.exists():
                os.replace(newer, older)

    def add(self, event: dict):
        record = {"time": time.time(), **redact(event)}
        # Never put images or entire page/chat bodies into the action journal.
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            with self.path.open("a", encoding="utf-8") as file:
                file.write(encoded + "\n")


class RunLock:
    """An OS-released lock: killed processes cannot leave a permanently stale lock."""
    def __init__(self, path: Path):
        self.path = path
        self.local = threading.RLock()
        self.depth = 0
        self.file = None

    def __enter__(self):
        # Never wait: the MCP stdio loop is single-threaded, so blocking here while a
        # background workflow owns the device would stall status and cancel requests.
        if not self.local.acquire(blocking=False):
            raise AutomationError("Another automation owns the device. Retry after it finishes.")
        if self.depth:
            self.depth += 1
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("a+b")
            self.file.seek(0, 2)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.depth = 1
            return self
        except OSError as exc:
            if self.file:
                self.file.close()
                self.file = None
            self.local.release()
            raise AutomationError("Another automation owns the device. Retry after it finishes.") from exc

    def __exit__(self, *args):
        self.depth -= 1
        if not self.depth:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None
        self.local.release()
