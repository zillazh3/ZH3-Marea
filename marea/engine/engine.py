"""Estado de animación puro (sin dibujo): port de `relayout`/`renderLine`/`frame` del prototipo.

Tiempo interno `t` en segundos, `now` en ms (para los retrasos del stagger). Se avanza con `advance(dt)`
a paso fijo, así que dos corridas iguales producen estados idénticos. El render lee `engine.items`.
Las unidades de longitud son px de diseño (letra de 42 px) o em, según el campo.
"""
import math
from dataclasses import dataclass
from typing import Callable

from marea.engine import curves, motion
from marea.engine.curves import GLOW, L_SCALE, L_YOFF, SCALE, WIPE, YOFF, clamp01, smoothstep
from marea.engine.interlude import HIDDEN, interlude_snap, plan_interlude
from marea.engine.layout import INACTIVE_SCALE, blur_level, pos_y_policy
from marea.engine.springs import Spr, Spring
from marea.model.lyrics import Interlude, Line, Lyrics, Syllable

LOOKAHEAD = 0.05  # s: una línea cuenta como activa 50 ms antes de empezar
# Sílabas sostenidas (notas largas): de SUSTAIN_FROM a SUSTAIN_FULL s van de «normal» a «máximo énfasis»; a tope suben
# SUSTAIN_LIFT× más, crecen SUSTAIN_SCALE× más sobre 1.0 y brillan SUSTAIN_GLOW más. Las breves se quedan como estaban.
SUSTAIN_FROM, SUSTAIN_FULL = 0.45, 1.4
SUSTAIN_SCALE, SUSTAIN_LIFT, SUSTAIN_GLOW = 1.0, 3.0, 0.6
MIN_PLAN_MS = 1910  # duración mínima con la que plan_interlude arma la animación completa (entrada + relleno + salida de 1 s)
APPROX_OVERLAP = 0.35  # letra por línea: cada palabra empieza a llenarse este tanto de su duración antes de que acabe la anterior


@dataclass
class Params:
    align: float = 0.35  # ancla vertical de la línea activa (fracción del alto)
    stagger: float = 50.0  # ms por línea visible
    blurm: float = 1.0
    float: bool = True  # springs por sílaba
    emph: bool = True  # letras en palabras largas
    scale: bool = True  # escala 0.97 en líneas inactivas
    vib: bool = True  # micro-vibrato
    wob: float = 0.4  # ratio de amortiguación del spring Y (rebote)
    motion: str = "auto"  # capa de movimiento por carácter (marea.engine.motion): auto | onda | rebote | respira | suave | clasico
    motion_amt: float = 1.0  # expresividad de esa capa (1.0 = normal)
    reduce: bool = False  # movimiento reducido: sin capa por carácter, sin saltos por sílaba/letra ni vibrato (quedan wipe, brillo y scroll)


class Tween:
    """Transición CSS: desde el valor actual hacia la meta, con curva y duración fijas."""

    def __init__(self, v, dur_ms, ease):
        self.t = self.v0 = v
        self.t0 = 0.0
        self.dur = dur_ms
        self.ease = ease

    def to(self, target, now):
        if target != self.t:
            self.v0 = self.value(now)
            self.t, self.t0 = target, now

    def snap(self, v):
        self.t = self.v0 = v

    def value(self, now):
        x = clamp01((now - self.t0) / self.dur)
        return self.v0 + (self.t - self.v0) * self.ease(x)


