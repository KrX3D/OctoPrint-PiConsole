/*
 * View model for OctoPrint-PiConsole
 */
$(function () {
    var SCRIPT_ID = /^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$/;
    var MAX_INPUT_CHUNK = 32 * 1024;

    var NEW_SCRIPT_TEMPLATE =
        "#!/bin/bash\n" +
        "# @name: My script\n" +
        "# @description: What this script does\n" +
        "# @confirm: true\n" +
        "# @block_while_printing: false\n" +
        "\n" +
        'echo "Hello from $(hostname)"\n';

    var TERM_STATUS_CSS = {
        disconnected: "",
        connecting: "label-info",
        connected: "label-success",
        exited: "label-warning",
        error: "label-important"
    };

    var JOB_STATUS_CSS = {
        running: "label-info",
        success: "label-success",
        failed: "label-important",
        killed: "label-warning"
    };

    var TERM_THEME = {background: "#1e1e1e"};

    // refit whenever the container size changes (page scrollbar, fullscreen, window resize)
    function observeResize(element, fit) {
        if (window.ResizeObserver) {
            new ResizeObserver(_.debounce(fit, 100)).observe(element);
        }
    }

    function fitTerm(term, fitAddon, element) {
        if (!term || !$(element).is(":visible")) return;
        // xterm measures the font once when opened, which can happen before layout and fonts
        // are settled and leaves lines clipped; changing the font size forces a re-measure
        var size = term.options.fontSize;
        term.options.fontSize = size + 1;
        term.options.fontSize = size;
        try {
            fitAddon.fit();
        } catch (e) {}
    }

    function b64ToBytes(b64) {
        var bin = atob(b64);
        var bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) {
            bytes[i] = bin.charCodeAt(i);
        }
        return bytes;
    }

    function errorText(xhr) {
        if (xhr && xhr.responseJSON && xhr.responseJSON.error) {
            return xhr.responseJSON.error;
        }
        if (xhr && xhr.status === 403) {
            return gettext("You don't have permission to do that.");
        }
        return (xhr && xhr.statusText) || gettext("Unknown error");
    }

    function notifyError(title, xhr) {
        new PNotify({title: title, text: _.escape(errorText(xhr)), type: "error", hide: true});
    }

    function PiConsoleViewModel(parameters) {
        var self = this;

        self.loginState = parameters[0];
        self.access = parameters[1];
        self.printerState = parameters[2];
        self.settingsViewModel = parameters[3];

        self.api = OctoPrint.getBlueprintUrl("piconsole");
        self.textEdit = gettext("Edit");
        self.textView = gettext("View");

        // ~~ permissions & settings

        function permission(key) {
            return ko.pureComputed(function () {
                self.loginState.currentUser(); // re-evaluate on login/logout
                var perm = self.access.permissions["PLUGIN_PICONSOLE_" + key];
                return perm ? self.loginState.hasPermission(perm) : false;
            });
        }

        self.canTerminal = permission("TERMINAL");
        self.canRun = permission("RUN");
        self.canEdit = permission("EDIT");

        self.terminalEnabled = ko.observable(true);
        self.fontSize = 14;
        self.terminalAvailable = ko.pureComputed(function () {
            return self.canTerminal() && self.terminalEnabled();
        });

        self.readSettings = function () {
            var s = self.settingsViewModel.settings;
            if (!s || !s.plugins || !s.plugins.piconsole) return;
            self.terminalEnabled(!!s.plugins.piconsole.terminal_enabled());
            self.fontSize = parseInt(s.plugins.piconsole.font_size(), 10) || 14;
            if (self.term) self.term.options.fontSize = self.fontSize;
            if (self.outputTerm) self.outputTerm.options.fontSize = Math.max(8, self.fontSize - 1);
            self.fitAll();
        };

        // ~~ view switching

        self.tabActive = ko.observable(false);
        self.view = ko.observable("scripts");

        self.showView = function (view) {
            self.view(view);
            self.updateVisibility();
        };

        self.updateVisibility = function () {
            if (!self.tabActive()) {
                self.stopTermPolling();
                self.stopJobPolling();
                return;
            }
            if (self.view() === "terminal" && !self.terminalAvailable()) {
                self.view("scripts");
            }
            if (self.view() === "scripts" && !self.canRun() && !self.canEdit() && self.terminalAvailable()) {
                self.view("terminal");
            }

            if (self.view() === "terminal") {
                self.stopJobPolling();
                // wait for the container to become visible so xterm can measure it
                setTimeout(function () {
                    self.ensureTerminal();
                    self.fitTerminal();
                    if (self.termStatus() === "connected") {
                        self.startTermPolling();
                        self.term.focus();
                    } else if (self.termStatus() !== "connecting" && self.termStatus() !== "exited") {
                        self.connectTerminal(false);
                    }
                }, 0);
            } else {
                self.stopTermPolling();
                self.loadScripts();
                if (self.canRun()) {
                    setTimeout(function () {
                        self.ensureOutputTerm();
                        self.fitOutput();
                        self.loadJobs();
                    }, 0);
                }
            }
        };

        // ~~ scripts

        self.scripts = ko.observableArray([]);

        self.loadScripts = function () {
            if (!self.canRun() && !self.canEdit()) return;
            OctoPrint.get(self.api + "scripts").done(function (response) {
                self.scripts(response.scripts);
            });
        };

        self.isScriptRunning = function (scriptId) {
            return _.some(self.jobs(), function (job) {
                return job.script === scriptId && job.status === "running";
            });
        };

        self.runScript = function (script) {
            var start = function () {
                OctoPrint.postJson(self.api + "scripts/" + script.id + "/run", {})
                    .done(function (job) {
                        self.jobs.unshift(job);
                        self.selectJob(job.id);
                    })
                    .fail(function (xhr) {
                        notifyError(_.sprintf(gettext("Could not run %(name)s"), {name: script.name}), xhr);
                    });
            };

            var printing = self.printerState.isPrinting() || self.printerState.isPaused();
            if (printing && script.block_while_printing) {
                new PNotify({
                    title: script.name,
                    text: gettext("This script can't run while a print is active."),
                    type: "error",
                    hide: true
                });
                return;
            }
            if (!script.confirm && !printing) {
                start();
                return;
            }

            var html = "<p>" + _.escape(script.description || "") + "</p>";
            if (printing) {
                html += '<p class="text-error"><strong>' + gettext("A print is currently running.") + "</strong></p>";
            }
            showConfirmationDialog({
                title: _.sprintf(gettext("Run %(name)s?"), {name: script.name}),
                html: html,
                question: gettext("Do you want to run this script now?"),
                proceed: gettext("Run"),
                proceedClass: "primary",
                onproceed: start
            });
        };

        self.deleteScript = function (script) {
            showConfirmationDialog({
                title: gettext("Delete script?"),
                // OctoPrint hands "html" to jQuery, so it has to start with a tag
                html:
                    "<p>" +
                    _.sprintf(gettext("%(name)s (%(id)s.sh) will be deleted."), {
                        name: _.escape(script.name),
                        id: _.escape(script.id)
                    }) +
                    "</p>",
                proceed: gettext("Delete"),
                onproceed: function () {
                    OctoPrint.delete(self.api + "scripts/" + script.id)
                        .done(self.loadScripts)
                        .fail(function (xhr) {
                            notifyError(gettext("Could not delete the script"), xhr);
                        });
                }
            });
        };

        self.restoreDefaults = function () {
            showConfirmationDialog({
                title: gettext("Restore built-in scripts?"),
                message: gettext(
                    "All built-in scripts are reset to their original version and deleted ones come back. Your own scripts are not touched."
                ),
                proceed: gettext("Restore"),
                onproceed: function () {
                    OctoPrint.postJson(self.api + "defaults/restore", {})
                        .done(function (response) {
                            self.scripts(response.scripts);
                        })
                        .fail(function (xhr) {
                            notifyError(gettext("Could not restore the built-in scripts"), xhr);
                        });
                }
            });
        };

        // ~~ edit dialog

        self.editDialog = undefined;
        self.editId = ko.observable("");
        self.editContent = ko.observable("");
        self.editIsNew = ko.observable(false);
        self.editSaving = ko.observable(false);
        self.editIdValid = ko.pureComputed(function () {
            return SCRIPT_ID.test(self.editId() || "");
        });
        self.editTitle = ko.pureComputed(function () {
            if (self.editIsNew()) return gettext("New script");
            return (self.canEdit() ? gettext("Edit") : gettext("View")) + " " + self.editId() + ".sh";
        });

        self.showEditor = function (id, content, isNew) {
            self.editId(id);
            self.editContent(content);
            self.editIsNew(isNew);
            self.editSaving(false);
            self.editDialog.modal("show");
        };

        self.newScript = function () {
            self.showEditor("", NEW_SCRIPT_TEMPLATE, true);
        };

        self.openScript = function (script) {
            OctoPrint.get(self.api + "scripts/" + script.id)
                .done(function (response) {
                    self.showEditor(response.id, response.content, false);
                })
                .fail(function (xhr) {
                    notifyError(gettext("Could not load the script"), xhr);
                });
        };

        self.saveScript = function () {
            var id = self.editId();
            var save = function () {
                self.editSaving(true);
                OctoPrint.postJson(self.api + "scripts/" + id, {content: self.editContent()})
                    .done(function () {
                        self.editDialog.modal("hide");
                        self.loadScripts();
                    })
                    .fail(function (xhr) {
                        notifyError(gettext("Could not save the script"), xhr);
                    })
                    .always(function () {
                        self.editSaving(false);
                    });
            };

            var exists = _.some(self.scripts(), function (s) {
                return s.id === id;
            });
            if (self.editIsNew() && exists) {
                showConfirmationDialog({
                    title: gettext("Overwrite script?"),
                    html:
                        "<p>" +
                        _.sprintf(gettext("%(id)s.sh already exists and will be replaced."), {id: _.escape(id)}) +
                        "</p>",
                    proceed: gettext("Overwrite"),
                    onproceed: save
                });
            } else {
                save();
            }
        };

        self.pickUpload = function () {
            $("#piconsole_upload").click();
        };

        self.uploadSelected = function (data, event) {
            var input = event.target;
            var file = input.files && input.files[0];
            input.value = "";
            if (!file) return;

            var reader = new FileReader();
            reader.onload = function () {
                var id = file.name
                    .replace(/\.(sh|bash|txt)$/i, "")
                    .replace(/[^A-Za-z0-9_\-]/g, "_")
                    .replace(/^[_\-]+/, "")
                    .substring(0, 64);
                // let the user review the uploaded file before it is saved
                self.showEditor(id, reader.result, true);
            };
            reader.readAsText(file);
        };

        // ~~ jobs / script output

        self.jobs = ko.observableArray([]);
        self.selectedJobId = ko.observable();
        self.selectedJob = ko.pureComputed(function () {
            var id = self.selectedJobId();
            return _.find(self.jobs(), function (job) {
                return job.id === id;
            });
        });
        self.jobStatusCss = ko.pureComputed(function () {
            var job = self.selectedJob();
            return job ? JOB_STATUS_CSS[job.status] || "" : "";
        });

        self.jobLabel = function (job) {
            var time = new Date(job.started * 1000).toLocaleTimeString();
            return job.name + " - " + time + " (" + job.status + ")";
        };

        self.outputTerm = null;
        self.outputFit = null;
        self.jobOffset = null;
        self.jobTimer = null;
        self.viewedJobId = null;

        self.ensureOutputTerm = function () {
            if (self.outputTerm) return;
            self.outputTerm = new Terminal({
                convertEol: true,
                disableStdin: true,
                cursorStyle: "underline",
                cursorInactiveStyle: "none",
                fontSize: Math.max(8, self.fontSize - 1),
                fontFamily: 'Menlo, Consolas, "DejaVu Sans Mono", monospace',
                scrollback: 10000,
                theme: TERM_THEME
            });
            self.outputFit = new FitAddon.FitAddon();
            self.outputTerm.loadAddon(self.outputFit);
            var element = document.getElementById("piconsole_output");
            self.outputTerm.open(element);
            observeResize(element, self.fitOutput);
        };

        self.fitOutput = function () {
            fitTerm(self.outputTerm, self.outputFit, "#piconsole_output");
        };

        self.loadJobs = function () {
            if (!self.canRun()) return;
            OctoPrint.get(self.api + "jobs").done(function (response) {
                self.jobs(response.jobs);
                if (!self.selectedJob() && response.jobs.length) {
                    self.selectJob(response.jobs[0].id);
                } else if (self.selectedJobId()) {
                    self.viewJob(self.selectedJobId());
                }
            });
        };

        self.selectJob = function (id) {
            if (self.selectedJobId() === id) {
                self.viewJob(id);
            } else {
                self.selectedJobId(id); // the subscription below calls viewJob
            }
        };

        self.selectedJobId.subscribe(function (id) {
            if (id) self.viewJob(id);
        });

        self.viewJob = function (id) {
            self.ensureOutputTerm();
            if (id !== self.viewedJobId) {
                self.viewedJobId = id;
                self.jobOffset = null;
                self.outputTerm.reset();
            }
            self.stopJobPolling();
            self.pollJob();
        };

        self.stopJobPolling = function () {
            clearTimeout(self.jobTimer);
            self.jobTimer = null;
        };

        self.pollJob = function () {
            var id = self.viewedJobId;
            if (!id) return;
            var url = self.api + "jobs/" + id + (self.jobOffset === null ? "" : "?offset=" + self.jobOffset);
            OctoPrint.get(url)
                .done(function (job) {
                    if (id !== self.viewedJobId) return;
                    if (job.data) self.outputTerm.write(b64ToBytes(job.data));
                    self.jobOffset = job.offset;

                    // update the list entry so labels and run buttons follow the status
                    var data = _.omit(job, "data", "offset");
                    self.jobs(
                        _.map(self.jobs(), function (j) {
                            return j.id === id ? data : j;
                        })
                    );

                    if (job.status === "running" && self.tabActive() && self.view() === "scripts") {
                        self.jobTimer = setTimeout(self.pollJob, 300);
                    } else if (job.status !== "running") {
                        self.loadScripts();
                    }
                })
                .fail(function (xhr) {
                    if (xhr.status === 404) {
                        self.loadJobs();
                    } else if (self.tabActive()) {
                        self.jobTimer = setTimeout(self.pollJob, 2000);
                    }
                });
        };

        self.killSelectedJob = function () {
            var job = self.selectedJob();
            if (!job) return;
            OctoPrint.postJson(self.api + "jobs/" + job.id + "/kill", {}).fail(function (xhr) {
                notifyError(gettext("Could not stop the script"), xhr);
            });
        };

        // ~~ interactive terminal

        self.term = null;
        self.termFit = null;
        self.termSession = null;
        self.termOffset = null;
        self.termPolling = false;
        self.termTimer = null;
        self.termInFlight = false;
        self.termPollAgain = false;
        self.pendingInput = "";
        self.inputInFlight = false;
        self.resizeTimer = null;

        // gettext is only available once view models are constructed
        var termStatusText = {
            disconnected: gettext("Not connected"),
            connecting: gettext("Connecting..."),
            connected: gettext("Connected"),
            exited: gettext("Session ended"),
            error: gettext("Error")
        };

        self.termStatus = ko.observable("disconnected");
        self.termStatusText = ko.pureComputed(function () {
            return termStatusText[self.termStatus()];
        });
        self.termStatusCss = ko.pureComputed(function () {
            return TERM_STATUS_CSS[self.termStatus()];
        });
        self.fullscreen = ko.observable(false);

        self.ensureTerminal = function () {
            if (self.term) return;
            self.term = new Terminal({
                cursorBlink: true,
                fontSize: self.fontSize,
                fontFamily: 'Menlo, Consolas, "DejaVu Sans Mono", monospace',
                scrollback: 5000,
                theme: TERM_THEME
            });
            self.termFit = new FitAddon.FitAddon();
            self.term.loadAddon(self.termFit);
            var element = document.getElementById("piconsole_terminal");
            self.term.open(element);
            observeResize(element, self.fitTerminal);
            self.term.onData(self.queueInput);
            self.term.onResize(function (size) {
                clearTimeout(self.resizeTimer);
                self.resizeTimer = setTimeout(function () {
                    if (self.termStatus() !== "connected") return;
                    OctoPrint.postJson(self.api + "terminal/resize", {rows: size.rows, cols: size.cols});
                }, 200);
            });
        };

        self.fitTerminal = function () {
            fitTerm(self.term, self.termFit, "#piconsole_terminal");
        };

        self.fitAll = function () {
            self.fitTerminal();
            self.fitOutput();
        };

        self.connectTerminal = function (restart) {
            self.ensureTerminal();
            self.fitTerminal();
            self.stopTermPolling();
            self.termStatus("connecting");
            OctoPrint.postJson(self.api + "terminal/open", {
                rows: self.term.rows,
                cols: self.term.cols,
                restart: !!restart
            })
                .done(function (info) {
                    if (info.id !== self.termSession) {
                        self.term.reset();
                        self.termSession = info.id;
                        self.termOffset = null;
                    }
                    self.termStatus("connected");
                    if (self.tabActive() && self.view() === "terminal") {
                        self.startTermPolling();
                        self.term.focus();
                    }
                })
                .fail(function (xhr) {
                    self.termStatus("error");
                    notifyError(gettext("Could not open the terminal"), xhr);
                });
        };

        self.restartTerminal = function () {
            self.connectTerminal(true);
        };

        self.closeTerminal = function () {
            showConfirmationDialog({
                title: gettext("Close terminal session?"),
                message: gettext("The shell and everything still running in it is stopped."),
                proceed: gettext("Close session"),
                onproceed: function () {
                    OctoPrint.postJson(self.api + "terminal/close", {});
                }
            });
        };

        self.toggleFullscreen = function () {
            self.fullscreen(!self.fullscreen());
            setTimeout(function () {
                self.fitTerminal();
                if (self.term) self.term.focus();
            }, 0);
        };

        self.startTermPolling = function () {
            self.termPolling = true;
            self.scheduleTermPoll(0);
        };

        self.stopTermPolling = function () {
            self.termPolling = false;
            clearTimeout(self.termTimer);
        };

        self.scheduleTermPoll = function (delay) {
            clearTimeout(self.termTimer);
            if (!self.termPolling) return;
            self.termTimer = setTimeout(self.pollTerminal, delay);
        };

        self.pollTerminal = function () {
            if (!self.termPolling) return;
            if (self.termInFlight) {
                self.termPollAgain = true;
                return;
            }
            self.termInFlight = true;

            var url = self.api + "terminal/read?id=" + encodeURIComponent(self.termSession || "");
            if (self.termOffset !== null) url += "&offset=" + self.termOffset;

            OctoPrint.get(url)
                .done(function (response) {
                    if (!response.id) {
                        // closed via "Close session" or the idle timeout
                        self.stopTermPolling();
                        self.termSession = null;
                        self.termStatus("exited");
                        self.term.write("\r\n\x1b[90m[" + gettext("session closed - press Restart") + "]\x1b[0m\r\n");
                        return;
                    }
                    if (response.id !== self.termSession) {
                        self.term.reset();
                        self.termSession = response.id;
                    }
                    if (response.data) self.term.write(b64ToBytes(response.data));
                    self.termOffset = response.offset;

                    if (!response.alive) {
                        self.stopTermPolling();
                        self.termStatus("exited");
                        self.term.write("\r\n\x1b[90m[" + gettext("shell exited - press Restart") + "]\x1b[0m\r\n");
                        return;
                    }
                    self.scheduleTermPoll(response.data ? 50 : 300);
                })
                .fail(function () {
                    self.scheduleTermPoll(2000);
                })
                .always(function () {
                    self.termInFlight = false;
                    if (self.termPollAgain) {
                        self.termPollAgain = false;
                        self.scheduleTermPoll(0);
                    }
                });
        };

        self.queueInput = function (data) {
            if (self.termStatus() !== "connected") return;
            self.pendingInput += data;
            if (!self.inputInFlight) self.flushInput();
        };

        self.flushInput = function () {
            if (!self.pendingInput) return;
            var data = self.pendingInput.substring(0, MAX_INPUT_CHUNK);
            self.pendingInput = self.pendingInput.substring(data.length);
            self.inputInFlight = true;

            OctoPrint.postJson(self.api + "terminal/write", {data: data})
                .done(function () {
                    self.scheduleTermPoll(20);
                })
                .fail(function (xhr) {
                    if (xhr.status === 409) {
                        self.pendingInput = "";
                        self.scheduleTermPoll(0);
                    }
                })
                .always(function () {
                    self.inputInFlight = false;
                    if (self.pendingInput) self.flushInput();
                });
        };

        // ~~ OctoPrint callbacks

        self.onBeforeBinding = function () {
            self.editDialog = $("#piconsole_edit_dialog");
            self.readSettings();
        };

        self.onAfterBinding = function () {
            $(window).on(
                "resize",
                _.debounce(function () {
                    self.fitAll();
                }, 150)
            );
        };

        self.onEventSettingsUpdated = function () {
            self.readSettings();
        };

        self.onAfterTabChange = function (current) {
            self.tabActive(current === "#tab_plugin_piconsole");
            self.updateVisibility();
        };

        self.onUserLoggedIn = function () {
            self.updateVisibility();
        };

        self.onUserLoggedOut = function () {
            self.stopTermPolling();
            self.stopJobPolling();
            self.termStatus("disconnected");
            self.termSession = null;
            if (self.term) self.term.reset();
            if (self.outputTerm) self.outputTerm.reset();
            self.scripts([]);
            self.jobs([]);
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PiConsoleViewModel,
        dependencies: ["loginStateViewModel", "accessViewModel", "printerStateViewModel", "settingsViewModel"],
        elements: ["#tab_plugin_piconsole", "#piconsole_edit_dialog"]
    });
});
