"""renderdoc-mcp: capture and inspect GPU frames with RenderDoc, for agents.

Captures stay loaded between calls (opening a multi-GB capture takes tens of seconds; queries
after that are fast). Every RenderDoc call runs on one dedicated worker thread.
"""
import asyncio
import functools
import io
import msvcrt
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from mcp.server.fastmcp import FastMCP

from . import launcher, replay

INSTRUCTIONS = """\
RenderDoc frame capture + headless analysis (D3D11/D3D12/Vulkan/OpenGL, Windows).

Workflow:
1. launch(exe) starts the program under RenderDoc (it must be launched by RenderDoc: injecting
   into an already-rendering process does not work). Remember the returned ident.
2. When the program shows the frame you want, trigger_capture(ident) -> .rdc path(s). No hotkey
   or input needed. The program keeps running; capture again any time.
3. open_capture(path) -> capture_id + summary. Then: list_passes (find e.g. depth prepass /
   GBuffer / lighting), list_actions, pipeline(eid) (shaders, cbuffers, bound textures, targets),
   find_matrices(eid) (camera matrices even when reflection names are stripped), cbuffer,
   save_targets(eid, compare_previous=True) (see what one draw changed), save_texture, pick_pixel.
4. close_capture when done (captures use a lot of RAM/VRAM).

Rules: single-player/offline programs only; never use on games with anti-cheat or online
clients. Captures and exported images contain game assets: keep them local, never publish.
"""

mcp = FastMCP("renderdoc", instructions=INSTRUCTIONS)
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="renderdoc")
_captures = {}


