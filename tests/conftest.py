# coding=utf-8
"""Lets the tests import the plugin's helper modules without OctoPrint installed.

``octoprint_piconsole/__init__.py`` imports flask and OctoPrint. ssh.py, sudo.py and
scripts.py don't, so the package is registered as an empty namespace and only the
submodules are loaded from disk.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(ROOT, "octoprint_piconsole")
DEFAULT_SCRIPTS = os.path.join(PACKAGE_DIR, "default_scripts")

if "octoprint_piconsole" not in sys.modules:
    package = types.ModuleType("octoprint_piconsole")
    package.__path__ = [PACKAGE_DIR]
    sys.modules["octoprint_piconsole"] = package