class Part:
    """Sílaba o letra con sus tres springs; los campos de salida los llena el motor en cada frame."""

    def __init__(self, text, b, e, letter=False, phase=0.0):
        self.text, self.b, self.e, self.phase = text, b, e, phase
        self.S = Spr(L_SCALE(0) if letter else SCALE(0), 0.88, 0.64)
        self.Y = Spr(0.01, 1.45, 0.4)
        self.G = Spr(0, 1.18, 0.56)
        self.p = 0.0  # progreso del wipe de la sílaba, o de la letra en palabras con énfasis
        self.sb, self.se, self.j, self.k = b, e, 0, 1  # letra: inicio y fin de su sílaba, su posición en ella y cuántas letras tiene
        self.sus = 0.0  # 0..1: qué tan sostenida es la sílaba (solo sílabas normales)
        self.wdy = self.wds = None  # capa por carácter (motion): dy en em y ds en escala de cada carácter, None = en reposo
        self.scale, self.y, self.x = 1.0, 0.0, 0.0  # y, x en em
        self.glow_r, self.glow_a = 0.0, 0.0  # halo: radio px y alpha (0 = sin halo)


class Word:
    def __init__(self, parts, bg, b, e, emph, last, soft=False):
        self.parts, self.bg, self.b, self.e, self.emph, self.last = parts, bg, b, e, emph, last
        self.soft = soft  # letra por línea: borde del wipe más ancho
        self.text = "".join(p.text for p in parts)
        self.p = 0.0  # progreso del wipe de toda la palabra (emph: promedio de sus letras)


class Dot:
    def __init__(self):
        self.sp = Spr(0.5, 1.6, 0.5)
        self.yp = Spr(0, 2.1, 0.32)
        self.prev = 0.0
        self.op, self.scale, self.y = 0.0, 1.0, 0.0
        self.halo_r, self.halo_a = 0.0, 0.0  # em, alpha


class Item:
    """Una línea de letra o un interludio."""

    def __init__(self, kind, index, src, start, end):
        self.kind, self.index, self.src, self.start, self.end = kind, index, src, start, end
        self.y = Spring(0)
        self.s = Spring(INACTIVE_SCALE)
        self.s.k, self.s.c = 100.0, 10.0
        self.h = 0.0
        self.on = False
        self.duet = False
        self.speed = 1.0  # interludio: factor de velocidad de su animación (>1 en intros más cortas que el plan mínimo)
        self.motion = ""  # perfil automático de la capa por carácter (motion.pick); vacío en interludios y crédito
        self.approx = False  # letra por línea (tiempos repartidos): barrido continuo en vez de animar palabra por palabra
        self.words: list[Word] = []
        self.dots: list[Dot] = []
        self.plan = None
        self.phase = "oculto"
        self.grp_op, self.grp_sc = 0.0, 1.0
        self.blur_tw = Tween(0.0, 400, curves.EASE)
        self.op_tw = Tween(1.0, 400, curves.EASE)
        self.act_tw = Tween(0.0, 450, curves.EASE_OUT)  # crossfade del brillo (--ba/--da)
        self.bgp_tw = Tween(0.0, 550, curves.bezier(0.2, 0.9, 0.25, 1))  # entrada de la voz de fondo
        self.bgo_tw = Tween(0.0, 300, curves.EASE)
        self.blur = self.opacity = self.act = self.bg_p = self.bg_op = 0.0  # valores ya en transición

    @property
    def is_gap(self):
        return self.kind == "gap"

    @property
    def is_credit(self):
        return self.kind == "credit"


def _phase(*idx):
    """Fase del vibrato: determinista (el prototipo usa Math.random)."""
    return (idx[0] * 73856093 ^ idx[1] * 19349663 ^ idx[2] * 83492791) % 1000 / 1000 * 6.28


