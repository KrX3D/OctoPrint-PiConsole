# coding=utf-8
"""Built-in scripts: header metadata, bash syntax and sudo usage."""
import io
import os
import re
import shutil
import subprocess

import pytest

from conftest import DEFAULT_SCRIPTS
from octoprint_piconsole import scripts

KNOWN_KEYS = {"name", "description", "confirm", "block_while_printing", "sudo"}
BOOL_VALUES = {"1", "0", "true", "false", "yes", "no", "on", "off"}
SUDO_VALUES = BOOL_VALUES | {"optional"}

# Scripts that try "sudo -n" only as a bonus and print a hint when it doesn't work.
# Without a @sudo header they never get the password, so this must stay a short, deliberate list.
OPPORTUNISTIC_SUDO = {"octoprint_logs"}

# Scripts that change the system: they must ask first, and updates must not run mid-print
MUST_CONFIRM = {"enable_ssh", "run_updates"}
MUST_BLOCK_WHILE_PRINTING = {"run_updates"}

SUDO_CALL = re.compile(r"(?:^|[\s;&|(`!]|\$\()sudo(?=\s|$)")


def _ids():
    return sorted(name[:-3] for name in os.listdir(DEFAULT_SCRIPTS) if name.endswith(".sh"))


def _raw(script_id):
    with io.open(os.path.join(DEFAULT_SCRIPTS, script_id + ".sh"), "rb") as f:
        return f.read()


SIMPLE_SUBSTITUTION = re.compile(r"\$\((?![^)]*\bsudo\b)[^()\"']*\)")
QUOTED_TEXT = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")


def _strip_quoted(match):
    # keep strings that may run sudo in a command substitution
    return match.group(0) if "$(" in match.group(0) else '""'


def _code_lines(script_id):
    """Script lines without comments and quoted text (good enough for the sudo checks)."""
    for line in _raw(script_id).decode("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield QUOTED_TEXT.sub(_strip_quoted, SIMPLE_SUBSTITUTION.sub("X", line))


def test_there_are_builtin_scripts():
    assert len(_ids()) >= 5


@pytest.mark.parametrize("script_id", _ids())
def test_id_is_valid(script_id):
    assert scripts.valid_id(script_id)


@pytest.mark.parametrize("script_id", _ids())
def test_file_format(script_id):
    raw = _raw(script_id)
    raw.decode("utf-8")
    assert b"\r" not in raw, "CRLF line endings break bash"
    assert raw.startswith(b"#!/bin/bash\n")
    assert raw.endswith(b"\n")


@pytest.mark.parametrize("script_id", _ids())
def test_header(script_id):
    meta = scripts.parse_meta(_raw(script_id).decode("utf-8"))
    assert meta.get("name"), "@name missing (or not in the first 40 lines)"
    assert meta.get("description"), "@description missing"
    unknown = set(meta) - KNOWN_KEYS
    assert not unknown, "unknown header keys (typo?): {}".format(sorted(unknown))
    for key in ("confirm", "block_while_printing"):
        if key in meta:
            assert meta[key].lower() in BOOL_VALUES, "@{}: {!r}".format(key, meta[key])
    if "sudo" in meta:
        assert meta["sudo"].lower() in SUDO_VALUES, "@sudo: {!r}".format(meta["sudo"])


def test_names_are_unique():
    names = [scripts.parse_meta(_raw(i).decode("utf-8"))["name"] for i in _ids()]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("script_id", _ids())
def test_bash_syntax(script_id):
    proc = subprocess.run(
        ["bash", "-n", os.path.join(DEFAULT_SCRIPTS, script_id + ".sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("script_id", _ids())
def test_sudo_calls_are_non_interactive(script_id):
    """Scripts have no tty; a bare "sudo" would wait for a password that never comes."""
    for line in _code_lines(script_id):
        for match in SUDO_CALL.finditer(line):
            rest = line[match.end():]
            assert rest.startswith(" -n"), "sudo without -n: {}".format(line.strip())


@pytest.mark.parametrize("script_id", _ids())
def test_scripts_using_sudo_declare_it(script_id):
    uses_sudo = any(SUDO_CALL.search(line) for line in _code_lines(script_id))
    meta = scripts.parse_meta(_raw(script_id).decode("utf-8"))
    need = scripts._sudo_need(meta.get("sudo"))
    if uses_sudo and script_id not in OPPORTUNISTIC_SUDO:
        assert need in ("required", "optional"), "uses sudo but has no @sudo header"
    if script_id in OPPORTUNISTIC_SUDO:
        assert uses_sudo, "no longer uses sudo, remove it from OPPORTUNISTIC_SUDO"


def test_store_info_for_builtins(tmp_path):
    store = scripts.ScriptStore(str(tmp_path / "scripts"), DEFAULT_SCRIPTS)
    added, updated = store.install_defaults()
    assert sorted(added) == _ids() and updated == []
    infos = dict((s["id"], s) for s in store.list())
    for script_id in _ids():
        assert infos[script_id]["builtin"]
        assert infos[script_id]["name"] != script_id
    for script_id in MUST_CONFIRM:
        assert infos[script_id]["confirm"], script_id
    for script_id in MUST_BLOCK_WHILE_PRINTING:
        assert infos[script_id]["block_while_printing"], script_id
    assert infos["enable_ssh"]["sudo"] == "required"


def test_previous_defaults_refer_to_existing_scripts():
    assert set(scripts.PREVIOUS_DEFAULTS) <= set(_ids())


def test_install_defaults_keeps_edits_and_deletions(tmp_path):
    defaults = tmp_path / "defaults"
    shutil.copytree(DEFAULT_SCRIPTS, str(defaults))
    store = scripts.ScriptStore(str(tmp_path / "data" / "scripts"), str(defaults))
    store.install_defaults()

    store.save("system_info", store.read("system_info") + "echo edited\n")
    store.delete("check_updates")
    unedited_before = store.read("check_ssh")
    for script_id in ("system_info", "check_ssh", "check_updates"):
        with io.open(str(defaults / (script_id + ".sh")), "a", encoding="utf-8") as f:
            f.write("echo new version\n")

    added, updated = store.install_defaults()
    assert added == [] and updated == ["check_ssh"]
    assert store.read("check_ssh") != unedited_before
    assert store.read("system_info").endswith("echo edited\n")
    assert not store.exists("check_updates")

    store.restore_defaults()
    assert store.exists("check_updates")
    assert "echo edited" not in store.read("system_info")


def test_normalize_converts_crlf():
    assert scripts.normalize("a\r\nb\rc") == "a\nb\nc\n"
