"""Launch a program under RenderDoc and trigger captures through target control.

The process is created suspended, RenderDoc is injected, then the main thread resumes - the
same sequence RenderDoc's own ExecuteAndInject uses, with an optional step in between that
preloads the System32 VC runtime (only useful for a renderdoc.dll built with the dynamic CRT;
the static-CRT build from build_renderdoc.ps1 doesn't need it).
"""
import ctypes
import os
import time
from ctypes import wintypes as wt

from . import rdlib

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_SUSPENDED = 0x00000004
MEM_COMMIT, MEM_RESERVE, MEM_RELEASE = 0x1000, 0x2000, 0x8000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF
CRT_DLLS = ["ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
                ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE),
                ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
                               wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
                               ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualFreeEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_void_p]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetProcAddress.argtypes = [wt.HMODULE, ctypes.c_char_p]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.CreateRemoteThread.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                   ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
k32.CreateRemoteThread.restype = wt.HANDLE
k32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.ResumeThread.argtypes = [wt.HANDLE]
k32.ResumeThread.restype = wt.DWORD
k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]


def _remote_loadlibrary(hproc, path):
    """LoadLibraryW(path) inside hproc; returns the low 32 bits of the HMODULE (0 = failed)."""
    buf = ctypes.create_unicode_buffer(path)
    size = ctypes.sizeof(buf)
    mem = k32.VirtualAllocEx(hproc, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    if not mem:
        raise OSError(ctypes.get_last_error(), "VirtualAllocEx")
    try:
        if not k32.WriteProcessMemory(hproc, mem, buf, size, None):
            raise OSError(ctypes.get_last_error(), "WriteProcessMemory")
        # kernel32 sits at the same base in every process of a boot session
        fn = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
        th = k32.CreateRemoteThread(hproc, None, 0, fn, mem, 0, None)
        if not th:
            raise OSError(ctypes.get_last_error(), "CreateRemoteThread")
        k32.WaitForSingleObject(th, INFINITE)
        code = wt.DWORD()
        k32.GetExitCodeThread(th, ctypes.byref(code))
        k32.CloseHandle(th)
        return code.value
    finally:
        k32.VirtualFreeEx(hproc, mem, 0, MEM_RELEASE)


def capture_options(hook_children=False, api_validation=False, ref_all_resources=False,
                    capture_callstacks=False):
    rd = rdlib.load()
    o = rd.CaptureOptions()
    o.hookIntoChildren = hook_children
    o.apiValidation = api_validation
    o.refAllResources = ref_all_resources
    o.captureCallstacks = capture_callstacks
    return o


def launch(exe, args="", working_dir="", capture_template="", opts=None, preload_crt=False):
    """Create exe suspended, inject RenderDoc, resume. Returns dict(pid, ident, log)."""
    rd = rdlib.load()
    if not os.path.isfile(exe):
        raise FileNotFoundError(exe)
    workdir = working_dir or os.path.dirname(exe)
    if capture_template:
        os.makedirs(os.path.dirname(capture_template) or ".", exist_ok=True)
    cmd = ctypes.create_unicode_buffer(f'"{exe}" {args}'.strip())
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    if not k32.CreateProcessW(exe, cmd, None, None, False, CREATE_SUSPENDED, None, workdir,
                              ctypes.byref(si), ctypes.byref(pi)):
        raise OSError(ctypes.get_last_error(), f"CreateProcessW({exe})")
    log = [f"created pid {pi.dwProcessId} suspended"]
    ok = False
    try:
        if preload_crt:
            sysdir = os.path.join(os.environ["SystemRoot"], "System32")
            for name in CRT_DLLS:
                h = _remote_loadlibrary(pi.hProcess, os.path.join(sysdir, name))
                log.append(f"preload {name}: {'ok' if h else 'FAILED'}")
        res = rd.InjectIntoProcess(pi.dwProcessId, [], capture_template,
                                   opts or rd.CaptureOptions(), False)
        log.append(f"inject: {res.result}")
        if res.ident == 0:
            raise RuntimeError(
                f"RenderDoc injection failed: {res.result}. If the program ships an old "
                f"msvcp140/vcruntime140 next to its exe, use a static-CRT renderdoc build "
                f"(build_renderdoc.ps1) or preload_crt=True. Log: {log}")
        ok = True
        return {"pid": pi.dwProcessId, "ident": res.ident, "log": log}
    finally:
        if ok:
            while k32.ResumeThread(pi.hThread) > 1:
                pass
        else:
            k32.TerminateProcess(pi.hProcess, 1)
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)