def _words(syllables: list[Syllable], bg: bool, line_idx: int, approx: bool = False) -> list[Word]:
    """approx: tiempos repartidos a partir de una línea (no hay sílabas reales): sin énfasis por letras y con barrido continuo."""
    groups, cur = [], []
    for s in syllables:
        cur.append(s)
        if s.word_end:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    words = []
    for wi, g in enumerate(groups):
        text = "".join(s.text for s in g)
        wb, we = g[0].begin / 1000, g[-1].end / 1000
        last = wi == len(groups) - 1
        clean = [c for c in text if c.isalnum()]
        if not approx and we - wb >= 1.0 and 1 < len(clean) <= 7:  # énfasis por letras
            parts = []
            for s in g:  # las letras se reparten dentro de SU sílaba: la pausa entre sílabas ("si… ento") queda sin letras activas
                sb, se, k = s.begin / 1000, s.end / 1000, max(1, len(s.text))
                for j, ch in enumerate(s.text):
                    c = Part(ch, sb + (se - sb) * j / k, sb + (se - sb) * (j + 1) / k, True, _phase(line_idx, wi + 97 * bg, len(parts)))
                    c.sb, c.se, c.j, c.k = sb, se, j, k
                    parts.append(c)
            words.append(Word(parts, bg, wb, we, True, last))
            continue
        parts = [Part(s.text, s.begin / 1000, s.end / 1000) for s in g]
        for u in parts:
            u.sus = smoothstep((u.e - u.b - SUSTAIN_FROM) / (SUSTAIN_FULL - SUSTAIN_FROM))
        if approx:  # cada palabra se solapa con sus vecinas: el relleno se lee como un solo barrido por la línea
            ov = APPROX_OVERLAP * (we - wb)
            if wi:
                wb, parts[0].b = wb - ov, parts[0].b - ov
            if not last:
                we, parts[-1].e = we + ov, parts[-1].e + ov
        words.append(Word(parts, bg, wb, we, False, last, approx))
    return words


def _prog(t, b, e):
    return clamp01((t - b) / (e - b)) if e > b else float(t >= b)


