# coding=utf-8
"""How commands get root rights, and the bash shims that scripts run with."""
import os
import subprocess

import pytest

from octoprint_piconsole import sudo as sudo_mod


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_run(args, input=None, timeout=60):
        recorded.append((list(args), input))
        return 0, "", ""

    monkeypatch.setattr(sudo_mod, "run", fake_run)
    return recorded


def make_sudo(monkeypatch, mode, password=None):
    monkeypatch.setattr(sudo_mod, "detect_mode", lambda: mode)
    return sudo_mod.Sudo(password)


def test_root_runs_directly(monkeypatch, calls):
    s = make_sudo(monkeypatch, sudo_mod.ROOT, "ignored")
    assert s.available and s.password is None
    s.run(["chpasswd"], input="pi:pw\n")
    assert calls == [(["chpasswd"], "pi:pw\n")]


def test_nopasswd_uses_non_interactive_sudo(monkeypatch, calls):
    s = make_sudo(monkeypatch, sudo_mod.NOPASSWD, "ignored")
    assert s.available and s.password is None
    s.run(["systemctl", "reload", "ssh.service"])
    assert calls == [(["sudo", "-n", "systemctl", "reload", "ssh.service"], None)]


def test_password_goes_to_stdin_never_argv(monkeypatch, calls):
    s = make_sudo(monkeypatch, sudo_mod.PASSWORD, "hunter2pw")
    assert s.available
    s.run(["chpasswd"], input="pi:newpass\n")
    args, data = calls[0]
    assert args == ["sudo", "-S", "-k", "-p", "", "chpasswd"]
    assert data == "hunter2pw\npi:newpass\n"
    assert not any("hunter2pw" in a for a in args)


def test_password_without_input(monkeypatch, calls):
    s = make_sudo(monkeypatch, sudo_mod.PASSWORD, "hunter2pw")
    s.run(["true"])
    assert calls[0][1] == "hunter2pw\n"


@pytest.mark.parametrize("password", [None, ""])
def test_password_mode_without_password_is_unavailable(monkeypatch, calls, password):
    s = make_sudo(monkeypatch, sudo_mod.PASSWORD, password)
    assert not s.available
    assert "needs the password" in s.verify()
    assert calls == []


def test_no_sudo_binary(monkeypatch, calls):
    s = make_sudo(monkeypatch, sudo_mod.NONE)
    assert not s.available
    assert s.verify() == "sudo is not installed"


@pytest.mark.parametrize(
    "err, message",
    [
        ("pi is not in the sudoers file.", "not allowed"),
        ("Sorry, user pi may not run sudo on host.", "not allowed"),
        ("Sorry, try again.", "Wrong sudo password"),
    ],
)
def test_verify_messages(monkeypatch, err, message):
    monkeypatch.setattr(sudo_mod, "run", lambda args, input=None, timeout=60: (1, "", err))
    s = make_sudo(monkeypatch, sudo_mod.PASSWORD, "hunter2pw")
    assert message in s.verify()


def test_real_run_never_inherits_stdin():
    rc, out, _ = sudo_mod.run(["cat"])
    assert rc == 0 and out == ""


def test_real_run_reports_missing_binary():
    rc, _, err = sudo_mod.run(["/nonexistent/binary"])
    assert rc == 1 and err


# --- bash shims (run in real bash) -------------------------------------------


def _fake_sudo_dir(tmp_path):
    """A 'sudo' that prints each argument on its own line."""
    folder = tmp_path / "bin"
    folder.mkdir()
    fake = folder / "sudo"
    fake.write_text("#!/bin/bash\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done\n")
    fake.chmod(0o755)
    return str(folder)


def _bash(tmp_path, preamble, body, path):
    script = tmp_path / "script.sh"
    script.write_text(preamble + body)
    proc = subprocess.run(
        ["/bin/bash", str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env={"PATH": path},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


@pytest.mark.parametrize(
    "call, expected",
    [
        ("sudo -n true", ["-A", "true"]),
        ("sudo -n apt-get install -y openssh-server", ["-A", "apt-get", "install", "-y", "openssh-server"]),
        ("sudo -n -u pi id", ["-A", "-u", "pi", "id"]),
        ("sudo -n -- ls -n", ["-A", "ls", "-n"]),
        ("sudo -n env X=1 sh -c 'echo a b'", ["-A", "env", "X=1", "sh", "-c", "echo a b"]),
        ("sudo true", ["-A", "true"]),
    ],
)
def test_sudo_preamble_turns_n_into_askpass(tmp_path, call, expected):
    out = _bash(tmp_path, sudo_mod.SUDO_PREAMBLE, call + "\n", _fake_sudo_dir(tmp_path))
    assert out == expected


def test_sudo_preamble_is_exported_to_child_shells(tmp_path):
    out = _bash(tmp_path, sudo_mod.SUDO_PREAMBLE, "/bin/bash -c 'sudo -n true'\n", _fake_sudo_dir(tmp_path))
    assert out == ["-A", "true"]


def test_preamble_is_one_line():
    """Keeps line numbers in script errors off by exactly one."""
    assert sudo_mod.SUDO_PREAMBLE.count("\n") == 1 and sudo_mod.SUDO_PREAMBLE.endswith("\n")
    assert sudo_mod.ROOT_PREAMBLE.count("\n") == 1 and sudo_mod.ROOT_PREAMBLE.endswith("\n")


def test_root_preamble_without_sudo_runs_command(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = _bash(tmp_path, sudo_mod.ROOT_PREAMBLE, "sudo -n -u root printf '%s\\n' a 'b c'\n", str(empty))
    assert out == ["a", "b c"]


def test_root_preamble_keeps_real_sudo(tmp_path):
    out = _bash(tmp_path, sudo_mod.ROOT_PREAMBLE, "sudo -n true\n", _fake_sudo_dir(tmp_path))
    assert out == ["-n", "true"]


def test_askpass_prints_password_file(tmp_path):
    path = sudo_mod.write_askpass(str(tmp_path))
    assert os.stat(path).st_mode & 0o777 == 0o700
    pwfile = tmp_path / "pw"
    pwfile.write_text("p@ss w0rd\n")
    proc = subprocess.run(
        [path], stdout=subprocess.PIPE, universal_newlines=True, env={"PICONSOLE_SUDO_PWFILE": str(pwfile)}
    )
    assert proc.stdout == "p@ss w0rd\n"
