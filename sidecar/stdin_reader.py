"""Lectura de stdin sin bloquear. Módulo mínimo (solo stdlib) a propósito: tiene que poder importarse ANTES que numpy y cualquier librería nativa."""
import os
import time

FILE_TYPE_PIPE = 3


def stdin_lines():
    """Líneas de stdin (bytes, con su salto) SIN dejar nunca un hilo bloqueado en una lectura síncrona del pipe.

    En Windows, mientras un hilo está dentro de `ReadFile` esperando datos del pipe de stdin, el hilo principal se queda colgado al cargar
    ciertas DLL nativas (se midió con `import scipy.linalg.blas`, que `audio-separator` arrastra: 0 % de CPU para siempre). Por eso aquí se
    pregunta con `PeekNamedPipe` si hay datos y solo entonces se lee; el costo es ≤ 20 ms de latencia por orden."""
    if os.name != "nt":
        yield from open(0, "rb", closefd=False)
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(0))
    if k32.GetFileType(handle) != FILE_TYPE_PIPE:  # un archivo o la consola: una lectura bloqueante no tiene este problema
        yield from open(0, "rb", closefd=False)
        return
    avail, buf = wintypes.DWORD(), b""
    while k32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):  # falla cuando el otro extremo cerró y no queda nada
        if not avail.value:
            time.sleep(0.02)
            continue
        buf += os.read(0, avail.value)  # hay datos: no bloquea
        while (i := buf.find(b"\n")) >= 0:
            yield buf[:i + 1]
            buf = buf[i + 1:]
    if buf:
        yield buf
