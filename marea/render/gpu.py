"""Contexto OpenGL sin ventana (Qt) + superficies GPU de skia, para el export y las pruebas.

La preview dentro de la app usará el contexto de su propio widget; esto es para renderizar sin ventana.
Llamar a `close()` antes de salir: si no, el proceso puede colgarse al destruir el contexto GL.
"""
import numpy as np
import skia
from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtWidgets import QApplication


class CpuContext:
    """Mismo contrato que GpuContext pero en CPU (raster): ~50× más lento; para equipos sin OpenGL y para tests."""

    gpu = False

    def make_surface(self, w: int, h: int) -> skia.Surface:
        return skia.Surface(w, h)

    def read_rgba(self, surface: skia.Surface) -> np.ndarray:
        return surface.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)

    def close(self):
        pass


def make_context(prefer_gpu: bool = True):
    """GpuContext si hay OpenGL 3.3; si no, CpuContext."""
    if prefer_gpu:
        try:
            return GpuContext()
        except RuntimeError:
            pass
    return CpuContext()


class GpuContext:
    gpu = True

    def __init__(self):
        self._app = QApplication.instance() or QApplication([])  # QApplication: la UI comparte el mismo proceso
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setStencilBufferSize(8)
        self._surface = QOffscreenSurface()
        self._surface.setFormat(fmt)
        self._surface.create()
        self._gl = QOpenGLContext()
        self._gl.setFormat(fmt)
        if not (self._gl.create() and self._gl.makeCurrent(self._surface)):
            raise RuntimeError("No se pudo crear un contexto OpenGL 3.3. Actualiza el driver de la tarjeta de video.")
        self.ctx = skia.GrDirectContext.MakeGL()
        if self.ctx is None:
            raise RuntimeError("skia no pudo usar el contexto OpenGL.")

    def make_surface(self, w: int, h: int) -> skia.Surface:
        return skia.Surface.MakeRenderTarget(self.ctx, skia.Budgeted.kNo, skia.ImageInfo.MakeN32Premul(w, h))

    def read_rgba(self, surface: skia.Surface) -> np.ndarray:
        """Frame como array (alto, ancho, 4) uint8 RGBA, tal cual se manda a ffmpeg."""
        self.ctx.flushAndSubmit()
        return surface.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)

    def close(self):
        self.ctx.releaseResourcesAndAbandonContext()
        self._gl.doneCurrent()
