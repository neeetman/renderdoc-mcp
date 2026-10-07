"""Locate and import the renderdoc Python module from a RenderDoc build directory.

The build directory holds renderdoc.dll and pymodules/renderdoc.pyd. Looked up in order:
  1. RENDERDOC_DIR (env)
  2. ../renderdoc/x64/Release   (this repo cloned next to a RenderDoc checkout)
  3. ../x64/Release             (this repo cloned inside a RenderDoc checkout)
The .pyd must be built for the running Python's major.minor (see build_renderdoc.ps1).
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = [os.path.join(os.path.dirname(_REPO), "renderdoc", "x64", "Release"),
              os.path.join(os.path.dirname(_REPO), "x64", "Release")]

_rd = None


def build_dir():
    env = os.environ.get("RENDERDOC_DIR")
    if env:
        return env
    for d in CANDIDATES:
        if os.path.exists(os.path.join(d, "pymodules", "renderdoc.pyd")):
            return d
    return CANDIDATES[0]


def load():
    global _rd
    if _rd is None:
        d = build_dir()
        if not os.path.exists(os.path.join(d, "pymodules", "renderdoc.pyd")):
            raise RuntimeError(f"renderdoc.pyd not found under {d}\\pymodules; set RENDERDOC_DIR or "
                               f"run build_renderdoc.ps1")
        os.add_dll_directory(d)
        sys.path.insert(0, os.path.join(d, "pymodules"))
        import renderdoc
        _rd = renderdoc
    return _rd