async def _run(fn, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(_worker, functools.partial(fn, *args, **kwargs))


def _get(capture_id):
    if capture_id not in _captures:
        raise ValueError(f"unknown capture_id {capture_id!r}; open: {sorted(_captures)}")
    return _captures[capture_id]


def _default_out(cap, sub):
    stem = os.path.splitext(os.path.basename(cap.path))[0]
    return os.path.join(os.path.dirname(cap.path), f"{stem}_out", sub)


# ---------------- capture side ----------------

@mcp.tool()
async def launch(exe: str, args: str = "", working_dir: str = "", capture_template: str = "",
                 hook_children: bool = False, api_validation: bool = False,
                 preload_crt: bool = False, wait_for_api_s: float = 0) -> dict:
    """Start a program under RenderDoc (created suspended, injected, resumed).

    exe: full path to the process that actually renders (for Unreal games the
      *-Win64-Shipping.exe, not the small launcher; or set hook_children=True on the launcher).
    capture_template: path prefix for .rdc files, e.g. D:/captures/game/game -> game_frameN.rdc.
    wait_for_api_s: if > 0, block until the graphics device is created and report the API.
    Returns pid and ident (needed by trigger_capture/target_status).
    """
    def go():
        opts = launcher.capture_options(hook_children=hook_children, api_validation=api_validation)
        r = launcher.launch(exe, args, working_dir, capture_template, opts, preload_crt)
        if wait_for_api_s > 0:
            r["api"] = launcher.wait_for_api(r["ident"], wait_for_api_s)
        return r
    return await _run(go)


@mcp.tool()
async def target_status(ident: int) -> dict:
    """Connection status, API and existing captures of a program launched under RenderDoc."""
    return await _run(launcher.status, ident)


@mcp.tool()
async def trigger_capture(ident: int, frames: int = 1, delay_s: float = 0) -> dict:
    """Capture the next `frames` frames of a launched program; returns the new .rdc paths.

    The program must be rendering (not minimized). Capture files can be several GB."""
    return {"captures": await _run(launcher.trigger, ident, frames, delay_s)}


# ---------------- analysis side ----------------

@mcp.tool()
async def thumbnail(capture_path: str, out_path: str = "") -> dict:
    """Save the capture's embedded thumbnail as PNG without loading the capture (fast).
    Look at it to check the frame shows what you wanted before a slow open_capture."""
    out = out_path or os.path.splitext(capture_path)[0] + "_thumb.png"
    return await _run(replay.thumbnail, capture_path, out)


@mcp.tool()
async def open_capture(capture_path: str) -> dict:
    """Load a .rdc for replay and keep it open. Returns capture_id and a summary.
    Large captures take tens of seconds and a lot of memory."""
    def go():
        cid = os.path.splitext(os.path.basename(capture_path))[0]
        if cid in _captures:
            return {"capture_id": cid, "already_open": True, **_captures[cid].summary()}
        cap = replay.Capture(capture_path)
        _captures[cid] = cap
        return {"capture_id": cid, **cap.summary()}
    return await _run(go)


@mcp.tool()
async def close_capture(capture_id: str) -> dict:
    """Unload a capture and free its replay memory."""
    def go():
        _get(capture_id).close()
        del _captures[capture_id]
        return {"closed": capture_id, "still_open": sorted(_captures)}
    return await _run(go)


@mcp.tool()
async def list_open_captures() -> dict:
    """Captures currently loaded."""
    return {"open": sorted(_captures)}


@mcp.tool()
async def list_passes(capture_id: str, include_dispatch: bool = False, largest_first: bool = False,
                      min_count: int = 1, limit: int = 60) -> dict:
    """Render passes: consecutive draws grouped by their colour targets + depth target.
    Each pass has first/last eid, draw count, targets (id, size, format) and any debug marker
    path. Shipping builds often have no markers; identify passes by targets instead (e.g. a
    depth-only pass = depth prepass/shadows, 4-6 targets + depth = GBuffer)."""
    def go():
        cap = _get(capture_id)
        ps = cap.list_passes(include_dispatch, min_count, largest_first, limit)
        return {"passes": ps, "total_passes": len(cap.passes())}
    return await _run(go)


@mcp.tool()
async def list_actions(capture_id: str, first_eid: int, last_eid: int, only_work: bool = True,
                       limit: int = 200) -> dict:
    """Actions (draws, dispatches; or everything with only_work=False) in an event range,
    with index/instance counts. Large index counts are the real meshes."""
    return {"actions": await _run(lambda: _get(capture_id).list_actions(first_eid, last_eid, only_work, limit))}


@mcp.tool()
async def pipeline(capture_id: str, eid: int) -> dict:
    """Pipeline state at an action: shaders per stage, cbuffers (index, name, size, buffer),
    bound textures (SRVs: id, size, format - how to find a sprite atlas or a material's
    textures), render targets, depth target, depth state (test/write/func - e.g. a base pass
    with write=false + Equal means depth came from a prepass), stencil and viewport."""
    return await _run(lambda: _get(capture_id).pipeline(eid))


@mcp.tool()
async def cbuffer(capture_id: str, eid: int, stage: str = "vs", index: int = 0,
                  raw_bytes: int = 1024) -> dict:
    """Contents of one bound constant buffer. Named variables when reflection survives,
    otherwise raw float4 rows keyed by byte offset (first raw_bytes bytes)."""
    return await _run(lambda: _get(capture_id).cbuffer(eid, stage, index, raw_bytes))


@mcp.tool()
async def find_matrices(capture_id: str, eid: int, stage: str = "vs", kinds: list[str] | None = None,
                        verbose: bool = False) -> dict:
    """Scan the cbuffers bound at an action for 4x4 matrices and classify them by structure:
    perspective (fov, aspect, near/far, reversed-Z, TAA jitter), world_to_clip (fov, camera
    forward, camera-relative or absolute), rigid (view rotation), and inverses of these.
    Works without reflection names. kinds filters, e.g. ["perspective", "world_to_clip"].
    Pick a draw of a real mesh in the main scene pass (e.g. GBuffer)."""
    return {"matrices": await _run(lambda: _get(capture_id).find_matrices(eid, stage, kinds, verbose))}


@mcp.tool()
async def save_targets(capture_id: str, eid: int, out_dir: str = "", compare_previous: bool = False) -> dict:
    """Save the colour targets and depth at an action as PNG (depth normalised to its range).
    compare_previous=True also saves them just before this action, so diffing the images shows
    exactly what this draw drew."""
    def go():
        cap = _get(capture_id)
        return cap.save_targets(eid, out_dir or _default_out(cap, f"eid{eid}"), compare_previous)
    return await _run(go)


@mcp.tool()
async def save_texture(capture_id: str, resource_id: int, out_path: str = "", eid: int | None = None,
                       mip: int = 0, slice: int = 0) -> dict:
    """Save any texture (by id from pipeline/list_textures/list_passes) as PNG, optionally as
    it is at a given eid. Depth textures are normalised to their min/max."""
    def go():
        cap = _get(capture_id)
        out = out_path or os.path.join(_default_out(cap, "textures"),
                                       f"tex{resource_id}" + (f"_eid{eid}" if eid else "") + ".png")
        return cap.save_texture(resource_id, out, eid, mip, slice)
    return await _run(go)


@mcp.tool()
async def pick_pixel(capture_id: str, eid: int, resource_id: int, x: int, y: int) -> dict:
    """Exact value of one texel of a texture at an action (e.g. raw depth to validate a
    reconstructed view-space depth, or a GBuffer normal)."""
    return await _run(lambda: _get(capture_id).pick_pixel(eid, resource_id, x, y))


@mcp.tool()
async def list_textures(capture_id: str, name_filter: str = "", min_width: int = 0,
                        depth_only: bool = False, limit: int = 100) -> dict:
    """Textures in the capture, largest first. name_filter matches name or format."""
    return {"textures": await _run(lambda: _get(capture_id).list_textures(name_filter, min_width, depth_only, limit))}


def main():
    # MCP speaks JSON over stdout. Keep a private handle on the real stdout for the protocol
    # and point fd 1 at stderr, so anything native code prints can't corrupt the stream.
    real = os.dup(1)
    msvcrt.setmode(real, os.O_BINARY)
    os.dup2(2, 1)
    sys.stdout = io.TextIOWrapper(io.BufferedWriter(io.FileIO(real, "w")), encoding="utf-8",
                                  newline="\n", write_through=True)
    try:
        mcp.run()
    finally:
        for cap in list(_captures.values()):
            try:
                cap.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
