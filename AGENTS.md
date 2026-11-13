# OctoPrint-PiConsole

OctoPrint plugin: web terminal, one-click bash scripts and SSH user management for the OctoPrint host.
The SSH and sudo code can lock the user out of their Pi, so treat `ssh.py` and `sudo.py` as high risk.

## Rules
- Bump `__plugin_version__` in `octoprint_piconsole/__init__.py` **and** `version` in `setup.py` on every PR (CI checks they match; a bump on `main` creates the GitHub release the Plugin Manager updates from).
- Passwords only ever go to `chpasswd` / `sudo -S` on stdin, never on a command line, never logged.
- The sshd drop-in (`/etc/ssh/sshd_config.d/piconsole.conf`) is always checked with `sshd -t` after writing and rolled back if sshd rejects it. Never restart or reload sshd before that check.
- Built-in scripts (`octoprint_piconsole/default_scripts/*.sh`): LF endings, `#!/bin/bash`, `@name` and `@description` headers. Every `sudo` call uses `sudo -n` and the script declares `# @sudo: true` or `optional` (exception list in `tests/test_script_headers.py`).
- Scripts that change the system need `# @confirm: true`; updates also `# @block_while_printing: true`.

## Tests
`tests/` runs without OctoPrint installed (`conftest.py` loads only the helper modules). Root commands are faked; nothing touches the real `/etc/ssh`.

```
python -m venv /tmp/venv && /tmp/venv/bin/pip install pytest
/tmp/venv/bin/pytest -q tests
```

CI (`.github/workflows/pr-checks.yml`) runs pytest on Python 3.9/3.11/3.13 plus `bash -n`, `shellcheck -S error` and `node --check`.

## Manual testing in Docker
Use the full `python:3.11` image (slim lacks gcc for netifaces), `octoprint serve --iknowwhatimdoing`, and set `plugins.tracking.enabled false` to skip the wizard.
