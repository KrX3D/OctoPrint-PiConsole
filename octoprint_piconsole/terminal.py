# coding=utf-8
from __future__ import absolute_import

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
import uuid

from .buffer import OutputBuffer

TERMINAL_BUFFER_SIZE = 256 * 1024
RESET_SIGNALS = [
    getattr(signal, name)
    for name in ("SIGINT", "SIGQUIT", "SIGPIPE", "SIGTERM", "SIGHUP", "SIGCHLD", "SIGXFSZ", "SIGTSTP")
    if hasattr(signal, name)
]


class TerminalSession(object):
    """A login shell running in a pseudo terminal, owned by one OctoPrint user."""

    def __init__(self, owner, shell, rows, cols):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.shell = shell
        self.alive = True
        self.exit_code = None
        self.created = self.last_activity = time.time()
        self._output = OutputBuffer(TERMINAL_BUFFER_SIZE)

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env.setdefault("LANG", "C.UTF-8")
        home = env.get("HOME") or os.path.expanduser("~")

        pid, fd = pty.fork()
        if pid == 0:  # child: become the shell, never return into OctoPrint code
            try:
                # ignored signals survive exec (Python ignores SIGPIPE, a service may ignore SIGINT),
                # which would break Ctrl+C and pipes inside the shell
                for sig in RESET_SIGNALS:
                    try:
                        signal.signal(sig, signal.SIG_DFL)
                    except (OSError, ValueError):
                        pass
                os.chdir(home)
                os.execvpe(shell, [shell, "-l"], env)
            finally:
                os._exit(127)

        self.pid = pid
        self.fd = fd
        self.resize(rows, cols)

        thread = threading.Thread(target=self._read_loop, name="piconsole-pty-{}".format(pid))
        thread.daemon = True
        thread.start()

    def info(self):
        return dict(id=self.id, alive=self.alive, exit_code=self.exit_code, pid=self.pid)

    def read(self, offset=None):
        self.last_activity = time.time()
        return self._output.read(offset)

    def write(self, data):
        self.last_activity = time.time()
        if not self.alive:
            return
        while data:
            written = os.write(self.fd, data)
            data = data[written:]

    def resize(self, rows, cols):
        if not self.alive:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def close(self):
        if not self.alive:
            return
        self._signal(signal.SIGHUP)

        def force_kill():
            time.sleep(3)
            if self.alive:
                self._signal(signal.SIGKILL)

        killer = threading.Thread(target=force_kill)
        killer.daemon = True
        killer.start()

    def _signal(self, sig):
        try:
            os.killpg(self.pid, sig)
        except OSError:
            try:
                os.kill(self.pid, sig)
            except OSError:
                pass

    def _read_loop(self):
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 1.0)
                if not ready:
                    # background jobs can keep the pty open after the shell itself exited
                    if self._child_exited():
                        break
                    continue
                data = os.read(self.fd, 65536)
            except (OSError, ValueError) as e:
                if getattr(e, "errno", None) == errno.EINTR:
                    continue
                break  # EIO: the shell closed the pty
            if not data:
                break
            self._output.append(data)

        if self.exit_code is None:
            try:
                _, status = os.waitpid(self.pid, 0)
                self.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
            except OSError:
                self.exit_code = -1
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.alive = False

    def _child_exited(self):
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            self.exit_code = -1
            return True
        if pid == 0:
            return False
        self.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        return True


class TerminalManager(object):
    """One terminal session per user, so a page reload reconnects to the same shell."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get(self, owner):
        with self._lock:
            return self._sessions.get(owner)

    def open(self, owner, shell, rows, cols, restart=False):
        """Returns ``(session, created)``."""
        with self._lock:
            session = self._sessions.get(owner)
            if session is not None and session.alive and not restart:
                session.resize(rows, cols)
                return session, False
            if session is not None:
                session.close()
            session = TerminalSession(owner, shell, rows, cols)
            self._sessions[owner] = session
            return session, True

    def close(self, owner):
        with self._lock:
            session = self._sessions.pop(owner, None)
        if session is not None:
            session.close()
        return session

    def reap_idle(self, timeout):
        """Closes sessions nobody polled for ``timeout`` seconds. Returns the closed sessions."""
        now = time.time()
        with self._lock:
            idle = [s for s in self._sessions.values() if now - s.last_activity > timeout]
            for session in idle:
                del self._sessions[session.owner]
        for session in idle:
            session.close()
        return idle

    def close_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
