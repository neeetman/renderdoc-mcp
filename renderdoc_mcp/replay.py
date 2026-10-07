"""A loaded RenderDoc capture kept open for repeated queries.

All methods must run on the same thread (the server's single RenderDoc worker thread).
"""
import os

from . import matrices, rdlib

STAGE_NAMES = {"vs": "Vertex", "hs": "Hull", "ds": "Domain", "gs": "Geometry", "ps": "Pixel",
               "cs": "Compute", "ms": "Mesh", "as": "Amplification"}

_initialised = False


def _rd():
    global _initialised
    rd = rdlib.load()
    if not _initialised:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        _initialised = True
    return rd


def _ok(res, rd):
    return getattr(res, "code", res) == rd.ResultCode.Succeeded


def stage_of(name):
    rd = _rd()
    key = name.lower()
    if key not in STAGE_NAMES:
        raise ValueError(f"stage must be one of {sorted(STAGE_NAMES)}")
    return getattr(rd.ShaderStage, STAGE_NAMES[key])


def thumbnail(path, out_path):
    rd = _rd()
    cap = rd.OpenCaptureFile()
    try:
        if not _ok(cap.OpenFile(path, "", None), rd):
            raise RuntimeError(f"cannot open {path}")
        th = cap.GetThumbnail(rd.FileType.PNG, 0)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(bytes(th.data))
        return {"path": out_path, "width": th.width, "height": th.height}
    finally:
        cap.Shutdown()


