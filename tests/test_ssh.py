# coding=utf-8
"""SSH user management: the sshd drop-in and chpasswd.

A bug here can lock the user out of their Pi, so the root commands are faked and
every call is checked. File commands (cat, tee, rm, mkdir) really run, but on a
temporary copy of /etc/ssh.
"""
import collections
import subprocess

import pytest

from octoprint_piconsole import ssh

INCLUDE = "Include /etc/ssh/sshd_config.d/*.conf\n"
Entry = collections.namedtuple("Entry", "pw_name pw_uid pw_dir pw_shell")
USERS = [
    Entry("pi", 1000, "/home/pi", "/bin/bash"),
    Entry("alice", 1001, "/home/alice", "/bin/bash"),
]


class FakeSudo(object):
    mode = "nopasswd"
    available = True

    def __init__(self, sshd_rc=0, sshd_err="", fail=()):
        self.calls = []
        self.sshd_rc = sshd_rc
        self.sshd_err = sshd_err
        self.fail = set(fail)

    def commands(self):
        return [args for args, _ in self.calls]

    def run(self, args, input=None, timeout=60):
        args = list(args)
        self.calls.append((args, input))
        if args[0] in self.fail:
            return 1, "", args[0] + " failed"
        if args[0] in ("cat", "tee", "rm", "mkdir"):
            proc = subprocess.run(
                args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        if args[0] == ssh.SSHD:
            return self.sshd_rc, "", self.sshd_err
        if args[0] == "passwd":
            return 0, "{} P 01/01/2026 0 99999 7 -1".format(args[2]), ""
        return 0, "", ""


@pytest.fixture
def etc(tmp_path, monkeypatch):
    config = tmp_path / "sshd_config"
    config.write_text("Port 22\n" + INCLUDE)
    monkeypatch.setattr(ssh, "SSHD_CONFIG", str(config))
    monkeypatch.setattr(ssh, "DROPIN_DIR", str(tmp_path / "sshd_config.d"))
    monkeypatch.setattr(ssh, "DROPIN", str(tmp_path / "sshd_config.d" / "piconsole.conf"))
    monkeypatch.setattr(ssh, "login_users", lambda: list(USERS))
    state = dict(active="active", socket=False, systemctl=[])

    def fake_run(args, input=None, timeout=60):
        if args[:2] == ["systemctl", "is-active"]:
            return 0, state["active"] if args[2] == ssh.SERVICE else "inactive", ""
        if args[:2] == ["systemctl", "cat"]:
            return (0 if state["socket"] else 1), "", ""
        return 0, "", ""

    monkeypatch.setattr(ssh, "run", fake_run)
    return tmp_path, state


def dropin(tmp_path):
    path = tmp_path / "sshd_config.d" / "piconsole.conf"
    return path.read_text() if path.exists() else None


# --- reading the config -------------------------------------------------------


@pytest.mark.parametrize(
    "config, supported",
    [
        (INCLUDE, True),
        ("  include   /etc/ssh/sshd_config.d/*.conf  \n", True),
        ("#Include /etc/ssh/sshd_config.d/*.conf\n", False),
        ("Include /etc/ssh/other.d/*.conf\n", False),
        ("", False),
    ],
)
def test_include_supported(etc, config, supported):
    tmp_path, _ = etc
    (tmp_path / "sshd_config").write_text(config)
    assert ssh.include_supported(FakeSudo()) is supported


def test_include_not_supported_without_config(etc, monkeypatch):
    tmp_path, _ = etc
    monkeypatch.setattr(ssh, "SSHD_CONFIG", str(tmp_path / "missing"))
    assert ssh.include_supported(FakeSudo()) is False


def test_denied_users_parsing(etc):
    tmp_path, _ = etc
    (tmp_path / "sshd_config.d").mkdir()
    (tmp_path / "sshd_config.d" / "piconsole.conf").write_text(
        "# DenyUsers commented\nDenyUsers pi alice\ndenyusers bob\nAllowUsers carol\n"
    )
    assert ssh.denied_users(FakeSudo()) == ["pi", "alice", "bob"]


def test_denied_users_without_dropin(etc):
    assert ssh.denied_users(FakeSudo()) == []


# --- blocking and allowing users ---------------------------------------------


def test_block_user_writes_dropin_validates_and_reloads(etc):
    tmp_path, _ = etc
    sudo = FakeSudo()
    ssh.set_ssh_allowed("pi", False, sudo)

    assert dropin(tmp_path) == "# Managed by OctoPrint-PiConsole: users blocked from SSH login\nDenyUsers pi\n"
    commands = sudo.commands()
    tee = commands.index(["tee", ssh.DROPIN])
    check = commands.index([ssh.SSHD, "-t"])
    reload_ = commands.index(["systemctl", "reload", ssh.SERVICE])
    assert tee < check < reload_, "the config must be checked after writing and before reloading"


def test_block_second_user_keeps_first_sorted(etc):
    tmp_path, _ = etc
    ssh.set_ssh_allowed("pi", False, FakeSudo())
    ssh.set_ssh_allowed("alice", False, FakeSudo())
    assert dropin(tmp_path).splitlines()[-1] == "DenyUsers alice pi"


def test_allowing_last_user_removes_dropin(etc):
    tmp_path, _ = etc
    ssh.set_ssh_allowed("pi", False, FakeSudo())
    sudo = FakeSudo()
    ssh.set_ssh_allowed("pi", True, sudo)
    assert dropin(tmp_path) is None
    assert ["rm", "-f", ssh.DROPIN] in sudo.commands()


def test_no_reload_when_sshd_is_not_running(etc):
    _, state = etc
    state["active"] = "inactive"
    sudo = FakeSudo()
    ssh.set_ssh_allowed("pi", False, sudo)
    assert not any(c[:2] == ["systemctl", "reload"] for c in sudo.commands())


def test_unchanged_state_writes_nothing(etc):
    sudo = FakeSudo()
    ssh.set_ssh_allowed("pi", True, sudo)
    assert not any(c[0] in ("tee", "rm", "systemctl", ssh.SSHD) for c in sudo.commands())


@pytest.mark.parametrize("sshd_rc, message", [(255, "invalid"), (139, "SIGSEGV"), (-6, "SIGABRT")])
def test_invalid_config_is_rolled_back(etc, sshd_rc, message):
    """A drop-in sshd rejects must never stay in place: the next sshd restart would fail."""
    tmp_path, _ = etc
    ssh.set_ssh_allowed("pi", False, FakeSudo())
    before = dropin(tmp_path)

    sudo = FakeSudo(sshd_rc=sshd_rc, sshd_err="bad line")
    with pytest.raises(ssh.SshError, match=message):
        ssh.set_ssh_allowed("alice", False, sudo)
    assert dropin(tmp_path) == before
    assert not any(c[:2] == ["systemctl", "reload"] for c in sudo.commands())


def test_invalid_config_rollback_removes_new_dropin(etc):
    tmp_path, _ = etc
    with pytest.raises(ssh.SshError):
        ssh.set_ssh_allowed("pi", False, FakeSudo(sshd_rc=1))
    assert dropin(tmp_path) is None


def test_failed_write_raises(etc):
    with pytest.raises(ssh.SshError, match="Writing"):
        ssh.set_ssh_allowed("pi", False, FakeSudo(fail={"tee"}))


def test_block_refused_without_include(etc):
    tmp_path, _ = etc
    (tmp_path / "sshd_config").write_text("Port 22\n")
    sudo = FakeSudo()
    with pytest.raises(ssh.SshError, match="does not include"):
        ssh.set_ssh_allowed("pi", False, sudo)
    assert dropin(tmp_path) is None
    assert not any(c[0] in ("tee", "mkdir") for c in sudo.commands())


@pytest.mark.parametrize("name", ["root", "bob", "pi alice", "pi\nDenyUsers alice", ""])
def test_block_unknown_user_refused(etc, name):
    tmp_path, _ = etc
    with pytest.raises(ssh.SshError, match="not a login user"):
        ssh.set_ssh_allowed(name, False, FakeSudo())
    assert dropin(tmp_path) is None


# --- passwords ---------------------------------------------------------------


@pytest.mark.parametrize(
    "password, ok",
    [
        ("12345678", True),
        ("x" * 128, True),
        ("pass:with:colons", True),
        ("pässwörd", True),
        ("1234567", False),
        ("x" * 129, False),
        ("password\nroot:owned", False),
        ("password\r", False),
        ("pass\0word", False),
        (None, False),
        (12345678, False),
    ],
)
def test_validate_password(password, ok):
    assert (ssh.validate_password(password) is None) is ok


def test_set_password_uses_stdin_only(etc):
    sudo = FakeSudo()
    ssh.set_password("alice", "s3cret pass", sudo)
    assert sudo.calls == [(["chpasswd"], "alice:s3cret pass\n")]


def test_set_password_rejects_line_injection(etc):
    sudo = FakeSudo()
    with pytest.raises(ssh.SshError):
        ssh.set_password("alice", "s3cretpass\nroot:owned", sudo)
    assert sudo.calls == []


@pytest.mark.parametrize("name", ["root", "bob", "alice:x", ""])
def test_set_password_unknown_user(etc, name):
    sudo = FakeSudo()
    with pytest.raises(ssh.SshError, match="not a login user"):
        ssh.set_password(name, "s3cretpass", sudo)
    assert sudo.calls == []


def test_set_password_failure_is_reported(etc):
    with pytest.raises(ssh.SshError, match="chpasswd failed"):
        ssh.set_password("alice", "s3cretpass", FakeSudo(fail={"chpasswd"}))


# --- service -----------------------------------------------------------------


def test_enable_checks_config_before_restart(etc, monkeypatch):
    monkeypatch.setattr(ssh.os.path, "exists", lambda path: True)
    sudo = FakeSudo()
    ssh.set_service(True, sudo)
    commands = sudo.commands()
    assert commands == [
        [ssh.SSH_KEYGEN, "-A"],
        [ssh.SSHD, "-t"],
        ["systemctl", "enable", ssh.SERVICE],
        ["systemctl", "restart", ssh.SERVICE],
    ]


def test_enable_with_broken_sshd_does_not_restart(etc, monkeypatch):
    """Restarting with a crashing sshd or bad config would kill the running one."""
    monkeypatch.setattr(ssh.os.path, "exists", lambda path: True)
    sudo = FakeSudo(sshd_rc=139)
    with pytest.raises(ssh.SshError, match="Enable SSH"):
        ssh.set_service(True, sudo)
    assert not any(c[0] == "systemctl" for c in sudo.commands())


def test_enable_without_sshd_installed(etc, monkeypatch):
    monkeypatch.setattr(ssh.os.path, "exists", lambda path: False)
    sudo = FakeSudo()
    with pytest.raises(ssh.SshError, match="not installed"):
        ssh.set_service(True, sudo)
    assert sudo.calls == []


@pytest.mark.parametrize("socket", [True, False])
def test_disable_stops_socket_too(etc, socket):
    _, state = etc
    state["socket"] = socket
    sudo = FakeSudo()
    ssh.set_service(False, sudo)
    expected = [["systemctl", "disable", "--now", ssh.SERVICE]]
    if socket:
        expected.insert(0, ["systemctl", "disable", "--now", ssh.SOCKET])
    assert sudo.commands() == expected


# --- helpers -----------------------------------------------------------------


@pytest.mark.parametrize("rc, name", [(-11, "SIGSEGV"), (139, "SIGSEGV"), (134, "SIGABRT"), (1, None), (0, None), (255, None)])
def test_crash_signal(rc, name):
    assert ssh._crash_signal(rc) == name


def test_overview(etc):
    tmp_path, _ = etc
    ssh.set_ssh_allowed("alice", False, FakeSudo())
    data = ssh.overview(FakeSudo())
    users = dict((u["name"], u) for u in data["users"])
    assert users["alice"]["ssh_allowed"] is False
    assert users["pi"]["ssh_allowed"] is True
    assert users["pi"]["password"] == "P"
    assert data["include_supported"] is True
    assert data["service"]["running"] is True
