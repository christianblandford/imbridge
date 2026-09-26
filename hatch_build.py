"""Wheel build hook: put the helper dylib in the wheel (building it if needed) and tag the wheel for Apple Silicon."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class HelperBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        dylib = Path(self.root) / "src" / "imbridge" / "helper" / "imbridge-helper.dylib"
        if version == "editable":
            # Editable installs use the dylib in place; build it on a Mac if it's missing, skip it elsewhere (tests).
            if not dylib.exists() and sys.platform == "darwin":
                subprocess.run([str(Path(self.root) / "helper" / "build.sh"), str(dylib)], check=True)
            return
        if not dylib.exists():
            if sys.platform != "darwin":
                raise RuntimeError("the imbridge helper can only be built on macOS (helper/build.sh)")
            subprocess.run([str(Path(self.root) / "helper" / "build.sh"), str(dylib)], check=True)
        # The dylib is native code, so the wheel is not pure Python: macOS 11+ on Apple Silicon only.
        build_data["pure_python"] = False
        build_data["tag"] = "py3-none-macosx_11_0_arm64"
