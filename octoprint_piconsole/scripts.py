# coding=utf-8
from __future__ import absolute_import

import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import OrderedDict

from .buffer import OutputBuffer

SCRIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
META_LINE = re.compile(r"^#\s*@(\w+)\s*:\s*(.*?)\s*$")
MAX_SCRIPT_SIZE = 256 * 1024
JOB_BUFFER_SIZE = 512 * 1024
KEEP_FINISHED_JOBS = 20


def valid_id(script_id):
    return bool(script_id) and SCRIPT_ID.match(script_id) is not None


def normalize(content):
    # scripts written or uploaded from Windows would otherwise fail with "$'\r': command not found"
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if not content.endswith("\n"):
        content += "\n"
    return content


def _flag(value, default):
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def parse_meta(content):
    """Reads ``# @key: value`` lines from the top of a script."""
    meta = {}
    for line in content.splitlines()[:40]:
        match = META_LINE.match(line)
        if match:
            meta[match.group(1).lower()] = match.group(2)
    return meta


class ScriptStore(object):
    """User scripts as ``<id>.sh`` files in the plugin data folder."""

    def __init__(self, folder, defaults_folder):
        self.folder = folder
        self.defaults_folder = defaults_folder
        self._marker = os.path.join(os.path.dirname(folder), "installed_defaults.json")
        if not os.path.isdir(folder):
            os.makedirs(folder)

    def path(self, script_id):
        return os.path.join(self.folder, script_id + ".sh")

    def default_ids(self):
        return sorted(
            name[:-3] for name in os.listdir(self.defaults_folder) if name.endswith(".sh")
        )

    def exists(self, script_id):
        return valid_id(script_id) and os.path.isfile(self.path(script_id))

    def read(self, script_id):
        with io.open(self.path(script_id), "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def info(self, script_id):
        if not self.exists(script_id):
            return None
        meta = parse_meta(self.read(script_id))
        return dict(
            id=script_id,
            name=meta.get("name") or script_id,
            description=meta.get("description", ""),
            confirm=_flag(meta.get("confirm"), True),
            block_while_printing=_flag(meta.get("block_while_printing"), False),
            builtin=script_id in self.default_ids(),
            modified=os.path.getmtime(self.path(script_id)),
        )

    def list(self):
        ids = sorted(name[:-3] for name in os.listdir(self.folder) if name.endswith(".sh"))
        scripts = [self.info(script_id) for script_id in ids if valid_id(script_id)]
        return sorted(scripts, key=lambda s: s["name"].lower())

    def save(self, script_id, content):
        with io.open(self.path(script_id), "w", encoding="utf-8", newline="\n") as f:
            f.write(normalize(content))

    def delete(self, script_id):
        os.remove(self.path(script_id))

    def install_defaults(self):
        """Copies built-in scripts that were never installed before.

        Deleted built-ins stay deleted, new ones from a plugin update show up.
        """
        try:
            with io.open(self._marker, "r", encoding="utf-8") as f:
                installed = set(json.load(f))
        except (IOError, OSError, ValueError):
            installed = set()

        added = []
        for script_id in self.default_ids():
            if script_id in installed:
                continue
            if not os.path.exists(self.path(script_id)):
                shutil.copyfile(os.path.join(self.defaults_folder, script_id + ".sh"), self.path(script_id))
                added.append(script_id)
            installed.add(script_id)

        with io.open(self._marker, "w", encoding="utf-8") as f:
            f.write(json.dumps(sorted(installed)))
        return added

    def restore_defaults(self):
        """Overwrites all built-in scripts with their original version."""
        for script_id in self.default_ids():
            shutil.copyfile(os.path.join(self.defaults_folder, script_id + ".sh"), self.path(script_id))


class Job(object):
    def __init__(self, script_id, name, content, owner, tmp_folder, on_finished):
        self.id = uuid.uuid4().hex[:12]
        self.script = script_id
        self.name = name
        self.owner = owner
        self.status = "running"
        self.exit_code = None
        self.started = time.time()
        self.finished = None
        self._killed = False
        self._on_finished = on_finished
        self._output = OutputBuffer(JOB_BUFFER_SIZE)

        # run a snapshot, bash reads scripts while executing and editing one mid-run breaks it
        fd, self._tmp_path = tempfile.mkstemp(prefix=script_id + "-", suffix=".sh", dir=tmp_folder)
        with io.open(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(normalize(content))

        env = dict(os.environ)
        env["TERM"] = "dumb"
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["PICONSOLE_SCRIPT"] = script_id
        home = env.get("HOME") or os.path.expanduser("~")

        self._proc = subprocess.Popen(
            ["/bin/bash", self._tmp_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=home,
            env=env,
            start_new_session=True,
        )
        thread = threading.Thread(target=self._read_loop, name="piconsole-job-" + self.id)
        thread.daemon = True
        thread.start()

    @property
    def running(self):
        return self.status == "running"

    def info(self):
        return dict(
            id=self.id,
            script=self.script,
            name=self.name,
            owner=self.owner,
            status=self.status,
            exit_code=self.exit_code,
            started=self.started,
            finished=self.finished,
        )

    def read(self, offset=None):
        return self._output.read(offset)

    def kill(self):
        if not self.running:
            return
        self._killed = True
        self._signal(signal.SIGTERM)

        def force_kill():
            time.sleep(5)
            if self.running:
                self._signal(signal.SIGKILL)

        killer = threading.Thread(target=force_kill)
        killer.daemon = True
        killer.start()

    def _signal(self, sig):
        try:
            os.killpg(self._proc.pid, sig)
        except OSError:
            pass

    def _read_loop(self):
        fd = self._proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            self._output.append(data)
        self._proc.stdout.close()
        self.exit_code = self._proc.wait()
        self.finished = time.time()
        try:
            os.remove(self._tmp_path)
        except OSError:
            pass

        if self._killed:
            self.status = "killed"
        elif self.exit_code == 0:
            self.status = "success"
        else:
            self.status = "failed"
        self._output.append(
            "\n[{} - exit code {}, {:.1f}s]\n".format(
                self.status, self.exit_code, self.finished - self.started
            ).encode("utf-8")
        )
        self._on_finished(self)


class ScriptRunner(object):
    def __init__(self, tmp_folder, logger):
        self._tmp_folder = tmp_folder
        self._logger = logger
        self._jobs = OrderedDict()
        self._lock = threading.Lock()
        if not os.path.isdir(tmp_folder):
            os.makedirs(tmp_folder)

    def start(self, script_id, name, content, owner):
        with self._lock:
            if any(j.running and j.script == script_id for j in self._jobs.values()):
                return None
            job = Job(script_id, name, content, owner, self._tmp_folder, self._finished)
            self._jobs[job.id] = job
            self._prune()
            return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self):
        with self._lock:
            return [job.info() for job in reversed(list(self._jobs.values()))]

    def kill_all(self):
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.kill()

    def _finished(self, job):
        self._logger.info(
            "Script %s (job %s, started by %s) finished: %s, exit code %s",
            job.script, job.id, job.owner, job.status, job.exit_code,
        )

    def _prune(self):
        finished = [j for j in self._jobs.values() if not j.running]
        for job in finished[:max(0, len(finished) - KEEP_FINISHED_JOBS)]:
            del self._jobs[job.id]
