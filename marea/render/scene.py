"""Escena de un clip: dibuja el estado del motor en un canvas de skia.

`Scene.render(canvas)` es lo único que llaman la preview y el export: el motor (marea.engine) avanza
el estado, aquí solo se dibuja. Todo se mide en px del lienzo; blur y glow vienen del motor en px de diseño
(letra de 42 px) y se escalan con `k = tamaño de letra / 42`.
"""
import math
import weakref
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import skia

from marea.engine.engine import Engine, Params
from marea.engine.tuning import TUNING
from marea.model.background import BackgroundConfig
from marea.model.cinematic import CinematicConfig
from marea.render.background import Background
from marea.render.cinematic import CinematicBackground, VideoBackground
from marea.render.text import (BG_PAD_TOP, CREDIT_GAP_TOP, LINE_H, PAD_L, PAD_R, PAD_V, Fonts, ItemLayout, layout_credit)

DESIGN_PX = 42.0
FW = 0.5  # borde del wipe (em)
PROBE_HZ = 10
PROBE_W, PROBE_H = 32, 56  # miniatura del fondo que se promedia bajo cada palabra con efecto (mismo aspecto 9:16)
FW_SOFT = 1.3  # borde del wipe en letra por línea (em): degradado ancho, como Spicy Lyrics sin sílabas
ORIGIN_SYL, ORIGIN_CH = 0.81, 0.75  # transform-origin vertical (em desde el borde superior de la línea)
BLOOM = ((0.28, 0.16), (0.06, 0.22))  # (radius em, alpha) of the two independent halos (outer soft, inner tight); x TUNING.bloom_gain
BLOOM_STEPS = 16  # the bloom fades with L.act in this many cached levels (1/16 of its alpha is invisible)
WHITE = skia.ColorSetARGB(255, 255, 255, 255)


@lru_cache(maxsize=2048)
def _part_char_widths(fonts, text: str, size: float) -> tuple[float, ...]:
    """Cached un-kerned per-character widths for a syllable/word part."""
    return tuple(fonts.text(ch, size)[1] for ch in text)


@dataclass
class Style:
    font_frac: float = 0.066  # tamaño de letra / ancho del lienzo
    bloom: bool = True
    title: str = ""
    artist: str = ""
    font_family: str = ""  # vacío = Inter Tight incluida
    wm_text: str = "Zilla-ZH3"  # marca de agua; vacío = sin marca
    wm_side: str = "left"  # "left" | "right"
    wm_glyph: bool = False  # tres ondas junto al texto
    credit: bool = True  # «Written by… / Provided by…» al final de la letra, como en Spicetify


WM_SIZE = 0.022  # alto de la letra / alto del lienzo
WM_ALPHA, WM_FADE_MS = 0.4, 600
WM_BASELINE = 0.835  # por encima del 15 % inferior que tapa la interfaz de TikTok
WM_RIGHT_MARGIN = 0.17  # fracción de `ref` que se deja libre a la derecha (columna de botones de TikTok)
WM_DODGE = 0.7  # cuánto baja la opacidad al cruzarse con la línea activa


def _argb(a: float, rgb: tuple[int, int, int] = (255, 255, 255)) -> int:
    return skia.ColorSetARGB(round(max(0.0, min(1.0, a)) * 255), rgb[0], rgb[1], rgb[2])