class Capture:
    def __init__(self, path):
        rd = _rd()
        self.rd = rd
        self.path = path
        self.cap = rd.OpenCaptureFile()
        res = self.cap.OpenFile(path, "", None)
        if not _ok(res, rd):
            raise RuntimeError(f"cannot open {path}: {res}")
        if self.cap.LocalReplaySupport() != rd.ReplaySupport.Supported:
            raise RuntimeError(f"capture not replayable on this machine: {self.cap.LocalReplaySupport()}")
        res, self.c = self.cap.OpenCapture(rd.ReplayOptions(), None)
        if not _ok(res, rd):
            raise RuntimeError(f"replay failed: {res}")
        self.sf = self.c.GetStructuredFile()
        self.textures = {int(t.resourceId): t for t in self.c.GetTextures()}
        self.buffers = {int(b.resourceId): b for b in self.c.GetBuffers()}
        self.resources = {int(r.resourceId): r for r in self.c.GetResources()}
        self.ids = {int(r.resourceId): r.resourceId for r in self.c.GetResources()}
        self.flat = []
        self._walk(self.c.GetRootActions(), [])
        self.by_eid = {a.eventId: i for i, (a, _) in enumerate(self.flat)}
        self._passes = None

    def close(self):
        self.c.Shutdown()
        self.cap.Shutdown()

    # ---------- helpers ----------
    def _walk(self, actions, path):
        for a in actions:
            if a.children:
                self._walk(a.children, path + [a.GetName(self.sf)])
            else:
                self.flat.append((a, path))

    def rid(self, n):
        n = int(n)
        if n not in self.ids:
            raise ValueError(f"unknown resource id {n}")
        return self.ids[n]

    def name(self, rid):
        r = self.resources.get(int(rid))
        return r.name if r else str(int(rid))

    def tex_info(self, rid):
        n = int(rid)
        t = self.textures.get(n)
        if not t:
            return {"id": n, "name": self.name(rid)}
        return {"id": n, "name": self.name(rid), "width": t.width, "height": t.height,
                "depth": t.depth, "mips": t.mips, "array": t.arraysize, "format": t.format.Name(),
                "is_depth": bool(t.creationFlags & self.rd.TextureCategory.DepthTarget)}

    def is_work(self, a):
        F = self.rd.ActionFlags
        return bool(a.flags & (F.Drawcall | F.Dispatch | F.MeshDispatch | F.DispatchRay))

    def kind(self, a):
        F = self.rd.ActionFlags
        if a.flags & F.Drawcall:
            return "draw"
        if a.flags & (F.Dispatch | F.MeshDispatch):
            return "dispatch"
        if a.flags & F.DispatchRay:
            return "raytrace"
        if a.flags & (F.Clear | F.ClearColor | F.ClearDepthStencil):
            return "clear"
        if a.flags & (F.Copy | F.Resolve):
            return "copy"
        if a.flags & F.Present:
            return "present"
        return "other"

    def seek(self, eid):
        if eid not in self.by_eid:
            raise ValueError(f"event {eid} is not an action; use list_actions to find valid eids")
        self.c.SetFrameEvent(eid, True)
        return self.c.GetPipelineState()

    def previous_eid(self, eid):
        i = self.by_eid.get(eid)
        return self.flat[i - 1][0].eventId if i else None

    # ---------- queries ----------
    def summary(self):
        props = self.c.GetAPIProperties()
        kinds = {}
        for a, _ in self.flat:
            k = self.kind(a)
            kinds[k] = kinds.get(k, 0) + 1
        swap = [self.tex_info(t.resourceId) for t in self.textures.values()
                if t.creationFlags & self.rd.TextureCategory.SwapBuffer]
        passes = self.passes()
        return {"path": self.path, "api": str(props.pipelineType), "actions": len(self.flat),
                "by_kind": kinds, "textures": len(self.textures), "buffers": len(self.buffers),
                "resources": len(self.resources), "passes": len(passes),
                "swapchain": swap[:2], "first_eid": self.flat[0][0].eventId if self.flat else None,
                "last_eid": self.flat[-1][0].eventId if self.flat else None,
                "has_markers": any(p for _, p in self.flat)}

    def passes(self):
        """Consecutive work grouped by (colour targets, depth target); dispatch runs grouped too."""
        if self._passes is not None:
            return self._passes
        NULL = self.rd.ResourceId.Null()
        out = []
        for a, path in self.flat:
            if not self.is_work(a):
                continue
            k = self.kind(a)
            if k == "draw":
                key = ("draw", tuple(int(o) for o in a.outputs if o != NULL), int(a.depthOut))
            else:
                key = (k,)
            if out and out[-1]["_key"] == key:
                out[-1]["last_eid"] = a.eventId
                out[-1]["count"] += 1
                continue
            p = {"_key": key, "index": len(out), "kind": k, "first_eid": a.eventId,
                 "last_eid": a.eventId, "count": 1, "marker": " > ".join(x for x in path if x)[-120:]}
            if k == "draw":
                p["targets"] = [self.tex_info(self.rid(o)) for o in key[1]]
                p["depth"] = self.tex_info(self.rid(key[2])) if key[2] else None
            out.append(p)
        self._passes = out
        return out

    def list_passes(self, include_dispatch=False, min_count=1, largest_first=False, limit=60):
        ps = [p for p in self.passes() if (include_dispatch or p["kind"] == "draw") and p["count"] >= min_count]
        if largest_first:
            ps = sorted(ps, key=lambda p: -p["count"])
        return [{k: v for k, v in p.items() if k != "_key"} for p in ps[:limit]]

    def list_actions(self, first_eid, last_eid, only_work=True, limit=200):
        out = []
        for a, path in self.flat:
            if a.eventId < first_eid or a.eventId > last_eid:
                continue
            if only_work and not self.is_work(a):
                continue
            out.append({"eid": a.eventId, "kind": self.kind(a), "name": a.GetName(self.sf)[:120],
                        "indices": a.numIndices, "instances": a.numInstances})
            if len(out) >= limit:
                break
        return out

    def pipeline(self, eid):
        rd = self.rd
        pipe = self.seek(eid)
        NULL = rd.ResourceId.Null()
        a = self.flat[self.by_eid[eid]][0]
        out = {"eid": eid, "kind": self.kind(a), "name": a.GetName(self.sf)[:120],
               "indices": a.numIndices, "instances": a.numInstances, "stages": {}}
        compute = self.kind(a) == "dispatch"
        stages = ["cs"] if compute else ["vs", "hs", "ds", "gs", "ms", "as", "ps"]
        for s in stages:
            st = stage_of(s)
            sh = pipe.GetShader(st)
            if sh == NULL:
                continue
            refl = pipe.GetShaderReflection(st)
            info = {"shader": int(sh), "entry": pipe.GetShaderEntryPoint(st), "cbuffers": [],
                    "textures": []}
            if refl:
                for idx, cb in enumerate(refl.constantBlocks):
                    d = pipe.GetConstantBlock(st, idx, 0).descriptor
                    info["cbuffers"].append({"index": idx, "name": cb.name, "declared_bytes": cb.byteSize,
                                             "variables": len(cb.variables),
                                             "buffer": int(d.resource) if d.resource != NULL else None,
                                             "offset": d.byteOffset})
            seen = set()
            for used in pipe.GetReadOnlyResources(st):
                r = used.descriptor.resource
                if r == NULL or int(r) in seen:
                    continue
                seen.add(int(r))
                if int(r) in self.textures:
                    info["textures"].append(self.tex_info(r))
            info["textures"] = info["textures"][:48]
            out["stages"][s] = info
        if not compute:
            out["targets"] = [self.tex_info(o.resource) for o in pipe.GetOutputTargets() if o.resource != NULL]
            dt = pipe.GetDepthTarget().resource
            out["depth"] = self.tex_info(dt) if dt != NULL else None
            vp = pipe.GetViewport(0)
            out["viewport"] = [vp.x, vp.y, vp.width, vp.height, vp.minDepth, vp.maxDepth]
        return out

    def _bound_cbuffers(self, pipe, st):
        NULL = self.rd.ResourceId.Null()
        refl = pipe.GetShaderReflection(st)
        if not refl:
            return refl, []
        out = []
        for idx, cb in enumerate(refl.constantBlocks):
            if not cb.bufferBacked:
                continue
            d = pipe.GetConstantBlock(st, idx, 0).descriptor
            if d.resource != NULL:
                out.append((idx, cb, d))
        return refl, out

    @staticmethod
    def _cb_size(cb, d):
        # root CBVs report "rest of the buffer"; a CBV can't exceed 64 KiB
        return min(d.byteSize, cb.byteSize or 65536, 65536)

    def _var(self, v):
        rd = self.rd
        if v.members:
            return {"name": v.name, "members": [self._var(m) for m in v.members]}
        n = v.rows * v.columns
        if v.type in (rd.VarType.SInt, rd.VarType.SShort, rd.VarType.SByte):
            vals = list(v.value.s32v[:n])
        elif v.type in (rd.VarType.UInt, rd.VarType.UShort, rd.VarType.UByte, rd.VarType.Bool):
            vals = list(v.value.u32v[:n])
        elif v.type == rd.VarType.Double:
            vals = list(v.value.f64v[:n])
        else:
            vals = [round(x, 7) for x in v.value.f32v[:n]]
        if v.rows > 1:
            vals = [vals[r * v.columns:(r + 1) * v.columns] for r in range(v.rows)]
        return {"name": v.name, "value": vals}

    def cbuffer(self, eid, stage="vs", index=0, raw_bytes=1024):
        pipe = self.seek(eid)
        st = stage_of(stage)
        refl, cbs = self._bound_cbuffers(pipe, st)
        match = [x for x in cbs if x[0] == index]
        if not match:
            raise ValueError(f"no bound buffer-backed cbuffer {index} on {stage} at eid {eid}; "
                             f"bound: {[x[0] for x in cbs]}")
        idx, cb, d = match[0]
        size = self._cb_size(cb, d)
        out = {"eid": eid, "stage": stage, "index": idx, "name": cb.name, "bytes": size,
               "buffer": int(d.resource), "offset": d.byteOffset}
        if cb.variables:
            pobj = pipe.GetComputePipelineObject() if stage == "cs" else pipe.GetGraphicsPipelineObject()
            vs = self.c.GetCBufferVariableContents(pobj, pipe.GetShader(st), st, refl.entryPoint, idx,
                                                   d.resource, d.byteOffset, size)
            out["variables"] = [self._var(v) for v in vs]
        else:
            # reflection stripped: raw float4 rows with byte offsets
            import struct
            data = self.c.GetBufferData(d.resource, d.byteOffset, min(size, raw_bytes))
            n = len(data) // 16
            rows = struct.unpack(f"<{n * 4}f", data[:n * 16])
            out["note"] = "no reflection names; raw float4 rows (offset: values)"
            out["rows"] = {str(i * 16): [round(x, 6) for x in rows[i * 4:i * 4 + 4]] for i in range(n)}
        return out

    def find_matrices(self, eid, stage="vs", kinds=None, verbose=False):
        pipe = self.seek(eid)
        st = stage_of(stage)
        _, cbs = self._bound_cbuffers(pipe, st)
        out = []
        for idx, cb, d in cbs:
            size = self._cb_size(cb, d)
            data = self.c.GetBufferData(d.resource, d.byteOffset, size)
            names = {v.byteOffset: v.name for v in cb.variables}
            for m in matrices.scan(data, names, set(kinds) if kinds else None):
                item = {"cbuffer": idx, "cbuffer_name": cb.name, "cbuffer_bytes": size,
                        "offset": m["offset"], "name": m["name"], "also_at": m["also_at"][:6],
                        "summary": matrices.describe(m)}
                if verbose:
                    item["matrix"] = m["matrix"]
                out.append(item)
        return out

    def save_texture(self, resource_id, out_path, eid=None, mip=0, slice_=0, depth_normalize=None):
        rd = self.rd
        if eid is not None:
            self.seek(eid)
        rid = self.rid(resource_id)
        info = self.tex_info(rid)
        ts = rd.TextureSave()
        ts.resourceId = rid
        ts.destType = rd.FileType.PNG
        ts.mip = mip
        ts.slice.sliceIndex = slice_
        ts.alpha = rd.AlphaMapping.Discard
        result = {"texture": info, "path": out_path}
        is_depth = info.get("is_depth") if depth_normalize is None else depth_normalize
        if is_depth:
            ts.channelExtract = 0          # depth only; D24S8/D32S8 would otherwise mix in stencil
            mn, mx = self.c.GetMinMax(rid, rd.Subresource(mip, slice_, 0), rd.CompType.Typeless)
            lo, hi = mn.floatValue[0], mx.floatValue[0]
            ts.comp.blackPoint, ts.comp.whitePoint = lo, (hi if hi > lo else lo + 1e-6)
            result["range"] = [lo, hi]
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        if not self.c.SaveTexture(ts, out_path):
            raise RuntimeError(f"SaveTexture failed for {info}")
        return result

    def save_targets(self, eid, out_dir, compare_previous=False):
        NULL = self.rd.ResourceId.Null()
        pipe = self.seek(eid)
        targets = [o.resource for o in pipe.GetOutputTargets() if o.resource != NULL]
        dt = pipe.GetDepthTarget().resource
        out = {"eid": eid, "after": [], "before": []}
        for i, t in enumerate(targets):
            out["after"].append(self.save_texture(int(t), os.path.join(out_dir, f"eid{eid}_rt{i}.png")))
        if dt != NULL:
            out["after"].append(self.save_texture(int(dt), os.path.join(out_dir, f"eid{eid}_depth.png")))
        prev = self.previous_eid(eid)
        if compare_previous and prev is not None:
            # same resources just before this action: diff the images to see what it drew
            self.seek(prev)
            out["previous_eid"] = prev
            for i, t in enumerate(targets):
                out["before"].append(self.save_texture(int(t), os.path.join(out_dir, f"eid{eid}_rt{i}_before.png")))
            if dt != NULL:
                out["before"].append(self.save_texture(int(dt), os.path.join(out_dir, f"eid{eid}_depth_before.png")))
        return out

    def pick_pixel(self, eid, resource_id, x, y, mip=0, slice_=0):
        rd = self.rd
        self.seek(eid)
        v = self.c.PickPixel(self.rid(resource_id), int(x), int(y), rd.Subresource(mip, slice_, 0),
                             rd.CompType.Typeless)
        return {"eid": eid, "texture": int(resource_id), "x": x, "y": y,
                "float": list(v.floatValue), "uint": list(v.uintValue)}

    def list_textures(self, name_filter="", min_width=0, depth_only=False, limit=100):
        out = []
        for t in self.textures.values():
            info = self.tex_info(t.resourceId)
            if name_filter and name_filter.lower() not in info["name"].lower() and name_filter.lower() not in info["format"].lower():
                continue
            if info["width"] < min_width or (depth_only and not info["is_depth"]):
                continue
            out.append(info)
        out.sort(key=lambda i: -(i["width"] * i["height"]))
        return out[:limit]
