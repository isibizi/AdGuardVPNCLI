"""Helpers for driving external commands.

Everything that touches `adguardvpn-cli` goes through the same lock: the client
keeps global state (mode, connection, session) and two concurrent commands can
leave it in a confusing half-state. Web requests and the watchdog both take it.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import select
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from app.config import SETTINGS

# The panel and the watchdog are separate processes, so a thread lock alone is
# not enough - the lock has to be visible across processes. A file lock on the
# data volume does that; the thread lock keeps it re-entrant within a process.
_THREAD_LOCK = threading.RLock()
_LOCK_PATH = SETTINGS.data_dir / "vpn.lock"
_depth = threading.local()


@contextmanager
def vpn_lock(blocking: bool = True) -> Iterator[bool]:
    """Serialise access to adguardvpn-cli.

    Yields True when the lock was acquired. With blocking=False it yields False
    immediately if somebody else is working - the watchdog uses that to skip a
    cycle instead of queueing up behind a five-minute login flow.
    """
    if getattr(_depth, "value", 0) > 0:  # already held by this thread
        _depth.value += 1
        try:
            yield True
        finally:
            _depth.value -= 1
        return

    if not _THREAD_LOCK.acquire(blocking=blocking):
        yield False
        return

    handle = None
    try:
        SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
        handle = _LOCK_PATH.open("w")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                yield False
                return
            raise
        _depth.value = 1
        try:
            yield True
        finally:
            _depth.value = 0
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
        _THREAD_LOCK.release()


def acquire_lock_handle():
    """Take the lock and keep it until the handle is released.

    The device-code login runs across several HTTP requests and can take
    minutes, so it cannot sit inside a `with` block. It holds this handle
    instead; the watchdog's non-blocking acquire then simply skips its cycle
    rather than racing the login.
    """
    SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
    handle = _LOCK_PATH.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_lock_handle(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\r")


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences so output can be parsed and displayed."""
    return _ANSI.sub("", text)


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


def run(args: list[str], timeout: int = 60, env: dict[str, str] | None = None) -> Result:
    """Run a command, never raising for a non-zero exit.

    Output goes to temporary files rather than pipes, and only the direct child
    is waited for. That distinction matters: `adguardvpn-cli connect` forks the
    VPN service into the background and exits. The forked service inherits the
    standard streams, so with pipes the read would block until that service
    ends - minutes or hours later - and every connect attempt would look like a
    timeout even though it had succeeded. A file descriptor the daemon keeps
    open costs us nothing.
    """
    merged = dict(os.environ)
    if env:
        merged.update(env)

    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(
                args, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=merged,
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                out.seek(0)
                err.seek(0)
                partial = strip_ansi((out.read() + err.read()).decode("utf-8", "replace"))
                detail = f"\n{partial.strip()[-400:]}" if partial.strip() else ""
                return Result(124, "", f"Zeitüberschreitung nach {timeout}s: {' '.join(args)}{detail}")

            out.seek(0)
            err.seek(0)
            return Result(
                returncode,
                strip_ansi(out.read().decode("utf-8", "replace")),
                strip_ansi(err.read().decode("utf-8", "replace")),
            )
    except OSError as exc:
        return Result(127, "", f"Konnte '{args[0]}' nicht ausführen: {exc}")


@dataclass
class PtySession:
    """A command running on a pseudo-terminal.

    `adguardvpn-cli login` draws an interactive menu and reads from the
    controlling terminal. Run through a plain pipe it either aborts or blocks
    forever, so the login flow needs a real PTY that we can also write to (to
    answer its menu).
    """

    args: list[str]
    _pid: int = 0
    _fd: int = -1
    buffer: str = ""
    finished: bool = False
    returncode: int | None = None
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        pid, fd = pty.fork()
        if pid == 0:  # child
            env = dict(os.environ, TERM="dumb", NO_COLOR="1")
            try:
                os.execvpe(self.args[0], self.args, env)
            finally:
                os._exit(127)
        self._pid, self._fd = pid, fd

    def read_available(self) -> str:
        """Drain whatever the process has written since the last call."""
        if self._fd < 0:
            return ""
        chunks: list[str] = []
        while True:
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                data = os.read(self._fd, 8192)
            except OSError:
                self._reap()
                break
            if not data:
                self._reap()
                break
            chunks.append(data.decode("utf-8", "replace"))

        text = strip_ansi("".join(chunks))
        if text:
            with self._lock:
                self.buffer += text
        self._poll()
        return text

    def send(self, text: str) -> None:
        if self._fd >= 0 and not self.finished:
            try:
                os.write(self._fd, text.encode())
            except OSError:
                pass

    def _poll(self) -> None:
        if self.finished or self._pid <= 0:
            return
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            self._reap()
            return
        if pid:
            self.finished = True
            self.returncode = os.waitstatus_to_exitcode(status)

    def _reap(self) -> None:
        self.finished = True
        if self.returncode is None:
            try:
                _, status = os.waitpid(self._pid, 0)
                self.returncode = os.waitstatus_to_exitcode(status)
            except (ChildProcessError, OSError):
                self.returncode = -1

    def stop(self) -> None:
        if self._pid > 0 and not self.finished:
            try:
                os.kill(self._pid, 15)
            except OSError:
                pass
        self._reap()
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