class Engine:
    def __init__(self, lyrics: Lyrics, measure: Callable[[Line, bool], float], stage_h: float,
                 font_px: float = 42.0, params: Params | None = None, now: float = 0.0, credit_h: float = 0.0):
        """measure(line, activa) -> alto en px de la línea (la activa crece si tiene voz de fondo).
        credit_h > 0 agrega al final el crédito de la letra («Written by…», «Provided by…») con ese alto: un elemento más
        de la lista que sube con ella, nunca la línea activa."""
        self.p = params or Params()
        self.measure, self.stage_h, self.font_px, self.credit_h = measure, stage_h, font_px, credit_h
        self.t, self.now = 0.0, now
        self.active, self.seek_flag, self.config_flag = -1, True, False
        self.live = frozenset()  # índices que se pintan como activos: la línea activa + anteriores cuyo fin (voz de fondo incluida) no llega
        self.last_policy = None
        entries = [(l.start, 1, l) for l in lyrics.lines] + [(i.start, 0, i) for i in lyrics.interludes]
        self.items = []
        for idx, (_, _, src) in enumerate(sorted(entries, key=lambda e: e[:2])):
            self.items.append(self._build(idx, src))
        if credit_h > 0 and self.items:
            end = self.items[-1].end
            self.items.append(Item("credit", len(self.items), None, end, end))
        for i, L in enumerate(self.items):
            if L.is_gap and i + 1 < len(self.items):
                L.duet = self.items[i + 1].duet  # los puntos van a la derecha si sigue un dueto

    def _build(self, idx, src):
        if isinstance(src, Interlude):
            L = Item("gap", idx, src, src.start / 1000, src.end / 1000)
            L.dots = [Dot(), Dot(), Dot()]
            # la línea siguiente se activa LOOKAHEAD antes de su inicio y el interludio colapsa en ese instante: el plan debe
            # terminar ahí, si no los puntos se cortan a media salida
            total = (L.end - L.start - LOOKAHEAD) * 1000
            plan_ms = max(total, MIN_PLAN_MS) if idx == 0 else total
            L.speed = plan_ms / total if total > 0 else 1.0  # intro corta: la animación completa se acelera para que quepa
            L.plan = plan_interlude(plan_ms, 0 if idx == 0 else 500)
            return L
        L = Item("line", idx, src, src.start / 1000, src.end / 1000)
        L.duet = src.agent == "v2"
        L.approx = src.approx
        L.motion = motion.pick(src)
        L.words = _words(src.syllables, False, idx, src.approx)
        for b in src.bg:
            L.words += _words(b.syllables, True, idx, src.approx)
        return L

    # ---- control del tiempo ----
    def start(self, t_ms):
        self.t, self.active, self.seek_flag = t_ms / 1000, -1, True

    def seek(self, t_ms):
        self.t, self.seek_flag = t_ms / 1000, True

    def reconfigure(self):
        """Los parámetros (blur, escala…) cambiaron: recalcula el layout en el próximo frame."""
        self.config_flag = True

    def advance(self, dt, running=True):
        """Un paso fijo. Con running=False (pausa) el tiempo de la canción se detiene pero los springs siguen."""
        if running:
            self.t += dt
        self.now += dt * 1000.0
        self._frame(dt)

    def warmup(self, t_ms, dt=1 / 60, preroll_ms=3000):
        """Simula desde `preroll_ms` antes hasta `t_ms` para que los springs lleguen asentados."""
        from_ms = max(0, t_ms - preroll_ms)
        self.start(from_ms)
        for _ in range(round((t_ms - from_ms) / 1000 / dt)):
            self.advance(dt)
        self.t = t_ms / 1000  # los pasos son enteros: alinea el tiempo exacto del primer frame

    # ---- layout ----
    def _find_active(self):
        a = 0
        for i, L in enumerate(self.items):
            if L.start <= self.t + LOOKAHEAD and not L.is_credit:
                a = i
        return a

    def _find_live(self, a):
        """El scroll sigue a `a`, pero una línea anterior cuya voz de fondo aún canta (Line.end la incluye) sigue viva."""
        # ponytail: solo mira 3 líneas atrás; subir si hay x-bg que se extienda más allá
        return frozenset([a] + [i for i in range(max(0, a - 3), a) if self.items[i].kind == "line" and self.t < self.items[i].end and self.items[i].end > self.items[a].start])

    def _relayout(self, reason, now):
        P, items, active = self.p, self.items, self.active
        seek_like = reason != "tick"
        for i, L in enumerate(items):
            L.on = i in self.live
            L.h = ((1.1 * self.font_px if L.on else 0.0) if L.is_gap else
                   self.credit_h if L.is_credit else self.measure(L.src, L.on))
        a = items[active]
        tops, cum = [], 0.0
        for L in items:
            tops.append(cum)
            cum += L.h
        offset = self.stage_h * P.align - a.h / 2 - tops[active]

        interval = None
        if active > 0 and not a.is_gap:
            interval = (a.start - items[active - 1].start) * 1000
        k, c = pos_y_policy(reason == "seek", a.is_gap, interval)
        self.last_policy = (k, c, reason)

        delay, base = 0.0, 0.0 if seek_like else P.stagger
        for i, L in enumerate(items):
            y = offset + tops[i]
            L.y.k, L.y.c = k, c
            if reason == "init":
                L.y.snap(y)
            else:
                L.y.to(y, delay, now)
            if y + L.h >= 0:
                delay += base
                if i >= active:
                    base /= 1.05
            plain = L.is_gap or L.is_credit  # ni escala 0.97, ni blur, ni la opacidad de línea inactiva
            on = L.on
            L.s.to(1.0 if on or not P.scale or plain else INACTIVE_SCALE, 0, now)
            if reason == "init":
                L.s.snap(L.s.t)
            blur = 0.0 if plain or on else blur_level(i, active, P.blurm)
            for tw, target in ((L.blur_tw, blur if blur > 0.01 else 0.0),
                               (L.op_tw, 1.0 if plain or not on else 0.85),
                               (L.act_tw, 1.0 if on else 0.0), (L.bgp_tw, 1.0 if on else 0.0),
                               (L.bgo_tw, 1.0 if on else 0.0)):
                tw.snap(target) if reason == "init" else tw.to(target, now)

    # ---- por frame ----
    def _frame(self, dt):
        a = self._find_active()
        live = self._find_live(a)
        if a != self.active or live != self.live or self.seek_flag or self.config_flag:
            reason = ("init" if self.active == -1 else "seek" if self.seek_flag else
                      "config" if self.config_flag else "tick")
            self.active, self.live, self.seek_flag, self.config_flag = a, live, False, False
            self._relayout(reason, self.now)
            for i, L in enumerate(self.items):
                self._render_item(L, i in live, dt if abs(i - a) <= 1 or i in live else 0.0)
        else:
            for i in sorted(live | set(range(max(0, a - 1), min(len(self.items) - 1, a + 1) + 1))):
                self._render_item(self.items[i], i in live, dt)
        for L in self.items:
            L.y.step(dt, self.now)
            L.s.step(dt, self.now)
            L.blur, L.opacity = L.blur_tw.value(self.now), L.op_tw.value(self.now)
            L.act, L.bg_p, L.bg_op = L.act_tw.value(self.now), L.bgp_tw.value(self.now), L.bgo_tw.value(self.now)

    def _render_item(self, L, on, dt):
        if L.is_gap:
            self._render_gap(L, on, dt)
        elif not L.is_credit:
            self._render_line(L, on, dt)

    def _render_gap(self, L, on, dt):
        op, sc, d, phase, f = interlude_snap(L.plan, (self.t - L.start) * 1000 * L.speed) if on else HIDDEN
        L.grp_op, L.grp_sc, L.phase = op, sc, phase
        for i, dot in enumerate(L.dots):
            fi = f[i]
            dot.sp.g = 0.5 + 0.8 * fi
            if dt:
                v = dot.sp.step(dt)
            else:
                dot.sp.snap(dot.sp.g)
                v = dot.sp.g
            dot.yp.g = -0.06 * fi  # sube conforme se llena, con impulsos al cruzar cada tramo
            if dt:
                for k, th in enumerate((0.02, 0.35, 0.7, 0.97)):
                    if dot.prev < th <= fi:
                        dot.yp.v -= 0.55 + 0.25 * k
                if fi < dot.prev - 0.5:
                    dot.yp.snap(dot.yp.g)
            else:
                dot.yp.snap(dot.yp.g)
            dot.prev = fi
            dot.y = dot.yp.step(dt) if dt else dot.yp.g
            dot.op, dot.scale = d[i], v
            dot.halo_r, dot.halo_a = (0.15 + 0.3 * fi, 0.45 * fi) if fi > 0.02 else (0.0, 0.0)

    def _render_line(self, L, on, dt):
        t, P, snap = self.t, self.p, not dt
        fl, vib = P.float and not P.reduce, P.vib and not P.reduce
        ta = L.start - LOOKAHEAD
        # la capa por carácter solo vive en la línea activa y sus vecinas (las únicas que se recalculan cada frame) y desde que la
        # línea se activó: así lo que sale del rango deja de moverse en 0, nunca congelado a medias
        pf = motion.resolve(P.motion, P.reduce, L.motion) if (abs(L.index - self.active) <= 1 or L.index in self.live) and t >= ta else None
        for wi, w in enumerate(L.words):
            dip = motion.enter_dip(pf, P.motion_amt, t, ta, wi) if pf else 0.0
            w.p = _prog(t, w.b, w.e)
            bgk = 2 if w.bg else 1
            if not w.emph:
                for u in w.parts:
                    raw = _prog(t, u.b, u.e)
                    u.p = WIPE(raw)  # el relleno acelera y frena dentro de cada sílaba; las curvas de abajo siguen el avance lineal
                    gs, gy, gg = 1.0, 0.0, 0.0
                    if L.approx:  # sin sílabas reales no hay saltos por palabra: solo el barrido y un brillo suave sobre lo que se canta
                        if u.b <= t < u.e and on:
                            gg = GLOW(raw)
                    elif fl:
                        if t < u.b:
                            gs, gy = SCALE(0), YOFF(0)
                        elif t < u.e:
                            s, y = SCALE(raw), YOFF(raw)  # las notas largas suben y crecen más (solo por encima de 1.0 / de la posición base)
                            gs = s + max(0.0, s - 1.0) * SUSTAIN_SCALE * u.sus
                            gy = y + min(0.0, y) * SUSTAIN_LIFT * u.sus
                            gg = GLOW(raw) * (1 + SUSTAIN_GLOW * u.sus) if on else 0.0
                    s, y, g = self._springs(u, gs, gy, gg, dt, snap)
                    u.scale, u.y, u.x = s, y * bgk, 0.0
                    u.wdy, u.wds = (motion.syllable(pf, P.motion_amt, t, u.b, u.e, len(u.text), u.sus, dip, u.y, s - 1.0)
                                    if pf else None) or (None, None)
                    u.glow_r, u.glow_a = (4 + 2 * g, min(0.35 * g, 1.0)) if g > 0.01 else (0.0, 0.0)
                continue
            # palabra con énfasis: la letra activa crece y brilla; las vecinas caen con la distancia
            for c in w.parts:  # cada sílaba se rellena con su propio easing y respeta las pausas; sus letras se reparten ese avance
                c.p = clamp01(WIPE(_prog(t, c.sb, c.se)) * c.k - c.j)
            w.p = sum(c.p for c in w.parts) / len(w.parts)
            active_w = on and P.emph and w.b <= t < w.e
            ai, ap = -1, 0.0
            if active_w:
                for i, c in enumerate(w.parts):
                    if c.b <= t < c.e:
                        ai, ap = i, (t - c.b) / (c.e - c.b)
            for i, c in enumerate(w.parts):
                gs, gy, gg = 1.0, 0.0, 0.0
                if P.emph and fl:
                    if t < w.b:
                        gs, gy = L_SCALE(0), L_YOFF(0)
                    elif active_w and ai >= 0:
                        dist = abs(i - ai)
                        fo, gfo = 1 / (1 + dist ** 2.8), 1 / (1 + dist * 0.9)
                        gs = L_SCALE(0) + (L_SCALE(ap) - L_SCALE(0)) * fo
                        gy = L_YOFF(0) + (L_YOFF(ap) - L_YOFF(0)) * fo
                        gg = GLOW(ap) * gfo * (1.15 if w.last else 1.0)
                    elif t < w.e:
                        gs, gy = L_SCALE(0), L_YOFF(0)
                s, y, g = self._springs(c, gs, gy, gg, dt, snap)
                vx = vy = 0.0
                if vib and active_w and g > 0.05:  # micro-vibrato mientras se sostiene la nota
                    a = min(1.0, g)
                    vy = math.sin(t * 2 * math.pi * 6.3 + c.phase) * 0.011 * a
                    vx = math.sin(t * 2 * math.pi * 4.7 + c.phase * 1.7) * 0.006 * a
                c.scale, c.y, c.x = s, y * 2 * bgk + vy, vx
                d = motion.limit(dip, c.y, motion.LIFT_CAP)  # las letras con énfasis ya tienen su propia animación: aquí solo la entrada
                c.wdy, c.wds = ([d], [0.0]) if d else (None, None)
                c.glow_r, c.glow_a = (4 + 12 * g, min(1.85 * g * 0.55, 1.0)) if g > 0.01 else (0.0, 0.0)

    def _springs(self, u, gs, gy, gg, dt, snap):
        u.S.g, u.Y.g, u.G.g = gs, gy, gg
        if snap:
            u.S.snap(gs)
            u.Y.snap(gy)
            u.G.snap(gg)
            return gs, gy, gg
        u.Y.d = self.p.wob
        return u.S.step(dt), u.Y.step(dt), u.G.step(dt)