class Scene:
    def __init__(self, lyrics, cover: skia.Image, size=(1080, 1920), params: Params | None = None,
                 style: Style | None = None, gpu: bool = False, cinematic: CinematicConfig | None = None,
                 async_video: bool = False, on_video_frame=None, wordfx=None, wordfx_debug: bool = False,
                 background: BackgroundConfig | None = None):
        """gpu=True si el canvas es de una superficie GPU (`marea.render.gpu`); la portada debe estar en ese contexto.
        wordfx: `marea.wordfx.plan.FxPlan` (experimental) o None; wordfx_debug dibuja la superposición de depuración (solo preview)."""
        self.w, self.h = size
        self.wordfx, self.wordfx_debug = wordfx, wordfx_debug
        self._probe = None  # miniatura del fondo para adaptar el color de las palabras con efecto (se crea al primer uso)
        self.style = style or Style()
        self.cover = cover
        self.fonts = Fonts(self.style.font_family)
        self.bg = Background(cover, self.w, self.h, lowres=1 if gpu else 4)
        self._async_video = async_video
        self._on_video_frame = on_video_frame
        self.cinematic_bg: CinematicBackground | None = None
        self.bgvideo: VideoBackground | None = None
        self.background = BackgroundConfig()
        self._scrim_paint: tuple[float, skia.Paint] | None = None
        self.set_background(background)
        if cinematic is not None:
            self.set_cinematic(cinematic)
        # medidas relativas a `ref` (el ancho en 9:16): sirven igual para 1080x1080 y 1920x1080
        self.ref = min(self.w, 0.8 * self.h)
        self.size = self.style.font_frac * self.ref
        self.k = self.size / DESIGN_PX
        stage_w = min(0.94 * self.w, 1.2 * self.h)  # en horizontal, la letra queda en una columna centrada
        self.stage = skia.Rect.MakeXYWH((self.w - stage_w) / 2, 0.16 * self.h, stage_w, 0.72 * self.h)
        fade = skia.GradientShader.MakeLinear([(0, self.stage.top()), (0, self.stage.bottom())],  # máscara de bordes del prototipo
                                              [_argb(0), _argb(1), _argb(1), _argb(0)], [0, 0.06, 0.90, 1])
        self._stage_fade_paint = skia.Paint(Shader=fade, BlendMode=skia.BlendMode.kDstIn)
        self._sampling_linear = skia.SamplingOptions(skia.FilterMode.kLinear)
        self._p_white = skia.Paint(AntiAlias=True, Color=WHITE)
        self._p_fill = skia.Paint(AntiAlias=True)
        self._p_wipe_fill = skia.Paint(AntiAlias=True)
        self._p_glow = skia.Paint(AntiAlias=True)
        self._p_wipe_glow = skia.Paint(AntiAlias=True)
        self._p_dstin = skia.Paint(BlendMode=skia.BlendMode.kDstIn)
        self._p_layer = skia.Paint(AntiAlias=True)
        self._p_stroke = skia.Paint(
            AntiAlias=True,
            Style=skia.Paint.kStroke_Style,
            StrokeCap=skia.Paint.kRound_Cap,
            StrokeJoin=skia.Paint.kRound_Join,
        )
        self._scratch_rect = skia.Rect.MakeEmpty()
        cs = 0.14 * self.ref
        hx, hy = self.stage.left() + 0.04 * self.ref, 0.06 * self.h
        self._hdr_box = skia.Rect.MakeXYWH(hx, hy, cs, cs)
        self._hdr_rrect = skia.RRect.MakeRectXY(self._hdr_box, cs * 0.14, cs * 0.14)
        self._hdr_geom = (cs, hx + cs + 0.035 * self.ref, hy, self.stage.right() - 0.04 * self.ref - (hx + cs + 0.035 * self.ref))
        self._hdr_cache: tuple | None = None
        self._wm_wave_cache: tuple | None = None
        self._bloom_cache: dict[tuple[float, int], object] = {}
        self._glow_tint: tuple[int, int, int] | None = None
        self._mask_blur_cache: dict[int, object] = {}
        self._blur_filter_cache: dict[int, object] = {}
        self._wfx_lyrics_ref = weakref.ref(lyrics)
        self._wfx_mods: tuple | None = None
        credit = lyrics.credit_lines() if self.style.credit else []
        self._credit_rows, self._credit_size, credit_h = layout_credit(
            credit, self.fonts, self.size, stage_w - PAD_L * self.size - PAD_R * stage_w)
        self.engine = Engine(lyrics, self._measure, self.stage.height(), self.size, params, credit_h=credit_h)
        self._by_line = {id(L.src): L for L in self.engine.items if not (L.is_gap or L.is_credit)}
        if wordfx is not None:
            self.attach_wordfx(lyrics, wordfx)
        self._layouts = {}
        self.clip_start_ms: float | None = None  # inicio del clip: la marca de agua entra con fundido desde ahí

    def set_cinematic(self, cinematic: CinematicConfig | None):
        self.invalidate_probe()
        if cinematic is None:
            if self.cinematic_bg is not None:
                self.cinematic_bg.close()
                self.cinematic_bg = None
            return
        if self.cinematic_bg is None:
            self.cinematic_bg = CinematicBackground(cinematic, self.w, self.h, async_video=self._async_video,
                                                    on_frame_ready=self._on_video_frame)
        else:
            self.cinematic_bg.set_config(cinematic)

    def set_background(self, cfg: BackgroundConfig | None):
        """Fondo del clip: portada, video propio o transparente (solo cambia el dibujo; no reinicia el motor)."""
        self.invalidate_probe()
        self.background = cfg = (cfg or BackgroundConfig()).clamped()
        if cfg.source != "video":
            if self.bgvideo is not None:
                self.bgvideo.close()
                self.bgvideo = None
        elif self.bgvideo is None:
            self.bgvideo = VideoBackground(cfg, self.w, self.h, self.bg, async_video=self._async_video,
                                           on_frame_ready=self._on_video_frame)
        else:
            self.bgvideo.set_config(cfg)

    def invalidate_probe(self):
        if self._probe is not None:
            self._probe[2][0] = None

    def matches_lyrics(self, lyrics) -> bool:
        return self._wfx_lyrics_ref() is lyrics

    def attach_wordfx(self, lyrics, plan):
        """Marca cada palabra del motor con su efecto: la clave (verso, palabra) coincide con el orden de `Item.words`."""
        self.wordfx = plan
        self._wfx_lyrics_ref = weakref.ref(lyrics)
        by_src = {id(l): i for i, l in enumerate(lyrics.lines)}
        has_word_fx = plan is not None and hasattr(plan, "word_fx")
        for L in self._by_line.values():
            li = by_src.get(id(L.src))
            if li is None:
                for w in L.words:
                    w.fx = None
                    w.fx_layer = None
                continue
            for wi, w in enumerate(L.words):
                if plan is None:
                    w.fx = None
                    w.fx_layer = None
                else:
                    w.fx = None if w.bg else plan.get(li, wi)
                    w.fx_layer = plan.word_fx(li, wi, is_bg=w.bg) if has_word_fx else w.fx

    def _get_wfx_mods(self):
        if self._wfx_mods is None:
            from marea.wordfx import debug, render
            from marea.wordfx.fxtypes import CarrierLetter, CarrierWord
            self._wfx_mods = (render, debug, CarrierLetter, CarrierWord)
        return self._wfx_mods

    def _mask_blur(self, sigma: float):
        key = round(sigma * 200.0)
        mf = self._mask_blur_cache.get(key)
        if mf is None:
            if len(self._mask_blur_cache) >= 256:
                self._mask_blur_cache.clear()
            mf = self._mask_blur_cache[key] = skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, key / 200.0)
        return mf

    def _blur_filter(self, sigma: float):
        key = round(sigma * 200.0)
        bf = self._blur_filter_cache.get(key)
        if bf is None:
            if len(self._blur_filter_cache) >= 256:
                self._blur_filter_cache.clear()
            s = key / 200.0
            bf = self._blur_filter_cache[key] = skia.ImageFilters.Blur(s, s)
        return bf

    def _glow_rgb(self) -> tuple[int, int, int]:
        """Halo colour: the cover's dominant colour pulled toward white, so it belongs to the background without looking muddy."""
        if self._glow_tint is None:
            from marea.wordfx.palette import dominant_colors
            img = self.cover.makeRasterImage() if self.cover.isTextureBacked() else self.cover  # a GPU texture cannot be read by the CPU sampler
            dom = dominant_colors(img, 1)
            base = dom[0] if dom else (255, 255, 255)
            mix = TUNING.glow_white_mix
            self._glow_tint = tuple(round(c + (255 - c) * mix) for c in base)
        return self._glow_tint

    def _bloom(self, size: float, act: float):
        """Image filter for the soft bloom of a line, or None. Two independent halos (no shadow of a shadow) around ONLY the sung text:
        the alpha of the line is remapped so unsung text (alpha `da`) contributes nothing and sung text (`ba`) everything. The whole
        bloom scales with `act`, so it fades in and out with the line instead of popping."""
        lvl = round(min(1.0, max(0.0, act)) * BLOOM_STEPS)
        if lvl == 0 or TUNING.bloom_gain <= 0.0:
            return None
        key = (size, lvl)
        f = self._bloom_cache.get(key)
        if f is None:
            if len(self._bloom_cache) >= 256:
                self._bloom_cache.clear()
            k = lvl / BLOOM_STEPS
            dim, unsung = TUNING.dim_alpha, TUNING.unsung_alpha_active
            da, ba = dim + (unsung - dim) * k, dim + (1.0 - dim) * k
            gain = 1.0 / (ba - da)
            only_sung = skia.ImageFilters.ColorFilter(skia.ColorFilters.Matrix(
                [1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, gain, -da * gain]), None)
            outer_rgb = self._glow_rgb()
            inner_rgb = tuple(round(c + (255 - c) * 0.5) for c in outer_rgb)
            halos = [skia.ImageFilters.DropShadowOnly(0, 0, r * size / 2, r * size / 2,
                                                      _argb(min(1.0, a * TUNING.bloom_gain * k), rgb), only_sung)
                     for (r, a), rgb in zip(BLOOM, (outer_rgb, inner_rgb))]
            f = self._bloom_cache[key] = skia.ImageFilters.Merge(halos + [None])
        return f

    def close(self):
        if self.cinematic_bg is not None:
            self.cinematic_bg.close()
        if self.bgvideo is not None:
            self.bgvideo.close()
            self.bgvideo = None

    # ---- layout ----
    def _layout(self, item) -> ItemLayout:
        lay = self._layouts.get(id(item))
        if lay is None:
            lay = self._layouts[id(item)] = ItemLayout(item, self.fonts, self.size, self.stage.width())
        return lay

    # ---- dibujo ----
    def _measure(self, line, active):
        lay = self._layout(self._by_line[id(line)])
        return lay.h_active if active else lay.h_inactive

    def _draw_bg_at(self, canvas: skia.Canvas, base, t_s: float):
        if self.background.transparent:
            return  # sin fondo: tampoco recuerdos cinemáticos (taparían el alfa)
        if self.bgvideo is not None:
            self.bgvideo.clip_start_ms = self.clip_start_ms or 0.0
            base = self.bgvideo
        if self.cinematic_bg is not None:
            self.cinematic_bg.draw(
                canvas,
                base,
                t_s,
                lambda: max((L.act for L in self.engine.items if not (L.is_gap or L.is_credit)), default=0.0),
            )
        else:
            base.draw(canvas, t_s)

    def _draw_bg(self, canvas: skia.Canvas, base):
        self._draw_bg_at(canvas, base, self.engine.t)

    def _backdrop(self, x: float, top: float, w: float, h: float) -> tuple[int, int, int]:
        """Color medio del fondo real (portada o cinemático) bajo una caja en px del lienzo: se dibuja el fondo en una miniatura
        (una vez por fotograma) y se promedia. Función pura de (portada, cinemático, t): preview y export ven lo mismo."""
        t = round(self.engine.t * PROBE_HZ) / PROBE_HZ  # el fondo cambia despacio: se muestrea a 10 Hz (sigue siendo función pura de t)
        if self._probe is None:
            self._probe = (skia.Surface(PROBE_W, PROBE_H), Background(self.cover, PROBE_W, PROBE_H, lowres=1), [None, None, None])
        surf, small, cache = self._probe
        if cache[0] != t:
            c = surf.getCanvas()
            c.save()
            if self.bgvideo is not None or (self.cinematic_bg is not None and self.cinematic_bg.active_at(t * 1000.0)):
                c.scale(PROBE_W / self.w, PROBE_H / self.h)  # el cinemático dibuja en px del lienzo real
                self._draw_bg_at(c, self.bg, t)
            else:
                small.draw(c, t)
            c.restore()
            rgb = surf.makeImageSnapshot().toarray(colorType=skia.kRGBA_8888_ColorType)[..., :3]
            sat = np.zeros((PROBE_H + 1, PROBE_W + 1, 3), dtype=np.int32)
            np.cumsum(np.cumsum(rgb, axis=0, dtype=np.int32), axis=1, out=sat[1:, 1:])
            cache[0], cache[1], cache[2] = t, rgb, sat.ravel().tolist()
        sat = cache[2]
        stride = (PROBE_W + 1) * 3
        x0 = min(int(max(0.0, x) / self.w * PROBE_W), PROBE_W - 1)
        x1 = min(PROBE_W, max(int(min(self.w, x + w) / self.w * PROBE_W) + 1, x0 + 1))
        y0 = min(int(max(0.0, top) / self.h * PROBE_H), PROBE_H - 1)
        y1 = min(PROBE_H, max(int(min(self.h, top + h) / self.h * PROBE_H) + 1, y0 + 1))
        i00 = y0 * stride + x0 * 3
        i01 = y0 * stride + x1 * 3
        i10 = y1 * stride + x0 * 3
        i11 = y1 * stride + x1 * 3
        area = (y1 - y0) * (x1 - x0)
        return (
            int((sat[i11] - sat[i01] - sat[i10] + sat[i00]) / area),
            int((sat[i11 + 1] - sat[i01 + 1] - sat[i10 + 1] + sat[i00 + 1]) / area),
            int((sat[i11 + 2] - sat[i01 + 2] - sat[i10 + 2] + sat[i00 + 2]) / area),
        )

    def render(self, canvas: skia.Canvas):
        e, st = self.engine, self.stage
        if self.background.transparent:
            canvas.clear(skia.ColorTRANSPARENT)  # la superficie GPU se reutiliza entre frames
        elif self.bgvideo is not None:
            canvas.clear(skia.ColorBLACK)  # un video con alfa propio no debe dejar rastro del frame anterior
        self._draw_bg(canvas, self.bg)
        self._scrim(canvas)
        self._header(canvas)
        canvas.save()
        canvas.clipRect(st)
        canvas.saveLayer(st, None)
        canvas.save()
        canvas.translate(st.left(), st.top())
        for L in e.items:
            self._item(canvas, L)
        canvas.restore()
        canvas.drawRect(st, self._stage_fade_paint)
        canvas.restore()
        canvas.restore()
        self._watermark(canvas)

    def _scrim(self, canvas):
        """Velo suave tras el bloque de letra (solo con video propio): más oscuro al centro, se desvanece hacia los bordes."""
        a = self.background.scrim if self.background.source == "video" else 0.0
        if a < 0.003:
            return
        if self._scrim_paint is None or self._scrim_paint[0] != a:
            st, pad = self.stage, 0.10 * self.h
            shader = skia.GradientShader.MakeLinear(
                [(0, st.top() - pad), (0, st.bottom() + pad)],
                [skia.ColorSetARGB(0, 0, 0, 0), skia.ColorSetARGB(round(0.7 * a * 255), 0, 0, 0),
                 skia.ColorSetARGB(round(0.7 * a * 255), 0, 0, 0), skia.ColorSetARGB(0, 0, 0, 0)], [0, 0.3, 0.7, 1])
            self._scrim_paint = (a, skia.Paint(Shader=shader))
        canvas.drawRect(skia.Rect.MakeWH(self.w, self.h), self._scrim_paint[1])

    def _watermark_box(self, sz: float) -> skia.Rect | None:
        st = self.style
        if not st.wm_text.strip():
            return None
        gw = 1.5 * sz if st.wm_glyph else 0.0
        w = gw + (0.5 * sz if gw else 0.0) + self.fonts.text(st.wm_text, sz)[1]
        x = self.w - WM_RIGHT_MARGIN * self.ref - w if st.wm_side == "right" else self.stage.left() + 0.04 * self.ref
        base = WM_BASELINE * self.h
        return skia.Rect.MakeXYWH(x, base - sz, w, 1.25 * sz)

    def _wm_dodge(self, box: skia.Rect, sz: float) -> float:
        """0..1: cuánto se cruza la línea cantada con la marca (función continua del estado del motor, sin memoria)."""
        worst, m = 0.0, 0.5 * sz
        for L in self.engine.items:
            if L.is_gap or L.act < 0.05:
                continue
            lay = self._layout(L)
            top = self.stage.top() + L.y.p
            v = (min(top + L.h, box.bottom() + m) - max(top, box.top() - m)) / (box.height() + 2 * m)
            tw = max(lay.main_w or lay.bg_w, default=0.0)
            x1 = self.stage.left() + (lay.width - lay.pad_r if L.duet else lay.pad_l + tw)
            hx = (min(x1, box.right() + m) - max(x1 - tw, box.left() - m)) / (0.5 * box.width())
            worst = max(worst, L.act * min(1.0, max(0.0, v)) * min(1.0, max(0.0, hx)))
        return worst

    def _watermark(self, canvas):
        sz = WM_SIZE * self.h
        box = self._watermark_box(sz)
        if box is None:
            return
        fade = 1.0 if self.clip_start_ms is None else (self.engine.t * 1000 - self.clip_start_ms) / WM_FADE_MS
        alpha = WM_ALPHA * min(1.0, max(0.0, fade)) * (1 - WM_DODGE * self._wm_dodge(box, sz))
        if alpha < 0.003:
            return
        blob, tw = self.fonts.text(self.style.wm_text, sz)
        tx = box.left() + (box.width() - tw)
        base = box.top() + sz  # línea base
        if not self.background.transparent:  # sobre transparencia una sombra negra se vería como un halo oscuro
            shadow = self._p_glow
            shadow.setColor(skia.ColorSetARGB(round(alpha * 0.9 * 255), 0, 0, 0))
            shadow.setMaskFilter(self._mask_blur(0.25 * sz))
            canvas.drawTextBlob(blob, tx, base + 0.06 * sz, shadow)
        fill = self._p_fill
        fill.setColor(_argb(alpha))
        canvas.drawTextBlob(blob, tx, base, fill)
        if self.style.wm_glyph:
            self._wm_waves(canvas, box.left(), base - 0.35 * sz, 1.5 * sz, sz, alpha)

    def _wm_waves(self, canvas, x, cy, w, sz, alpha):
        """Tres ondas apiladas (la central más gruesa), como el logo."""
        key = (x, cy, w, sz)
        cached = self._wm_wave_cache
        if cached is None or cached[0] != key:
            items = []
            for i, (dy, thick) in enumerate(((-0.34, 0.07), (0.0, 0.12), (0.34, 0.07))):
                path = skia.Path()
                for s in range(17):
                    px = x + w * s / 16
                    py = cy + dy * sz + 0.1 * sz * math.sin((s / 16 * 2 + i * 0.3) * math.pi)
                    path.moveTo(px, py) if s == 0 else path.lineTo(px, py)
                items.append((path, thick * sz))
            cached = (key, tuple(items))
            self._wm_wave_cache = cached
        p = self._p_stroke
        p.setColor(_argb(alpha))
        for path, sw in cached[1]:
            p.setStrokeWidth(sw)
            canvas.drawPath(path, p)

    def snapshot(self, t_ms: float) -> skia.Image:
        """Frame estático en t_ms con el motor ya asentado (3 s de pre-roll). Úsese con una escena nueva."""
        self.engine.warmup(t_ms)
        s = skia.Surface(self.w, self.h)
        self.render(s.getCanvas())
        return s.makeImageSnapshot()

    def _header(self, canvas):
        st = self.style
        canvas.save()
        canvas.clipRRect(self._hdr_rrect, doAntiAlias=True)
        canvas.drawImageRect(self.cover, self._hdr_box, self._sampling_linear)
        canvas.restore()
        key = (st.title, st.artist)
        cached = self._hdr_cache
        if cached is None or cached[0] != key:
            cs, tx, y, avail = self._hdr_geom
            R = self.ref
            entries = []
            for text, size, base, alpha, bold in ((st.title, 0.04 * R, 0.45, 1.0, True), (st.artist, 0.032 * R, 0.80, 0.62, False)):
                if text:
                    font = self.fonts.font(size, bold)
                    while font.measureText(text) > avail and len(text) > 1:
                        text = text[:-2] + "…"
                    entries.append((text, tx, y + cs * base, font, skia.Paint(AntiAlias=True, Color=WHITE, Alphaf=alpha)))
            cached = (key, tuple(entries))
            self._hdr_cache = cached
        for text, tx, ty, font, paint in cached[1]:
            canvas.drawString(text, tx, ty, font, paint)

    def _item(self, canvas, L):
        st_w, k = self.stage.width(), self.k
        if L.is_gap and L.grp_op < 0.001:
            return
        sigma = L.blur * k
        m = 0.6 * self.size + 3 * sigma
        if L.y.p + L.h < -m or L.y.p > self.stage.height() + m:
            return
        canvas.save()
        canvas.translate(0, L.y.p)
        ox = st_w if L.duet and not L.is_gap else 0.0  # transform-origin: right/left center
        canvas.translate(ox, L.h / 2)
        canvas.scale(L.s.p, L.s.p)
        canvas.translate(-ox, -L.h / 2)
        layer = L.opacity < 0.999 or sigma > 0.01
        if layer:
            self._scratch_rect.setLTRB(-m, -m, st_w + m, L.h + m)
            lp = self._p_layer
            lp.setAlphaf(L.opacity)
            lp.setImageFilter(self._blur_filter(sigma) if sigma > 0.01 else None)
            canvas.saveLayer(self._scratch_rect, lp)
        if L.is_gap:
            self._gap(canvas, L)
        elif L.is_credit:
            self._credit(canvas, L)
        else:
            self._line(canvas, L, self._layout(L))
        if layer:
            canvas.restore()
        canvas.restore()

    def _credit(self, canvas, L):
        """«Written by A, B» y «Provided by Apple Music»: el prefijo tenue y el nombre más claro, en pequeño."""
        csize, size = self._credit_size, self.size
        y = CREDIT_GAP_TOP * size
        p = self._p_fill
        c_bold, c_reg = _argb(0.62), _argb(0.38)
        for rows in self._credit_rows:
            for row in rows:
                base = y + self.fonts.baseline(csize)
                for word, x, bold in row:
                    blob, _ = self.fonts.text(word, csize, bold)
                    p.setColor(c_bold if bold else c_reg)
                    canvas.drawTextBlob(blob, PAD_L * size + x, base, p)
                y += LINE_H * csize
            y += 0.35 * csize

    def _gap(self, canvas, L):
        size, w = self.size, self.stage.width()
        d, gap = 0.32 * size, 0.24 * size
        gw = 3 * d + 2 * gap
        left = w - 0.15 * w - gw if L.duet else size  # con dueto siguiente, a la derecha
        cy = L.h / 2
        dot_rgb = (self.wordfx.interlude_color(self.engine.t)
                   if (self.wordfx is not None and self.wordfx.intensity > 0.0) else (255, 255, 255))
        canvas.save()
        canvas.translate(left + gw / 2, cy)
        canvas.scale(L.grp_sc, L.grp_sc)
        canvas.translate(-gw / 2, 0)
        p_glow, p_fill = self._p_glow, self._p_fill
        for i, dot in enumerate(L.dots):
            canvas.save()
            canvas.translate(i * (d + gap) + d / 2, dot.y * size)
            canvas.scale(dot.scale, dot.scale)
            if dot.halo_a > 0:
                p_glow.setColor(_argb(dot.halo_a * dot.op * L.grp_op, dot_rgb))
                p_glow.setMaskFilter(self._mask_blur(dot.halo_r * size / 2))
                canvas.drawCircle(0, 0, d / 2, p_glow)
            p_fill.setColor(_argb(dot.op * L.grp_op, dot_rgb))
            canvas.drawCircle(0, 0, d / 2, p_fill)
            canvas.restore()
        canvas.restore()

    def _line(self, canvas, L, lay):
        size = lay.size
        dim, unsung = TUNING.dim_alpha, TUNING.unsung_alpha_active
        ba, da = dim + (1.0 - dim) * L.act, dim + (unsung - dim) * L.act
        bloom = self._bloom(size, L.act) if self.style.bloom else None
        m = BLOOM[0][0] * size * 2
        top = PAD_V * size
        lp = self._p_layer
        if bloom:
            self._scratch_rect.setLTRB(-m, -m, lay.width + m, L.h + m)
            lp.setAlphaf(1.0)
            lp.setImageFilter(bloom)
            canvas.saveLayer(self._scratch_rect, lp)
        self._block(canvas, L, lay, lay.main, lay.main_w, size, top, ba, da)
        if bloom:
            canvas.restore()
        if lay.bg and L.bg_op > 0.003:
            box_top = top + lay.main_h
            ox = lay.width - lay.pad_r if L.duet else lay.pad_l
            canvas.save()
            canvas.translate(ox, box_top - 0.8 * lay.bg_h * (1 - L.bg_p))  # entra deslizándose desde -80 %
            canvas.scale(0.8 + 0.2 * L.bg_p, 0.8 + 0.2 * L.bg_p)
            canvas.translate(-ox, -box_top)
            self._scratch_rect.setLTRB(-m, box_top - m, lay.width + m, box_top + lay.bg_h + m)
            lp.setAlphaf(0.4 * L.bg_op)
            lp.setImageFilter(bloom)
            canvas.saveLayer(self._scratch_rect, lp)
            self._block(canvas, L, lay, lay.bg, lay.bg_w, lay.bg_size, box_top + BG_PAD_TOP * lay.bg_size, ba, da)
            canvas.restore()
            canvas.restore()

    def _block(self, canvas, L, lay, rows, widths, size, top, ba, da):
        fx_active = self.wordfx is not None and self.wordfx.intensity > 0.0
        t_now = self.engine.t
        strain_now = self.wordfx.strain_at(t_now) if fx_active else 0.0
        for ri, (row, rw) in enumerate(zip(rows, widths)):
            x0 = lay.width - lay.pad_r - rw if L.duet else lay.pad_l  # dueto: alineado a la derecha
            for word, wx, ww in row:
                lt = top + ri * LINE_H * size
                fx_item = word.fx if word.fx is not None else getattr(word, "fx_layer", None)
                if not fx_active or fx_item is None:
                    self._word(canvas, word, x0 + wx, lt, ww, size, ba, da)
                    continue
                p_fx = fx_item.params
                anti_s = TUNING.anticipation_s if p_fx.is_hero else 0.0  # same value the envelope uses to leave "idle"
                if (
                    word.p == 0.0
                    and not self.wordfx_debug
                    and t_now <= fx_item.match.begin_ms / 1000.0 - anti_s - 0.05
                    and p_fx.opacity_min >= 1.0
                    and p_fx.blur_em <= 0.0
                    and p_fx.grain <= 0.0
                    and p_fx.particles <= 0.0
                    and p_fx.sparkle <= 0.0
                    and p_fx.vanish_s <= 0.0
                    and (strain_now <= 0.0 or p_fx.strain_gain <= 0.0)
                    and (p_fx.bleed_donor_s < 0.0 or t_now <= p_fx.bleed_donor_s - 0.17)
                ):
                    # Completely idle pre-onset word: identical to base draw without layer/envelope allocations.
                    self._word(canvas, word, x0 + wx, lt, ww, size, ba, da)
                    continue
                self._fx_word(canvas, L, word, x0 + wx, lt, ww, size, ba, da, fx_item=fx_item, strain=strain_now)
                if self.wordfx_debug and word.fx is not None:
                    _, debug_mod, _, _ = self._get_wfx_mods()
                    debug_mod.draw_label(canvas, self.fonts, word.fx, x0 + wx, ww, lt + self.fonts.baseline(size), size)

    def _carrier_word(self, L, word, size: float):
        """Read-only snapshot of base renderer carrier state for `word` and its parent line `L`."""
        _, _, CarrierLetter, CarrierWord = self._get_wfx_mods()
        c_letters = []
        filled = 0.0
        total_w = 0.0
        for u in word.parts:
            pw = self.fonts.text(u.text, size)[1]
            filled += pw * u.p
            total_w += pw
            k = max(1, len(u.text))
            if word.emph:
                dy = u.y + (u.wdy[0] if u.wdy else 0.0)
                sc = u.scale * (1.0 + (u.wds[0] if u.wds else 0.0))
                c_letters.append(CarrierLetter(u.p, u.b, dy, sc, u.glow_a, u.glow_r))
            else:
                front_px = u.p * pw
                cx = 0.0
                grow = (u.scale - 1.0) / size  # base renderer scales the whole syllable about its centre: each letter drifts by this share of its offset
                for j, cw in enumerate(_part_char_widths(self.fonts, u.text, size)):
                    lp = min(1.0, max(0.0, (front_px - cx) / max(1.0, cw)))
                    dy = u.y + (u.wdy[j] if u.wdy else 0.0)
                    sc = u.scale * (1.0 + (u.wds[j] if u.wds else 0.0))
                    c_letters.append(CarrierLetter(lp, u.b + (u.e - u.b) * j / k, dy, sc, u.glow_a, u.glow_r,
                                                   (cx + cw / 2 - pw / 2) * grow + u.x))
                    cx += cw
        front_u = min(1.0, max(0.0, filled / total_w)) if total_w > 0.0 else word.p
        return CarrierWord(tuple(c_letters), front_u, L.act, L.on, L.opacity, L.blur * self.k, L.start, L.end, word.bg), front_u

    def _fx_word(self, canvas, L, word, x, line_top, ww, size, ba, da, fx_item=None, strain: float | None = None):
        """Palabra con efecto semántico (marea.wordfx): el render dibuja las letras con sus efectos y aquí se aplica el mismo
        wipe de la palabra con énfasis, así que el relleno cantado/no cantado se ve igual que en el resto de la línea."""
        render, _, _, _ = self._get_wfx_mods()
        fx_use = fx_item if fx_item is not None else word.fx
        p_fx = fx_use.params
        pad = TUNING.glow_pad_em * size  # a layer smaller than the widest halo clips it into a visible box
        bounds = skia.Rect.MakeLTRB(x - pad, line_top - pad, x + ww + pad, line_top + LINE_H * size + pad)
        carrier, front_u = self._carrier_word(L, word, size)
        t_now = self.engine.t
        strain_val = self.wordfx.strain_at(t_now) if strain is None else strain
        oy = line_top + (ORIGIN_CH if word.emph else ORIGIN_SYL) * size
        backdrop = (self._backdrop(x + self.stage.left(), line_top + self.stage.top(), ww, LINE_H * size)
                    if self.wordfx.adapt_bg and not self.background.transparent else None)
        p_adapted = render.adapt_params(p_fx, backdrop)
        dark_factor = render._backdrop_dark_factor(backdrop)
        letters = render._letters_cached(self.fonts, word.text, size)
        onsets = [cl.onset_s for cl in carrier.letters]
        rms_now = self.wordfx.rms_at(t_now)
        base = line_top + self.fonts.baseline(size)
        simple_wipe = (
            (front_u <= 0.0 or front_u >= 1.0)
            and p_fx.grain <= 0.0
            and p_fx.blur_em <= 0.0
            and p_fx.fracture <= 0.0
            and abs(p_fx.chromatic_em) == 0.0
            and p_fx.trail <= 0.0
            and (strain_val <= 0.0 or p_fx.strain_gain <= 0.0)
        )
        if simple_wipe:
            wipe_a = ba if front_u >= 1.0 else da
            render.draw_word_fx(canvas, self.fonts, fx_use, letters, x, base, size, t_now, rms_now,
                                self.engine.p.reduce, strain=strain_val, onsets=onsets,
                                backdrop=backdrop, carrier=carrier, origin_y=oy, wipe_alpha=wipe_a,
                                include_particles=False, adapted_params=p_adapted, dark_factor=dark_factor)
        else:
            canvas.saveLayer(bounds, None)
            render.draw_word_fx(canvas, self.fonts, fx_use, letters, x, base, size, t_now, rms_now,
                                self.engine.p.reduce, strain=strain_val, onsets=onsets,
                                backdrop=backdrop, carrier=carrier, origin_y=oy,
                                include_particles=False, adapted_params=p_adapted, dark_factor=dark_factor)
            fw = FW * max(0.35, min(1.8, p_fx.fill_edge_softness))
            self._p_dstin.setShader(self._wipe(x, ww, front_u, ba, da, size, fw=fw))
            canvas.drawRect(bounds, self._p_dstin)
            canvas.restore()
        p_wipe_a = da + (ba - da) * min(1.0, max(0.0, front_u))
        if p_fx.streak > 0.0 and not self.engine.p.reduce:  # overlay light, outside the wipe mask; thickness follows the output size
            render.draw_word_streak(canvas, fx_use, x, base, ww, size, t_now, front_u, res_scale=self.ref / 1080.0,
                                    wipe_alpha=p_wipe_a, adapted_params=p_adapted, dark_factor=dark_factor)
        if p_fx.particles > 0 or p_fx.sparkle > 0:
            render.draw_word_particles(canvas, fx_use, letters, x, base, size, t_now, self.engine.p.reduce,
                                       onsets=onsets, backdrop=backdrop, carrier=carrier, wipe_alpha=p_wipe_a,
                                       adapted_params=p_adapted, dark_factor=dark_factor)

    def _wipe(self, x, w, p, ba, da, size, a=1.0, fw=FW, rgb=(255, 255, 255)):
        """Máscara del prototipo: gradiente horizontal de alpha ba→da con borde de `fw` em que avanza con p."""
        x1 = x + p * (w + fw * size) - fw * size
        return skia.GradientShader.MakeLinear([(x1, 0), (x1 + fw * size, 0)], [_argb(a * ba, rgb), _argb(a * da, rgb)])

    def _word(self, canvas, word, x, line_top, ww, size, ba, da):
        base = line_top + self.fonts.baseline(size)
        p_fill, p_wipe_fill = self._p_fill, self._p_wipe_fill
        p_glow, p_wipe_glow = self._p_glow, self._p_wipe_glow
        tint = self._glow_rgb()
        if not word.emph:
            fw = FW_SOFT if word.soft else FW
            for u in word.parts:
                blob, pw = self.fonts.text(u.text, size)
                if u.p <= 0.0:
                    p_fill.setColor(_argb(da))
                    text = p_fill
                    glow = None  # nothing sung yet, nothing glows
                elif u.p >= 1.0:
                    p_fill.setColor(_argb(ba))
                    text = p_fill
                    if u.glow_a > 0:
                        p_glow.setColor(_argb(u.glow_a * ba, tint))
                        p_glow.setMaskFilter(self._mask_blur(u.glow_r * self.k / 2))
                        glow = p_glow
                    else:
                        glow = None
                else:
                    p_wipe_fill.setShader(self._wipe(x, pw, u.p, ba, da, size, fw=fw))
                    text = p_wipe_fill
                    if u.glow_a > 0:
                        p_wipe_glow.setShader(self._wipe(x, pw, u.p, ba, 0.0, size, u.glow_a, fw, tint))  # unsung side of the syllable: no halo
                        p_wipe_glow.setMaskFilter(self._mask_blur(u.glow_r * self.k / 2))
                        glow = p_wipe_glow
                    else:
                        glow = None
                self._glyphs(canvas, u, blob, x, base, pw, size, line_top + ORIGIN_SYL * size, text, glow)
                x += pw
            return
        # palabra con énfasis: una sola máscara para la palabra, las letras se animan dentro
        pad = TUNING.glow_pad_em * size
        bounds = self._scratch_rect
        bounds.setLTRB(x - pad, line_top - pad, x + ww + pad, line_top + LINE_H * size + pad)
        canvas.saveLayer(bounds, None)
        cx, filled = x, 0.0  # filled: px de la palabra ya rellenos; cada letra aporta su ancho real, no una fracción igual
        for c in word.parts:
            blob, pw = self.fonts.text(c.text, size)
            if c.glow_a > 0:
                p_glow.setColor(_argb(c.glow_a, tint))
                p_glow.setMaskFilter(self._mask_blur(c.glow_r * self.k / 2))
                glow = p_glow
            else:
                glow = None
            self._glyphs(canvas, c, blob, cx, base, pw, size, line_top + ORIGIN_CH * size,
                         self._p_white, glow)
            cx += pw
            filled += pw * c.p
        self._p_dstin.setShader(self._wipe(x, ww, filled / ww if ww else 0.0, ba, da, size))
        canvas.drawRect(bounds, self._p_dstin)
        canvas.restore()

    def _glyphs(self, canvas, u, blob, x, base, pw, size, oy, text, glow):
        ox = x + pw / 2
        canvas.save()
        canvas.translate(ox + u.x * size, oy + u.y * size)
        canvas.scale(u.scale, u.scale)
        canvas.translate(-ox, -oy)
        if u.wdy is None:
            if glow:
                canvas.drawTextBlob(blob, x, base, glow)
            canvas.drawTextBlob(blob, x, base, text)
        else:  # capa por carácter (motion): cada letra sube / crece sobre su propio centro, dentro del movimiento de la sílaba
            cx = x  # ponytail: se parte por code point; marcas combinantes se separarían de su base (agrupar si aparece)
            for ch, dy, ds in zip(u.text, u.wdy, u.wds):
                cblob, cw = self.fonts.text(ch, size)  # sin kerning: los anchos suman lo mismo que el bloque de la sílaba
                canvas.save()
                canvas.translate(cx + cw / 2, oy + dy * size)
                canvas.scale(1 + ds, 1 + ds)
                canvas.translate(-(cx + cw / 2), -oy)
                if glow:
                    canvas.drawTextBlob(cblob, cx, base, glow)
                canvas.drawTextBlob(cblob, cx, base, text)
                canvas.restore()
                cx += cw
        canvas.restore()
