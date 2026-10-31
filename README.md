# OctoPrint-PiConsole

A shell for the machine OctoPrint runs on, right in the OctoPrint web UI, for when SSH is broken or you just don't want to open it.

- **Terminal**: a real interactive login shell (xterm.js + pty). `nano`, `top`, `passwd` and `sudo` work like over SSH. The session survives page reloads and is closed after an idle timeout.
- **Scripts**: saved shell scripts you run with one click (plus a confirmation, if the script asks for one). Output streams live into the tab and the last 20 runs are kept.
- **Built-in scripts**: Check SSH, Enable SSH, Check for updates, Install updates, System info.

## Install

Settings > Plugin Manager > Get More > "... from URL":

```
https://github.com/KrX3D/OctoPrint-PiConsole/archive/main.zip
```

Linux hosts only (OctoPi / Raspberry Pi OS). Python 3.7+.

## Security

- Three permissions: **Terminal**, **Run scripts** and **Edit scripts**. All are marked dangerous and only the Admins group gets them by default. Change this under Settings > Access Control.
- Every API call requires a logged-in user with the right permission and is CSRF protected.
- Terminal sessions belong to one user. Nobody else can read their output or type into them.
- Opening and closing terminals, plus starting, stopping, saving and deleting scripts, are logged to `octoprint.log` with the user name.
- The terminal can be switched off in the plugin settings. The scripts keep working.
- Scripts marked `block_while_printing` are refused while a print is running or paused.

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

Scripts run as the OctoPrint user with no terminal and no stdin. Use `sudo -n` so a missing sudo right fails instead of hanging. A copy of the script is run, so editing it while it runs is safe. Windows line endings are converted on save.

"Restore built-in scripts" resets the built-in scripts to their original version. Your own scripts aren't touched.