def _connect(ident, timeout_s=30):
    rd = rdlib.load()
    end = time.time() + timeout_s
    while time.time() < end:
        tc = rd.CreateTargetControl("", ident, "renderdoc-mcp", True)
        if tc is not None and tc.Connected():
            return tc
        time.sleep(0.5)
    raise RuntimeError(f"could not connect to RenderDoc target ident={ident} (process exited?)")


def _drain(tc, rd, seconds):
    """Read pending messages; returns (existing capture paths, apis, disconnected)."""
    paths, apis = [], []
    end = time.time() + seconds
    while time.time() < end:
        msg = tc.ReceiveMessage(None)
        t = msg.type
        if t == rd.TargetControlMessageType.NewCapture:
            paths.append(msg.newCapture.path)
            end = max(end, time.time() + 0.5)
        elif t == rd.TargetControlMessageType.RegisterAPI:
            apis.append(msg.apiUse.name)
        elif t == rd.TargetControlMessageType.Disconnected:
            return paths, apis, True
    return paths, apis, False


def status(ident):
    rd = rdlib.load()
    tc = _connect(ident, 10)
    try:
        paths, apis, gone = _drain(tc, rd, 1.5)
        return {"ident": ident, "target": tc.GetTarget(), "pid": tc.GetPID(),
                "api": tc.GetAPI() or (apis[-1] if apis else ""), "captures": paths,
                "connected": not gone}
    finally:
        tc.Shutdown()


def wait_for_api(ident, timeout_s=120):
    """Block until the target reports a graphics API (device created)."""
    rd = rdlib.load()
    tc = _connect(ident)
    try:
        end = time.time() + timeout_s
        while time.time() < end:
            if tc.GetAPI():
                return tc.GetAPI()
            _, apis, gone = _drain(tc, rd, 0.5)
            if gone:
                raise RuntimeError("target exited before creating a graphics device")
            if apis:
                return apis[-1]
        raise TimeoutError("no graphics API registered yet")
    finally:
        tc.Shutdown()


def trigger(ident, frames=1, delay_s=0.0, timeout_s=180):
    """Capture `frames` consecutive frames; returns the new capture paths."""
    rd = rdlib.load()
    tc = _connect(ident)
    try:
        # on connect the target replays its existing captures as NewCapture messages
        old, _, gone = _drain(tc, rd, 1.5)
        if gone:
            raise RuntimeError("target disconnected (process exited?)")
        if delay_s:
            time.sleep(delay_s)
        tc.TriggerCapture(frames)
        new = []
        end = time.time() + timeout_s
        while time.time() < end and len(new) < frames:
            msg = tc.ReceiveMessage(None)
            t = msg.type
            if t == rd.TargetControlMessageType.NewCapture and msg.newCapture.path not in old:
                c = msg.newCapture
                new.append({"path": c.path, "frame": c.frameNumber, "api": str(c.api),
                            "bytes": os.path.getsize(c.path) if os.path.exists(c.path) else None})
            elif t == rd.TargetControlMessageType.Disconnected:
                raise RuntimeError("target disconnected while capturing")
        if not new:
            raise TimeoutError("no capture arrived; is the program rendering frames (not minimized)?")
        return new
    finally:
        tc.Shutdown()
