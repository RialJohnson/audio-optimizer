#!/usr/bin/env python3
"""Build a one-file Audio Optimizer executable for this operating system."""

import subprocess
import sys

def main():
    separator = ";" if sys.platform == "win32" else ":"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "AudioOptimizer",
        "--add-data",
        "icon.png%s." % separator,
        "--collect-all",
        "tkinterdnd2",
    ]
    if sys.platform == "win32":
        command.extend(
            [
                "--icon",
                "icon.ico",
                "--add-data",
                "icon.ico%s." % separator,
            ]
        )
    command.append("audio_optimizer.py")
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()
