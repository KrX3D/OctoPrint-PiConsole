# coding=utf-8
from __future__ import absolute_import

import os
import shutil
import subprocess

ROOT = "root"  # OctoPrint runs as root, no sudo needed
NOPASSWD = "nopasswd"  # passwordless sudo
PASSWORD = "password"  # sudo asks for the user's password
NONE = "none"  # no sudo binary

# Prepended to scripts when sudo needs a password: turns "sudo -n ..." into "sudo -A ..."
# so the askpass helper answers with the password the user entered. Kept on one line so
# error messages from the script are only off by one line.
SUDO_PREAMBLE = (
    'sudo() { local o=(); while [ $# -gt 0 ]; do case "$1" in -n) shift ;; '
    '-u|-g|-p|-C|-D|-h|-r|-t|-U|-T) o+=("$1" "$2"); shift 2 ;; --) shift; break ;; '
    '-*) o+=("$1"); shift ;; *) break ;; esac; done; command sudo -A "${o[@]}" "$@"; }; '
    "export -f sudo\n"
)

# Used when OctoPrint runs as root on a system without sudo (e.g. containers)
ROOT_PREAMBLE = (
    "command -v sudo >/dev/null 2>&1 || { sudo() { while [ $# -gt 0 ]; do case \"$1\" in "
    "-u|-g|-p|-C|-D|-h|-r|-t|-U|-T) shift 2 ;; --) shift; break ;; -*) shift ;; *) break ;; esac; done; "
    '"$@"; }; export -f sudo; }\n'
)

ASKPASS_SCRIPT = '#!/bin/sh\nexec cat "$PICONSOLE_SUDO_PWFILE"\n'


def run(args, input=None, timeout=60):
    try:
        proc = subprocess.run(
            args,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def detect_mode():
    if os.geteuid() == 0:
        return ROOT
    if not shutil.which("sudo"):
        return NONE
    return NOPASSWD if run(["sudo", "-n", "true"], timeout=10)[0] == 0 else PASSWORD


class Sudo(object):
    """Runs commands as root the way the system allows it."""

    def __init__(self, password=None):
        self.mode = detect_mode()
        self.password = password if self.mode == PASSWORD and password else None

    @property
    def available(self):
        return self.mode in (ROOT, NOPASSWD) or self.password is not None

    def run(self, args, input=None, timeout=60):
        args = list(args)
        if self.mode == ROOT:
            return run(args, input=input, timeout=timeout)
        if self.password is None:
            return run(["sudo", "-n"] + args, input=input, timeout=timeout)
        # -k: never use cached credentials, so the first stdin line is always the password
        data = self.password + "\n" + (input or "")
        return run(["sudo", "-S", "-k", "-p", ""] + args, input=data, timeout=timeout)

    def verify(self):
        """Returns ``None`` if sudo works, otherwise an error message."""
        if self.mode == NONE:
            return "sudo is not installed"
        if not self.available:
            return "sudo needs the password of user {}".format(_user())
        rc, out, err = self.run(["true"], timeout=15)
        if rc == 0:
            return None
        if "not in the sudoers" in err or "may not run sudo" in err:
            return "User {} is not allowed to use sudo".format(_user())
        if self.password is not None:
            return "Wrong sudo password"
        return err or out or "sudo failed"


def _user():
    import getpass

    return getpass.getuser()


def write_askpass(folder):
    path = os.path.join(folder, "askpass.sh")
    with open(path, "w") as f:
        f.write(ASKPASS_SCRIPT)
    os.chmod(path, 0o700)
    return path
