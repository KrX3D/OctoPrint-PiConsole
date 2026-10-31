# coding=utf-8
from __future__ import absolute_import

import base64
import os

import flask
from flask_login import current_user

import octoprint.plugin
from octoprint.access import ADMIN_GROUP
from octoprint.access.permissions import Permissions
from octoprint.util import RepeatedTimer

from . import ssh
from .scripts import MAX_SCRIPT_SIZE, ScriptRunner, ScriptStore, valid_id
from .sudo import Sudo, detect_mode
from .terminal import TerminalManager

MAX_INPUT_SIZE = 64 * 1024


def _int(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _error(message, status):
    return flask.make_response(flask.jsonify(error=message), status)


class PiConsolePlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.BlueprintPlugin,
):
    def __init__(self):
        self._terminals = None
        self._store = None
        self._runner = None
        self._reaper = None

    def initialize(self):
        data_folder = self.get_plugin_data_folder()
        self._terminals = TerminalManager()
        self._store = ScriptStore(
            os.path.join(data_folder, "scripts"),
            os.path.join(self._basefolder, "default_scripts"),
        )
        self._runner = ScriptRunner(
            os.path.join(data_folder, "tmp"),
            self._logger,
            env={"PICONSOLE_LOG_DIR": self._settings.global_get_basefolder("logs")},
        )

    # ~~ StartupPlugin / ShutdownPlugin

    def on_after_startup(self):
        added, updated = self._store.install_defaults()
        if added:
            self._logger.info("Installed built-in scripts: %s", ", ".join(added))
        if updated:
            self._logger.info("Updated unmodified built-in scripts: %s", ", ".join(updated))
        self._reaper = RepeatedTimer(30, self._reap_idle_terminals, daemon=True)
        self._reaper.start()

    def on_shutdown(self):
        if self._reaper is not None:
            self._reaper.cancel()
        self._terminals.close_all()
        self._runner.kill_all()

    def _reap_idle_terminals(self):
        minutes = self._settings.get_int(["terminal_idle_timeout"]) or 0
        if minutes <= 0:
            return
        for session in self._terminals.reap_idle(minutes * 60):
            self._logger.info("Closed idle terminal of user %s (pid %d)", session.owner, session.pid)

    # ~~ SettingsPlugin

    def get_settings_defaults(self):
        return dict(
            terminal_enabled=True,
            shell="/bin/bash",
            terminal_idle_timeout=30,
            font_size=14,
        )

    def get_settings_restricted_paths(self):
        return dict(admin=[["terminal_enabled"], ["shell"], ["terminal_idle_timeout"]])

    # ~~ TemplatePlugin / AssetPlugin

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [
            dict(type="tab", name="Pi Console", custom_bindings=True),
            dict(type="settings", name="Pi Console", custom_bindings=False),
            dict(type="generic", template="piconsole_dialogs.jinja2", custom_bindings=True),
        ]

    def get_assets(self):
        return dict(
            js=["vendor/xterm.js", "vendor/addon-fit.js", "js/piconsole.js"],
            css=["vendor/xterm.css", "css/piconsole.css"],
        )

    # ~~ Permissions

    def get_additional_permissions(self, *args, **kwargs):
        return [
            dict(
                key="TERMINAL",
                name="Terminal",
                description="Open an interactive shell on the OctoPrint host",
                roles=["terminal"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
            dict(
                key="RUN",
                name="Run scripts",
                description="Run the saved Pi Console scripts on the OctoPrint host",
                roles=["run"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
            dict(
                key="EDIT",
                name="Edit scripts",
                description="Create, change and delete Pi Console scripts",
                roles=["edit"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
            dict(
                key="SSH",
                name="SSH users",
                description="Change Linux user passwords, block or allow users for SSH and switch the SSH server on or off",
                roles=["ssh"],
                dangerous=True,
                default_groups=[ADMIN_GROUP],
            ),
        ]

    def _can(self, key):
        permission = getattr(Permissions, "PLUGIN_PICONSOLE_" + key, None)
        return permission is not None and permission.can()

    def _require(self, *keys):
        if not any(self._can(key) for key in keys):
            flask.abort(403)

    @staticmethod
    def _user():
        return current_user.get_name()

    @staticmethod
    def _json():
        return flask.request.get_json(silent=True) or {}

    def _sudo(self, required):
        """Sudo for this request, using the password the browser sent (never stored).

        Returns ``(sudo, error_response)``.
        """
        password = self._json().get("sudo_password")
        sudo = Sudo(password if isinstance(password, str) else None)
        if not required and sudo.password is None:
            return sudo, None
        error = sudo.verify()
        if error:
            reason = "sudo_password" if sudo.mode == "password" else "sudo"
            return sudo, flask.make_response(flask.jsonify(error=error, reason=reason), 400)
        return sudo, None

    # ~~ BlueprintPlugin

    def is_blueprint_protected(self):
        return True

    def is_blueprint_csrf_protected(self):
        return True

    # terminal

    @octoprint.plugin.BlueprintPlugin.route("/terminal/open", methods=["POST"])
    def terminal_open(self):
        self._require("TERMINAL")
        if not self._settings.get_boolean(["terminal_enabled"]):
            return _error("The terminal is disabled in the Pi Console settings", 403)

        shell = self._settings.get(["shell"]) or "/bin/bash"
        if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
            return _error("Configured shell {} is not an executable".format(shell), 400)

        data = self._json()
        session, created = self._terminals.open(
            self._user(),
            shell,
            _int(data.get("rows"), 24, 5, 500),
            _int(data.get("cols"), 80, 10, 1000),
            restart=bool(data.get("restart")),
        )
        if created:
            self._logger.info("Terminal opened by user %s (pid %d)", session.owner, session.pid)
        return flask.jsonify(session.info())

    @octoprint.plugin.BlueprintPlugin.route("/terminal/read", methods=["GET"])
    def terminal_read(self):
        self._require("TERMINAL")
        session = self._terminals.get(self._user())
        if session is None:
            return flask.jsonify(id=None, alive=False)

        offset = flask.request.args.get("offset", type=int)
        if flask.request.args.get("id") != session.id:
            offset = None
        data, offset = session.read(offset)
        result = session.info()
        result.update(data=base64.b64encode(data).decode("ascii"), offset=offset)
        return flask.jsonify(result)

    @octoprint.plugin.BlueprintPlugin.route("/terminal/write", methods=["POST"])
    def terminal_write(self):
        self._require("TERMINAL")
        session = self._terminals.get(self._user())
        if session is None or not session.alive:
            return _error("No running terminal session", 409)

        data = self._json().get("data", "")
        if not isinstance(data, str) or len(data) > MAX_INPUT_SIZE:
            return _error("Invalid input", 400)
        try:
            session.write(data.encode("utf-8"))
        except OSError as e:
            return _error("Writing to the terminal failed: {}".format(e), 500)
        return flask.jsonify(ok=True)

    @octoprint.plugin.BlueprintPlugin.route("/terminal/resize", methods=["POST"])
    def terminal_resize(self):
        self._require("TERMINAL")
        session = self._terminals.get(self._user())
        if session is not None:
            data = self._json()
            session.resize(_int(data.get("rows"), 24, 5, 500), _int(data.get("cols"), 80, 10, 1000))
        return flask.jsonify(ok=True)

    @octoprint.plugin.BlueprintPlugin.route("/terminal/close", methods=["POST"])
    def terminal_close(self):
        self._require("TERMINAL")
        session = self._terminals.close(self._user())
        if session is not None:
            self._logger.info("Terminal closed by user %s (pid %d)", session.owner, session.pid)
        return flask.jsonify(ok=True)

    # scripts

    @octoprint.plugin.BlueprintPlugin.route("/scripts", methods=["GET"])
    def scripts_list(self):
        self._require("RUN", "EDIT")
        return flask.jsonify(scripts=self._store.list(), sudo_mode=detect_mode())

    @octoprint.plugin.BlueprintPlugin.route("/scripts/<script_id>", methods=["GET"])
    def script_get(self, script_id):
        self._require("RUN", "EDIT")
        if not self._store.exists(script_id):
            return _error("Unknown script", 404)
        result = self._store.info(script_id)
        result["content"] = self._store.read(script_id)
        return flask.jsonify(result)

    @octoprint.plugin.BlueprintPlugin.route("/scripts/<script_id>", methods=["POST"])
    def script_save(self, script_id):
        self._require("EDIT")
        if not valid_id(script_id):
            return _error("Script names may only contain letters, digits, - and _ (max. 64)", 400)
        content = self._json().get("content")
        if not isinstance(content, str) or not content.strip():
            return _error("The script is empty", 400)
        if len(content.encode("utf-8")) > MAX_SCRIPT_SIZE:
            return _error("The script is too large", 400)

        self._store.save(script_id, content)
        self._logger.info("Script %s saved by user %s", script_id, self._user())
        return flask.jsonify(self._store.info(script_id))

    @octoprint.plugin.BlueprintPlugin.route("/scripts/<script_id>", methods=["DELETE"])
    def script_delete(self, script_id):
        self._require("EDIT")
        if not self._store.exists(script_id):
            return _error("Unknown script", 404)
        self._store.delete(script_id)
        self._logger.info("Script %s deleted by user %s", script_id, self._user())
        return flask.jsonify(ok=True)

    @octoprint.plugin.BlueprintPlugin.route("/scripts/<script_id>/run", methods=["POST"])
    def script_run(self, script_id):
        self._require("RUN")
        script = self._store.info(script_id)
        if script is None:
            return _error("Unknown script", 404)
        if script["block_while_printing"] and (self._printer.is_printing() or self._printer.is_paused()):
            return _error("{} can't run while a print is active".format(script["name"]), 409)

        sudo, error = self._sudo(required=script["sudo"] == "required")
        if error is not None:
            return error

        job = self._runner.start(script_id, script["name"], self._store.read(script_id), self._user(), sudo)
        if job is None:
            return _error("{} is already running".format(script["name"]), 409)
        self._logger.info("Script %s started by user %s (job %s)", script_id, job.owner, job.id)
        return flask.jsonify(job.info())

    @octoprint.plugin.BlueprintPlugin.route("/defaults/restore", methods=["POST"])
    def defaults_restore(self):
        self._require("EDIT")
        self._store.restore_defaults()
        self._logger.info("Built-in scripts restored by user %s", self._user())
        return flask.jsonify(scripts=self._store.list())

    # ssh (all POST, the sudo password must not end up in URLs or access logs)

    @octoprint.plugin.BlueprintPlugin.route("/ssh/overview", methods=["POST"])
    def ssh_overview(self):
        self._require("SSH")
        sudo, error = self._sudo(required=False)
        if error is not None:
            return error
        return flask.jsonify(ssh.overview(sudo))

    @octoprint.plugin.BlueprintPlugin.route("/ssh/service", methods=["POST"])
    def ssh_service(self):
        self._require("SSH")
        sudo, error = self._sudo(required=True)
        if error is not None:
            return error
        enabled = bool(self._json().get("enabled"))
        self._logger.info("SSH server %s requested by user %s", "start" if enabled else "stop", self._user())
        try:
            ssh.set_service(enabled, sudo)
        except ssh.SshError as e:
            self._logger.warning("Switching the SSH server failed: %s", e)
            return _error(str(e), 500)
        return flask.jsonify(ssh.overview(sudo))

    @octoprint.plugin.BlueprintPlugin.route("/ssh/users/<name>/password", methods=["POST"])
    def ssh_password(self, name):
        self._require("SSH")
        password = self._json().get("password")
        problem = ssh.validate_password(password)
        if problem:
            return _error(problem, 400)
        sudo, error = self._sudo(required=True)
        if error is not None:
            return error
        try:
            ssh.set_password(name, password, sudo)
        except ssh.SshError as e:
            return _error(str(e), 400)
        self._logger.info("Password of system user %s changed by user %s", name, self._user())
        return flask.jsonify(ssh.overview(sudo))

    @octoprint.plugin.BlueprintPlugin.route("/ssh/users/<name>/access", methods=["POST"])
    def ssh_access(self, name):
        self._require("SSH")
        sudo, error = self._sudo(required=True)
        if error is not None:
            return error
        allowed = bool(self._json().get("allowed"))
        try:
            ssh.set_ssh_allowed(name, allowed, sudo)
        except ssh.SshError as e:
            return _error(str(e), 400)
        self._logger.info("SSH %s for system user %s by user %s", "allowed" if allowed else "blocked", name, self._user())
        return flask.jsonify(ssh.overview(sudo))

    # jobs

    @octoprint.plugin.BlueprintPlugin.route("/jobs", methods=["GET"])
    def jobs_list(self):
        self._require("RUN")
        return flask.jsonify(jobs=self._runner.list())

    @octoprint.plugin.BlueprintPlugin.route("/jobs/<job_id>", methods=["GET"])
    def job_get(self, job_id):
        self._require("RUN")
        job = self._runner.get(job_id)
        if job is None:
            return _error("Unknown job", 404)
        data, offset = job.read(flask.request.args.get("offset", type=int))
        result = job.info()
        result.update(data=base64.b64encode(data).decode("ascii"), offset=offset)
        return flask.jsonify(result)

    @octoprint.plugin.BlueprintPlugin.route("/jobs/<job_id>/kill", methods=["POST"])
    def job_kill(self, job_id):
        self._require("RUN")
        job = self._runner.get(job_id)
        if job is None:
            return _error("Unknown job", 404)
        job.kill()
        self._logger.info("Job %s (%s) stopped by user %s", job.id, job.script, self._user())
        return flask.jsonify(job.info())


__plugin_name__ = "Pi Console"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_check__():
    return os.name == "posix"


def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__

    __plugin_implementation__ = PiConsolePlugin()
    __plugin_hooks__ = {
        "octoprint.access.permissions": __plugin_implementation__.get_additional_permissions,
    }
