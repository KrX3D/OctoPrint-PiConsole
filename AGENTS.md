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
Checked with OctoPrint 1.11.8. Use the full `python:3.11` image (slim lacks gcc for netifaces). In HomeClaude, Docker runs on a separate daemon (`DOCKER_HOST=tcp://192.168.90.74:2376`): published ports are on that IP, and bind mounts are read-only, so install from a copy.

```
docker network create pic-test
docker run -d --name pic-octo --network pic-test -p 5000:5000 -v "$PWD":/src:ro python:3.11 bash -c \
  'cp -r /src /tmp/plugin && pip install -q OctoPrint /tmp/plugin \
   && octoprint config set --bool plugins.tracking.enabled false \
   && octoprint serve --iknowwhatimdoing --host 0.0.0.0 --port 5000'
# once "Listening on" is in the log (pip takes a minute or two):
docker exec pic-octo octoprint user add --password test1234 --admin admin
docker restart pic-octo   # the running server does not see users added by the CLI
```

- Turning tracking off alone does **not** skip the first-run wizard (backup, classicwebcam, corewizard stay required). After logging in, mark them handled once: `POST /api/setup/wizard` with `{"handled":["backup","classicwebcam","corewizard","file_check","softwareupdate","tracking"]}`.
- API calls with a session need CSRF: `GET /` first, then send the `csrf_token` cookie value as the `X-CSRF-Token` header on `POST /api/login` and every other POST. Plugin routes are under `/plugin/piconsole/...` (e.g. `GET /plugin/piconsole/scripts`, `POST /plugin/piconsole/scripts/system_info/run`).
- UI tests: run `mcr.microsoft.com/playwright/python:v1.55.0-noble` on the same network and open `http://pic-octo:5000/`. Login fields are `#login-user`, `#login-password`, `#login-button`; the plugin tab link is `a[href='#tab_plugin_piconsole']`.
- The container runs as root with no sshd, systemd or sudo, so sudo goes through the root passthrough shim and the SSH tab has nothing real to manage. SSH/sudo changes still need testing on a Pi.
- Clean up afterwards: `docker rm -f pic-octo && docker network rm pic-test`.
