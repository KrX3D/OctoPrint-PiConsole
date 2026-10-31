# coding=utf-8
from __future__ import absolute_import

import getpass
import os
import pwd
import re

from .sudo import run

SSHD = "/usr/sbin/sshd"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
SSHD_CONFIG = "/etc/ssh/sshd_config"
DROPIN_DIR = "/etc/ssh/sshd_config.d"
DROPIN = DROPIN_DIR + "/piconsole.conf"
SERVICE = "ssh.service"
SOCKET = "ssh.socket"
INCLUDE_LINE = re.compile(r"^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf\s*$", re.IGNORECASE | re.MULTILINE)
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class SshError(Exception):
    pass


def _check(result, what):
    rc, out, err = result
    if rc != 0:
        raise SshError("{} failed: {}".format(what, err or out or "exit code {}".format(rc)))
    return out


def _read_file(path, sudo):
    try:
        with open(path) as f:
            return f.read()
    except IOError as e:
        if getattr(e, "errno", None) == 2:  # ENOENT
            return None
    if not sudo.available:
        return None
    rc, out, _ = sudo.run(["cat", path])
    return out if rc == 0 else None


def include_supported(sudo):
    """Only sshd configs that include sshd_config.d/*.conf can be managed (Debian 11+)."""
    config = _read_file(SSHD_CONFIG, sudo)
    return bool(config) and INCLUDE_LINE.search(config) is not None


def denied_users(sudo):
    text = _read_file(DROPIN, sudo) or ""
    denied = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].lower() == "denyusers":
            denied.extend(parts[1:])
    return denied


def login_users():
    users = []
    for entry in pwd.getpwall():
        if not 1000 <= entry.pw_uid < 65534:
            continue
        if not entry.pw_shell or entry.pw_shell.endswith(("nologin", "false")):
            continue
        users.append(entry)
    return sorted(users, key=lambda e: e.pw_name)


def _password_state(name, sudo):
    if not sudo.available:
        return None
    rc, out, _ = sudo.run(["passwd", "-S", name], timeout=10)
    if rc != 0 or not out:
        return None
    parts = out.split()
    return parts[1] if len(parts) > 1 else None  # P = set, L = locked, NP = none


def _unit_state(command, unit):
    return run(["systemctl", command, unit], timeout=10)[1] or "unknown"


def service_status():
    status = dict(
        installed=os.path.exists(SSHD),
        enabled=_unit_state("is-enabled", SERVICE),
        active=_unit_state("is-active", SERVICE),
    )
    # newer Debian can start sshd on demand via ssh.socket
    socket_active = _unit_state("is-active", SOCKET) == "active"
    status["running"] = status["active"] == "active" or socket_active
    return status


def overview(sudo):
    denied = set(denied_users(sudo))
    me = getpass.getuser()
    users = [
        dict(
            name=entry.pw_name,
            uid=entry.pw_uid,
            home=entry.pw_dir,
            shell=entry.pw_shell,
            password=_password_state(entry.pw_name, sudo),
            ssh_allowed=entry.pw_name not in denied,
            octoprint_user=entry.pw_name == me,
        )
        for entry in login_users()
    ]
    rc, ips, _ = run(["hostname", "-I"], timeout=5)
    return dict(
        sudo_mode=sudo.mode,
        sudo_available=sudo.available,
        include_supported=include_supported(sudo),
        service=service_status(),
        users=users,
        ips=ips.split() if rc == 0 else [],
        dropin=DROPIN,
    )


def _require_user(name):
    if name not in [entry.pw_name for entry in login_users()]:
        raise SshError("{} is not a login user".format(name))


def validate_password(password):
    if not isinstance(password, str):
        return "Invalid password"
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return "The password needs {} to {} characters".format(MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH)
    if any(c in password for c in "\r\n\0"):
        return "The password must not contain line breaks"
    return None


def set_password(name, password, sudo):
    _require_user(name)
    error = validate_password(password)
    if error:
        raise SshError(error)
    # via stdin, so the password never shows up in a process list
    _check(sudo.run(["chpasswd"], input="{}:{}\n".format(name, password)), "chpasswd")


def _write_dropin(denied, sudo):
    if not denied:
        _check(sudo.run(["rm", "-f", DROPIN]), "Removing " + DROPIN)
        return
    content = (
        "# Managed by OctoPrint-PiConsole: users blocked from SSH login\n"
        "DenyUsers {}\n".format(" ".join(sorted(denied)))
    )
    _check(sudo.run(["mkdir", "-p", DROPIN_DIR]), "Creating " + DROPIN_DIR)
    _check(sudo.run(["tee", DROPIN], input=content), "Writing " + DROPIN)


def set_ssh_allowed(name, allowed, sudo):
    _require_user(name)
    if not include_supported(sudo):
        raise SshError(
            "{} does not include sshd_config.d/*.conf, so users can't be blocked from here".format(SSHD_CONFIG)
        )

    before = set(denied_users(sudo))
    after = set(before)
    if allowed:
        after.discard(name)
    else:
        after.add(name)
    if after == before:
        return

    _write_dropin(after, sudo)
    rc, out, err = sudo.run([SSHD, "-t"])
    if rc != 0:
        _write_dropin(before, sudo)
        raise SshError("sshd rejected the new configuration: {}".format(err or out))
    if _unit_state("is-active", SERVICE) == "active":
        _check(sudo.run(["systemctl", "reload", SERVICE]), "Reloading SSH")


def set_service(enabled, sudo):
    if enabled:
        if not os.path.exists(SSHD):
            raise SshError("openssh-server is not installed - run the 'Enable SSH' script, it installs it")
        # missing host keys are a common reason for sshd refusing to start on a Pi
        _check(sudo.run([SSH_KEYGEN, "-A"]), "Creating host keys")
        _check(sudo.run([SSHD, "-t"]), "Checking the sshd configuration")
        _check(sudo.run(["systemctl", "enable", SERVICE]), "Enabling SSH")
        _check(sudo.run(["systemctl", "restart", SERVICE]), "Starting SSH")
    else:
        if run(["systemctl", "cat", SOCKET], timeout=10)[0] == 0:
            _check(sudo.run(["systemctl", "disable", "--now", SOCKET]), "Disabling ssh.socket")
        _check(sudo.run(["systemctl", "disable", "--now", SERVICE]), "Disabling SSH")
