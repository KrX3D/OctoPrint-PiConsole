# OctoPrint-PiConsole

A shell for the machine OctoPrint runs on, right in the OctoPrint web UI, for when SSH is broken or you just don't want to open it.

- **Terminal**: a real interactive login shell (xterm.js + pty). `nano`, `top`, `passwd` and `sudo` work like over SSH. The session survives page reloads and is closed after an idle timeout.
- **Scripts**: saved shell scripts you run with one click (plus a confirmation, if the script asks for one). Output streams live into the tab, the last 20 runs are kept and can be copied or downloaded as a text file.
- **SSH tab**: start/stop the SSH server, list every user that can log in, set a user's password and block or allow single users for SSH.
- **sudo with password**: if the OctoPrint user has no passwordless sudo, the UI asks for its password when something needs root. It stays in the browser tab's memory until the page is reloaded and is never stored or logged on the server.
- **Built-in scripts**: Check SSH, Enable SSH, Check for updates, Install updates, System info, OctoPrint logs, Follow OctoPrint log (live), Check system files (damaged files, SD card errors, overclock, heat, power), Diagnose crashes (finds the library that makes programs crash).

## Install

Settings > Plugin Manager > Get More > "... from URL":

```
https://github.com/KrX3D/OctoPrint-PiConsole/archive/main.zip
```

Linux hosts only (OctoPi / Raspberry Pi OS). Python 3.7+.

## Security

- Four permissions: **Terminal**, **Run scripts**, **Edit scripts** and **SSH users**. All are marked dangerous and only the Admins group gets them by default. Change this under Settings > Access Control.
- Every API call requires a logged-in user with the right permission and is CSRF protected.
- Terminal sessions belong to one user. Nobody else can read their output or type into them.
- Opening and closing terminals, plus starting, stopping, saving and deleting scripts, are logged to `octoprint.log` with the user name.
- The terminal can be switched off in the plugin settings. The scripts keep working.
- Scripts marked `block_while_printing` are refused while a print is running or paused.
- New passwords are passed to `chpasswd` on stdin (never on a command line). SSH blocks are written to `/etc/ssh/sshd_config.d/piconsole.conf` and checked with `sshd -t` before sshd is reloaded.

Anyone with the terminal or edit permission can run anything the OctoPrint user can, including `sudo` on a default OctoPi. Only give these permissions to people you would give SSH access to.

## Writing scripts

Scripts are plain bash files stored in the plugin data folder (`~/.octoprint/data/piconsole/scripts`). Create them in the UI, or upload a `.sh` file and check it before saving. Header lines control how a script appears:

```bash
#!/bin/bash
# @name: Restart webcam
# @description: Restarts the camera streamer service.
# @confirm: true
# @block_while_printing: false

sudo -n systemctl restart camera-streamer
```

| Header | Default | Meaning |
| --- | --- | --- |
| `@name` | file name | Name shown in the list |
| `@description` | empty | Shown in the list and in the confirmation dialog |
| `@confirm` | `true` | Ask before running |
| `@block_while_printing` | `false` | Refuse to run during a print |
| `@sudo` | `false` | `true`: ask for the sudo password first if one is needed. `optional`: offer it, but allow running without |

Scripts run as the OctoPrint user with no terminal and no stdin. Use `sudo -n` so a missing sudo right fails instead of hanging. When a sudo password was entered, `sudo -n` in scripts is answered with it automatically. A copy of the script is run, so editing it while it runs is safe. Windows line endings are converted on save.

Plugin updates also update built-in scripts you never edited. Edited ones are left alone. "Restore built-in scripts" resets all built-in scripts to their original version. Your own scripts aren't touched.
