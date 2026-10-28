"""Verifica que el .exe empaquetado muestre su ícono: lo lanza y le pregunta a Windows (WM_GETICON, lo que usa el taskbar) por los píxeles reales
de la ventana, y comprueba que el .exe lleve embebido SU ícono (no el genérico de Windows).

    python tools/check_exe_icon.py ["dist/ZH3 Marea/ZH3 Marea.exe"]     # código de salida 0 = bien

Existe porque «no se ve el ícono en la barra de tareas» no lo detecta ninguna prueba unitaria: depende del empaquetado.
"""
import ctypes
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V = ctypes.c_void_p
u32, g32, sh = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.shell32
u32.GetIconInfo.argtypes = [V, V]
u32.GetDC.argtypes, u32.GetDC.restype = [V], V
u32.ReleaseDC.argtypes = [V, V]
u32.SendMessageTimeoutW.restype = V
u32.LoadImageW.argtypes, u32.LoadImageW.restype = [V, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT], V
g32.GetObjectW.argtypes = [V, ctypes.c_int, V]
g32.GetDIBits.argtypes = [V, V, wintypes.UINT, wintypes.UINT, V, V, wintypes.UINT]
sh.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(V), ctypes.POINTER(V), wintypes.UINT]
sh.ExtractIconExW.restype = wintypes.UINT


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD), ("hbmMask", V), ("hbmColor", V)]


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD), ("x", wintypes.LONG), ("y", wintypes.LONG), ("c", wintypes.DWORD), ("d", wintypes.DWORD)]


def icon_pixels(hicon) -> tuple[int, int, tuple[int, int, int]]:
    """(lado, % de píxeles con alfa alto, color medio RGB) de un HICON."""
    info = ICONINFO()
    u32.GetIconInfo(hicon, ctypes.byref(info))
    bm = (ctypes.c_byte * 32)()
    g32.GetObjectW(info.hbmColor, 32, bm)
    w, h = struct.unpack_from("ii", bytes(bm), 4)
    buf = (ctypes.c_ubyte * (w * h * 4))()
    dc = u32.GetDC(None)
    g32.GetDIBits(dc, info.hbmColor, 0, h, buf, ctypes.byref(BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0)), 0)
    u32.ReleaseDC(None, dc)
    d = bytes(buf)
    px = [(d[i + 2], d[i + 1], d[i]) for i in range(0, len(d), 4) if d[i + 3] > 200]
    mean = tuple(round(sum(p[k] for p in px) / len(px)) if px else 0 for k in range(3))
    return w, round(100 * len(px) / (w * h)), mean


def visible_windows(pids: set[int]) -> list[tuple[int, str]]:
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip():
                out.append((hwnd, buf.value))
        return True

    u32.EnumWindows(cb, 0)
    return out


def process_tree(root: int) -> set[int]:
    ps = (f"$ids=@({root}); $all=Get-CimInstance Win32_Process; do {{ $n=@($all | ? {{ $ids -contains $_.ParentProcessId -and $ids -notcontains $_.ProcessId }} "
          "| % { $_.ProcessId }); $ids+=$n } while ($n.Count); $ids -join ','")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
    return {int(x) for x in out.split(",") if x}


def window_icon(hwnd, which) -> int:
    res = V()
    u32.SendMessageTimeoutW(hwnd, 0x7F, which, 0, 2, 1000, ctypes.byref(res))  # WM_GETICON
    return res.value or 0


def check(exe: Path, ico: Path) -> list[str]:
    problems = []
    # 1) el .exe lleva embebido SU ícono (mismo color que marea.ico y distinto del genérico de Windows)
    large = V()
    if sh.ExtractIconExW(str(exe), -1, None, None, 0) == 0:
        problems.append("el .exe no tiene ningún ícono embebido (revisa icon= en marea.spec)")
    else:
        sh.ExtractIconExW(str(exe), 0, ctypes.byref(large), None, 1)
        ref = u32.LoadImageW(None, str(ico), 1, 32, 32, 0x10)
        embedded, expected = icon_pixels(large)[2], icon_pixels(ref)[2]
        if max(abs(a - b) for a, b in zip(embedded, expected)) > 6:
            problems.append(f"el ícono embebido {embedded} no coincide con {ico.name} {expected}")
    # 2) la ventana en ejecución tiene ícono visible, desde el primer instante en que aparece
    p = subprocess.Popen([str(exe)], cwd=exe.parent)
    try:
        win, t0 = None, time.perf_counter()
        while time.perf_counter() - t0 < 40 and not win:
            time.sleep(0.05)
            found = visible_windows(process_tree(p.pid)) if int(time.perf_counter() * 20) % 10 == 0 else []
            win = found[0] if found else None
        if not win:
            return problems + ["la app no mostró ninguna ventana en 40 s"]
        first = {name: window_icon(win[0], w) for name, w in (("pequeño", 0), ("grande", 1))}
        time.sleep(2.5)
        for name, which in (("pequeño", 0), ("grande", 1)):
            h = window_icon(win[0], which)
            if not h:
                problems.append(f"la ventana no tiene ícono {name} (WM_GETICON = 0)")
                continue
            side, pct, mean = icon_pixels(h)
            if pct < 50:
                problems.append(f"el ícono {name} de la ventana ({side}px) está casi vacío: solo {pct}% de píxeles visibles")
            if not first[name]:
                problems.append(f"la ventana apareció SIN ícono {name} y lo recibió después (el taskbar puede quedarse con uno en blanco)")
    finally:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        try:
            p.wait(15)  # que el .exe suelte sus archivos (marea.log) antes de que quien nos llama limpie data/
        except subprocess.TimeoutExpired:
            problems.append("la app no cerró tras taskkill")
    return problems


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "ZH3 Marea" / "ZH3 Marea.exe"
    issues = check(target.resolve(), ROOT / "assets" / "logo" / "marea.ico")
    print("ÍCONO OK: el .exe y su ventana muestran el ícono de la app." if not issues else "PROBLEMAS DE ÍCONO:\n  - " + "\n  - ".join(issues))
    sys.exit(1 if issues else 0)
