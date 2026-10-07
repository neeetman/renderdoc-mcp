"""End-to-end over real MCP stdio against a real capture. Skipped unless
RENDERDOC_MCP_LIVE_RDC=<path to .rdc> (and optionally RENDERDOC_MCP_LIVE_EID=<a mesh draw eid>).

  set RENDERDOC_MCP_LIVE_RDC=D:\\captures\\game\\game_frame123.rdc
  uv run pytest -q tests/test_live.py -s
"""
import asyncio
import json
import os
import sys

import pytest

RDC = os.environ.get("RENDERDOC_MCP_LIVE_RDC")
pytestmark = pytest.mark.skipif(not RDC, reason="set RENDERDOC_MCP_LIVE_RDC to run")


def _data(res):
    assert not res.isError, res.content
    if getattr(res, "structuredContent", None):
        sc = res.structuredContent
        return sc.get("result", sc)
    return json.loads(res.content[0].text)


async def _session():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "renderdoc_mcp.server"],
                                   env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            assert {"launch", "trigger_capture", "open_capture", "find_matrices", "save_targets"} <= tools

            summary = _data(await s.call_tool("open_capture", {"capture_path": RDC}))
            cid = summary["capture_id"]
            print("\nsummary:", {k: summary[k] for k in ("api", "actions", "by_kind", "passes")})

            passes = _data(await s.call_tool("list_passes", {"capture_id": cid, "largest_first": True, "limit": 5}))["passes"]
            assert passes and passes[0]["kind"] == "draw"
            main = passes[0]
            print("largest pass:", main["first_eid"], main["last_eid"], main["count"],
                  [t["format"] for t in main["targets"]])

            eid = int(os.environ.get("RENDERDOC_MCP_LIVE_EID", 0))
            if not eid:
                acts = _data(await s.call_tool("list_actions", {"capture_id": cid, "first_eid": main["first_eid"],
                                                                "last_eid": main["last_eid"], "limit": 400}))["actions"]
                eid = max(acts, key=lambda a: a["indices"])["eid"]
            pipe = _data(await s.call_tool("pipeline", {"capture_id": cid, "eid": eid}))
            print("pipeline stages:", {k: (len(v["cbuffers"]), len(v["textures"])) for k, v in pipe["stages"].items()})

            mats = _data(await s.call_tool("find_matrices", {"capture_id": cid, "eid": eid,
                                                             "kinds": ["perspective", "world_to_clip"]}))["matrices"]
            for m in mats[:6]:
                print("  +%d %s" % (m["offset"], m["summary"]))
            assert any("PERSPECTIVE" in m["summary"] for m in mats)

            cb = _data(await s.call_tool("cbuffer", {"capture_id": cid, "eid": eid, "stage": "vs", "index": 0,
                                                     "raw_bytes": 128}))
            assert "rows" in cb or "variables" in cb

            saved = _data(await s.call_tool("save_targets", {"capture_id": cid, "eid": eid,
                                                             "compare_previous": True}))
            for item in saved["after"] + saved["before"]:
                assert os.path.exists(item["path"]), item
            print("saved:", [os.path.basename(i["path"]) for i in saved["after"] + saved["before"]])

            depth = pipe.get("depth")
            if depth:
                px = _data(await s.call_tool("pick_pixel", {"capture_id": cid, "eid": eid, "resource_id": depth["id"],
                                                            "x": depth["width"] // 2, "y": depth["height"] * 3 // 4}))
                print("depth texel:", px["float"][0])

            _data(await s.call_tool("close_capture", {"capture_id": cid}))


def test_live_end_to_end():
    asyncio.run(_session())
