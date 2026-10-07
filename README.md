# renderdoc-mcp

An MCP server that lets an agent capture GPU frames with [RenderDoc](https://renderdoc.org) and
read them headlessly: render passes, draws, pipeline state, constant buffers, camera matrices,
render targets, depth. Windows, D3D11 / D3D12 / Vulkan / OpenGL. Uses RenderDoc's own Python
replay API, built from a RenderDoc source checkout.

Verified end to end on an Unreal Engine 5.1 D3D12 game (3.7 GB capture, opened in ~26 s):
depth prepass and GBuffer found by their targets, the View uniform buffer's camera matrices
identified with reflection names stripped (75 deg horizontal FOV, reversed-Z infinite far,
near 4, TAA jitter, camera-relative world->clip), GBuffer normals and scene depth exported.

## Tools

| Tool | What it does |
|---|---|
| `launch(exe, args, capture_template, hook_children, wait_for_api_s)` | start a program under RenderDoc (created suspended, injected, resumed) → `pid`, `ident` |
| `trigger_capture(ident, frames)` | capture the next frame(s) remotely, no hotkey or input → `.rdc` paths |
| `target_status(ident)` | connected?, API, captures so far |
| `thumbnail(capture_path)` | the capture's embedded thumbnail, without loading it |
| `open_capture(path)` / `close_capture(id)` / `list_open_captures()` | captures stay loaded between calls |
| `list_passes(id, largest_first)` | draws grouped by colour + depth targets, with eid ranges |
| `list_actions(id, first_eid, last_eid)` | draws / dispatches with index and instance counts |
| `pipeline(id, eid)` | shaders, cbuffers, bound textures (SRVs), targets, depth, viewport |
| `cbuffer(id, eid, stage, index)` | named variables, or raw float4 rows when reflection is stripped |
| `find_matrices(id, eid, stage, kinds)` | perspective / world→clip / rigid / inverse matrices by structure |
| `save_targets(id, eid, compare_previous)` | targets + depth as PNG, optionally also just before the draw |
| `save_texture(id, resource_id, eid)` | any texture as PNG (depth normalised to its range) |
| `pick_pixel(id, eid, resource_id, x, y)` | exact texel value |
| `list_textures(id, name_filter, depth_only)` | textures, largest first |

Typical session: `launch` → play to the frame → `trigger_capture` → `thumbnail` (right frame?)
→ `open_capture` → `list_passes(largest_first=True)` → `list_actions` in the main scene pass
→ `find_matrices` on a big mesh draw → `save_targets` → `close_capture`.

## Setup

Needs Visual Studio 2022 with the C++ workload, Python 3.10 (`py -3.10`) and
[uv](https://docs.astral.sh/uv/).

1. Clone this repo next to a RenderDoc checkout:

   ```text
   <dir>\renderdoc       git clone https://github.com/baldurk/renderdoc.git
   <dir>\renderdoc-mcp   this repo
   ```

2. Build RenderDoc for it (static CRT, Python module for 3.10), from this folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File build_renderdoc.ps1
   ```

   `-RenderDocRoot <path>` if the checkout lives elsewhere.

3. Install and register (user scope = every project):

   ```powershell
   uv sync
   claude mcp add --scope user renderdoc -- uv --directory <dir>\renderdoc-mcp run renderdoc-mcp
   ```

   Other agents: command `uv`, args `--directory <dir>\renderdoc-mcp run renderdoc-mcp`, stdio.
   The build folder is found as `$RENDERDOC_DIR`, else `..\renderdoc\x64\Release`, else
   `..\x64\Release`.

3. Tests (pure logic, no GPU): `uv run pytest -q tests`

## Gotchas (all hit while building this)

- **RenderDoc must start the program.** Injecting into a process that already created its
  graphics device doesn't work (RenderDoc's own docs). Use `launch`, not attach.
- **Unreal games: launch the `*-Win64-Shipping.exe`** in `<Project>\Binaries\Win64`, not the
  small launcher exe in the game root (or pass `hook_children=True`).
- **"Failed to inject renderdoc.dll" on games that ship an old VC runtime.** Many games put
  `msvcp140.dll`/`vcruntime140.dll` (e.g. 14.24 from 2019) next to their exe. The loader
  prefers that folder, and a renderdoc.dll built with a newer toolset and the dynamic CRT
  can't run on an older runtime: `LoadLibrary` fails before `DllMain`, so RenderDoc writes no
  log at all. Proven by loading the game's CRT into a clean `cmd.exe` first, then
  renderdoc.dll (fails) vs the System32 CRT (works). Fix: build with the static CRT
  (`static_crt.props`, used by `build_renderdoc.ps1`). Official installers build with an
  older toolset, so they don't hit it.
- **The Python module is tied to one Python minor version.** RenderDoc bundles 3.6; the build
  script rebuilds `renderdoc.pyd` for 3.10 via `VSPythonOverridePath`.
- **Reconnecting to a target replays its old captures** as `NewCapture` messages;
  `trigger_capture` drains them before triggering so it only returns new files.
- **Root CBVs (D3D12) report "rest of the buffer" as their size.** Scans clamp to the shader's
  declared size, at most 64 KiB, or they read megabytes of unrelated data.
- **Shipping builds strip reflection names** (`cbuffer0`, no variables). `find_matrices`
  classifies by structure and also flags inverses; `cbuffer` falls back to raw float4 rows.
- **Depth/stencil formats**: depth is exported from channel 0 only, normalised to min/max.
  Reversed-Z: 0 = far/sky, larger = nearer. A target filled with RenderDoc's "DISCARDED"
  pattern was discarded in the frame; use the pass whose depth is the main scene depth.
- **Captures are big** (1-4 GB for a modern game frame) and replay needs RAM/VRAM; close them.
- Window size flags like `-ResX/-ResY` may be ignored by the game; captures use its settings.

## Rules

Single-player / offline programs only. Never use on games with anti-cheat or on online
clients. Captures and exported images contain the game's assets: keep them local, never
commit or publish them.
