# coding=utf-8
from __future__ import absolute_import

import hashlib
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
from .sudo import ROOT, ROOT_PREAMBLE, SUDO_PREAMBLE, write_askpass

SCRIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
META_LINE = re.compile(r"^#\s*@(\w+)\s*:\s*(.*?)\s*$")
MAX_SCRIPT_SIZE = 256 * 1024
JOB_BUFFER_SIZE = 512 * 1024
KEEP_FINISHED_JOBS = 20

# sha256 of built-in scripts shipped by earlier versions, so unedited copies get updated
PREVIOUS_DEFAULTS = {
    "check_ssh": {"7f9efcea909bcec7f0392ffbd16b814d9f6c450a560d92fa05df5f577c2f8bbb"},
    "check_updates": {"8b8d3a991b2861e6479000612074b5afbd548a12f5256fb6354e4e748fbc7233"},
    "enable_ssh": {
        "481b185c6b924bc94406d9953cb28e4029af8524d7020c73cdf978ebfea2e94f",
        "ad9dcd3897fe5c9962aa4888ba51bf46ee48ef196c3efb3f1ec1db1f94f5b10f",
    },
    "run_updates": {"b5f68c67da9b7efb754bcb1ec983d453e92089f2b6c32cab8b4278040688b761"},
    "check_system_files": {"461538ceeaad03035133ec3df708a0315f1b919fed9410cd61089ef58dc01cbb"},
    "system_info": {"9401b2c5086f8b84ef58f4d39596382d82883a4a190baf83dfc63c765892182d"},
}


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


def _sudo_need(value):
    """``@sudo`` header: "required", "optional" or "no"."""
    if value is not None and value.strip().lower() == "optional":
        return "optional"
    return "required" if _flag(value, False) else "no"


def _hash_file(path):
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        return hashlib.sha256(normalize(f.read()).encode("utf-8")).hexdigest()


def _session_members(sid):
    """PIDs of all processes in session ``sid`` (Linux /proc)."""
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/{}/stat".format(entry), "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            # the command name may contain spaces, fields after it are fixed
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[3]) == sid:
                pids.append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return pids


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
            sudo=_sudo_need(meta.get("sudo")),
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

    def _default_path(self, script_id):
        return os.path.join(self.defaults_folder, script_id + ".sh")

    def _load_marker(self):
        """Built-in id -> hash of the version that was installed (None if unknown)."""
        try:
            with io.open(self._marker, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (IOError, OSError, ValueError):
            return {}
        if isinstance(data, list):  # 0.1.0 stored only the ids
            return dict((script_id, None) for script_id in data)
        return data if isinstance(data, dict) else {}

    def _save_marker(self, marker):
        with io.open(self._marker, "w", encoding="utf-8") as f:
            f.write(json.dumps(marker, sort_keys=True))

    def _install(self, script_id, marker):
        shutil.copyfile(self._default_path(script_id), self.path(script_id))
        marker[script_id] = _hash_file(self._default_path(script_id))

    def install_defaults(self):
        """Installs new built-in scripts and updates built-ins the user never changed.

        Deleted built-ins stay deleted and edited ones are left alone.
        Returns ``(added, updated)``.
        """
        marker = self._load_marker()
        added, updated = [], []
        for script_id in self.default_ids():
            if not os.path.exists(self.path(script_id)):
                if script_id not in marker:
                    self._install(script_id, marker)
                    added.append(script_id)
                continue

            shipped = _hash_file(self._default_path(script_id))
            current = _hash_file(self.path(script_id))
            if current == shipped:
                marker[script_id] = shipped
                continue
            unchanged = PREVIOUS_DEFAULTS.get(script_id, set()) | {marker.get(script_id)}
            if current in unchanged:
                self._install(script_id, marker)
                updated.append(script_id)
            else:
                marker.setdefault(script_id, None)

        self._save_marker(marker)
        return added, updated

    def restore_defaults(self):
        """Overwrites all built-in scripts with their original version."""
        marker = self._load_marker()
        for script_id in self.default_ids():
            self._install(script_id, marker)
        self._save_marker(marker)


class Job(object):
    def __init__(self, script_id, name, content, owner, tmp_folder, extra_env, sudo, on_finished):
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

        env = dict(os.environ)
        env.update(extra_env)

        preamble = ""
        self._pw_path = None
        if sudo.mode == ROOT:
            preamble = ROOT_PREAMBLE
        elif sudo.password is not None:
            # the askpass helper reads the password from a private file deleted after the run
            fd, self._pw_path = tempfile.mkstemp(prefix="pw-", dir=tmp_folder)
            with io.open(fd, "w", encoding="utf-8") as f:
                f.write(sudo.password + "\n")
            env["SUDO_ASKPASS"] = write_askpass(tmp_folder)
            env["PICONSOLE_SUDO_PWFILE"] = self._pw_path
            preamble = SUDO_PREAMBLE

        # run a snapshot, bash reads scripts while executing and editing one mid-run breaks it
        fd, self._tmp_path = tempfile.mkstemp(prefix=script_id + "-", suffix=".sh", dir=tmp_folder)
        with io.open(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(preamble + normalize(content))

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
        # tools like timeout move into their own process group but stay in the job's session
        for pid in _session_members(self._proc.pid):
            try:
                os.kill(pid, sig)
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
        for path in (self._tmp_path, self._pw_path):
            if path is None:
                continue
            try:
                os.remove(path)
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
    def __init__(self, tmp_folder, logger, env=None):
        self._tmp_folder = tmp_folder
        self._logger = logger
        self._env = env or {}
        self._jobs = OrderedDict()
        self._lock = threading.Lock()
        if not os.path.isdir(tmp_folder):
            os.makedirs(tmp_folder)

    def start(self, script_id, name, content, owner, sudo):
        with self._lock:
            if any(j.running and j.script == script_id for j in self._jobs.values()):
                return None
            job = Job(script_id, name, content, owner, self._tmp_folder, self._env, sudo, self._finished)
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
