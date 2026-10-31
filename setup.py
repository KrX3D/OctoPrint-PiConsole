# coding=utf-8
from setuptools import setup

setup(
    name="OctoPrint-PiConsole",
    version="0.2.0",
    description="Web terminal and one-click shell scripts for the OctoPrint host (SSH check, system updates, ...)",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="KrX3D",
    url="https://github.com/KrX3D/OctoPrint-PiConsole",
    license="MIT",
    packages=["octoprint_piconsole"],
    include_package_data=True,
    package_data={
        "octoprint_piconsole": [
            "templates/*.jinja2",
            "static/js/*.js",
            "static/css/*.css",
            "static/vendor/*",
            "default_scripts/*.sh",
        ]
    },
    zip_safe=False,
    python_requires=">=3.7,<4",
    install_requires=[],
    entry_points={"octoprint.plugin": ["piconsole = octoprint_piconsole"]},
)
