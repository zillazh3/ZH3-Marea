"""Modelo cinemático (marea.cinematic v1): recursos de video, intervalos, estética de recuerdo y sugerencia musical.

Sin dependencias de Qt ni de Skia para que `ExportJob` cruce `multiprocessing` (`spawn`) sin problemas.
"""
from __future__ import annotations

import copy
import hashlib
import math
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields
from typing import Callable

import numpy as np

from marea.audio import align
from marea.engine.curves import clamp01, smoothstep
from marea.model.lyrics import Lyrics

SCHEMA = "marea.cinematic"
VERSION = 1

MIN_INTERVAL_MS = 800.0
DEFAULT_INTERVAL_MS = 4500.0
MIN_GAP_MS = 1600.0  # aire mínimo de portada entre recuerdos sugeridos (escala dinámicamente con el ritmo)
MAX_COVERAGE = 0.45  # fracción máxima del tramo cubierta por sugerencias automáticas


class CinematicError(ValueError):
    pass


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else float(v)


@dataclass
class CinematicTreatment:
    """Parámetros estéticos de un recuerdo visual; priorizan que la letra blanca siga siendo legible."""
    opacity: float = 1.0       # 0.20 .. 1.00: presencia pico sobre el fondo de portada
    blur: float = 0.35         # 0.00 .. 1.00: desenfoque suave base + dinámico en entradas/salidas/voz
    vignette: float = 0.45     # 0.00 .. 1.00: viñeta radial suave en bordes
    grain: float = 0.08        # 0.00 .. 0.35: grano de película determinista (24 Hz)
    saturation: float = 0.82   # 0.00 .. 1.40: saturación nostálgica ligeramente contenida
    contrast: float = 0.92     # 0.60 .. 1.30: contraste suave para domar altas luces
    dim: float = 0.32          # 0.00 .. 0.75: velo oscuro para proteger la tipografía

    def clamped(self) -> "CinematicTreatment":
        return CinematicTreatment(
            opacity=_clamp(self.opacity, 0.20, 1.00),
            blur=_clamp(self.blur, 0.00, 1.00),
            vignette=_clamp(self.vignette, 0.00, 1.00),
            grain=_clamp(self.grain, 0.00, 0.35),
            saturation=_clamp(self.saturation, 0.00, 1.40),
            contrast=_clamp(self.contrast, 0.60, 1.30),
            dim=_clamp(self.dim, 0.00, 0.75),
        )


@dataclass
class VideoResource:
    """Metadatos de un archivo de video fuente (solo visual; su audio nunca entra a la composición)."""
    id: str
    path: str
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    fps_num: int = 30
    fps_den: int = 1
    sar_num: int = 1
    sar_den: int = 1
    visual_start_ms: int = 0
    visual_end_ms: int = 0
    scene_cuts_ms: list[int] = field(default_factory=list)
    salience_peaks_ms: list[int] = field(default_factory=list)

    @property
    def fps(self) -> float:
        return self.fps_num / max(1, self.fps_den)

    @property
    def frame_duration_ms(self) -> float:
        return 1000.0 * max(1, self.fps_den) / max(1, self.fps_num)

    @property
    def frame_count(self) -> int:
        if self.duration_ms <= 0:
            return 1
        return max(1, int(math.ceil(self.duration_ms / 1000.0 * self.fps_num / max(1, self.fps_den))))

    @property
    def active_bounds_ms(self) -> tuple[float, float]:
        """Ventana útil sin cuadros negros/congelados iniciales ni finales, con margen seguro antes del último PTS."""
        if self.duration_ms <= 0:
            return 0.0, 0.0
        total = float(self.duration_ms)
        eof_safe = max(MIN_INTERVAL_MS, total - min(42.0, self.frame_duration_ms * 0.5)) if total > 1500.0 else total
        v0 = _clamp(float(self.visual_start_ms), 0.0, max(0.0, eof_safe - MIN_INTERVAL_MS))
        raw_v1 = float(self.visual_end_ms) if self.visual_end_ms > 0 else total
        v1 = _clamp(raw_v1, min(eof_safe, v0 + MIN_INTERVAL_MS), eof_safe)
        if v1 <= v0:
            return 0.0, eof_safe
        return v0, v1


@dataclass
class CinematicInterval:
    """Ventana donde el video reemplaza temporalmente al fondo animado de la portada."""
    id: str
    start_ms: float
    end_ms: float
    video_start_ms: float
    video_end_ms: float
    fade_in_ms: float = 900.0
    fade_out_ms: float = 1100.0
    treatment: CinematicTreatment = field(default_factory=CinematicTreatment)
    video_id: str = ""
    kind: str = "manual"  # "interlude" | "transition" | "pause" | "accent" | "manual"

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)

    @property
    def video_duration_ms(self) -> float:
        return max(0.0, self.video_end_ms - self.video_start_ms)

    def map_source_ms(self, t_ms: float, max_duration_ms: int = 0) -> float:
        """Mapeo explícito y determinista: tiempo de la canción -> tiempo en el segmento del video fuente."""
        dur = self.end_ms - self.start_ms
        u = clamp01((t_ms - self.start_ms) / dur) if dur > 1e-6 else 0.0
        v0 = max(0.0, float(self.video_start_ms))
        v1 = max(v0, float(self.video_end_ms))
        src = v0 + u * (v1 - v0)
        if max_duration_ms > 0:
            src = _clamp(src, 0.0, float(max_duration_ms))
        return src

    def envelope_at(self, t_ms: float) -> float:
        """0..1 con curva smoothstep; si fade_in + fade_out supera la duración, ambos se escalan sin salto."""
        if t_ms <= self.start_ms or t_ms >= self.end_ms or self.end_ms <= self.start_ms:
            return 0.0
        dur = self.end_ms - self.start_ms
        fi, fo = max(0.0, float(self.fade_in_ms)), max(0.0, float(self.fade_out_ms))
        if fi + fo > dur and fi + fo > 1e-6:
            k = dur / (fi + fo)
            fi, fo = fi * k, fo * k
        w_in = smoothstep((t_ms - self.start_ms) / fi) if fi > 1e-6 else 1.0
        w_out = smoothstep((self.end_ms - t_ms) / fo) if fo > 1e-6 else 1.0
        return min(w_in, w_out)


@dataclass
class ActiveInterval:
    interval: CinematicInterval
    resource: VideoResource
    envelope: float  # 0..1 (curva de entrada/salida)
    src_ms: float    # instante exacto en el video fuente


@dataclass
class CinematicConfig:
    version: int = VERSION
    enabled: bool = False
    primary_video_id: str = ""
    resources: dict[str, VideoResource] = field(default_factory=dict)
    default_treatment: CinematicTreatment = field(default_factory=CinematicTreatment)
    intervals: list[CinematicInterval] = field(default_factory=list)
    opening_trim_ms: float = 0.0
    auto_opening_trim: bool = True

    @property
    def primary_video(self) -> VideoResource | None:
        return self.resources.get(self.primary_video_id)

    def video_for(self, interval: CinematicInterval) -> VideoResource | None:
        vid = interval.video_id or self.primary_video_id
        return self.resources.get(vid) or self.primary_video

    def next_interval_id(self) -> str:
        used = {iv.id for iv in self.intervals}
        n = 1
        while f"iv_{n}" in used:
            n += 1
        return f"iv_{n}"

    def next_video_id(self) -> str:
        used = set(self.resources)
        n = 1
        while f"vid_{n}" in used:
            n += 1
        return f"vid_{n}"

    def set_primary_video(self, resource: VideoResource) -> VideoResource:
        """Importa o reemplaza el video principal, reasignando y ajustando intervalos previos sin perderlos."""
        old_id = self.primary_video_id
        if not resource.id:
            resource = copy.deepcopy(resource)
            resource.id = self.next_video_id()
        self.resources[resource.id] = resource
        self.primary_video_id = resource.id
        if old_id and old_id != resource.id:
            self.resources.pop(old_id, None)
        max_ms = float(resource.duration_ms) if resource.duration_ms > 0 else 0.0
        if max_ms > 0:
            self.opening_trim_ms = round(_clamp(self.opening_trim_ms, 0.0, max(0.0, max_ms - MIN_INTERVAL_MS)), 1)
        for iv in self.intervals:
            if not iv.video_id or iv.video_id == old_id:
                iv.video_id = resource.id
            if iv.video_id == resource.id and max_ms > 0:
                seg_len = max(MIN_INTERVAL_MS, iv.video_end_ms - iv.video_start_ms or iv.duration_ms)
                seg_len = min(seg_len, max_ms)
                iv.video_start_ms = _clamp(iv.video_start_ms, 0.0, max(0.0, max_ms - seg_len))
                iv.video_end_ms = min(max_ms, iv.video_start_ms + seg_len)
        return resource

    def add_interval(self, start_ms: float, end_ms: float, video_start_ms: float | None = None,
                     video_end_ms: float | None = None, fade_in_ms: float = 900.0, fade_out_ms: float = 1100.0,
                     treatment: CinematicTreatment | None = None, video_id: str = "", kind: str = "manual") -> CinematicInterval:
        start_ms = max(0.0, float(start_ms))
        end_ms = max(start_ms + MIN_INTERVAL_MS, float(end_ms))
        vid_id = video_id or self.primary_video_id
        res = self.resources.get(vid_id) or self.primary_video
        dur = end_ms - start_ms
        if video_start_ms is None:
            video_start_ms = self.opening_trim_ms
        video_start_ms = max(0.0, float(video_start_ms))
        if video_end_ms is None:
            video_end_ms = video_start_ms + dur
        video_end_ms = max(video_start_ms, float(video_end_ms))
        if res and res.duration_ms > 0:
            max_v = float(res.duration_ms)
            seg = min(max_v, max(MIN_INTERVAL_MS, video_end_ms - video_start_ms) if video_end_ms > video_start_ms else min(dur, max_v))
            video_start_ms = _clamp(video_start_ms, 0.0, max(0.0, max_v - seg))
            video_end_ms = min(max_v, video_start_ms + seg)
        iv = CinematicInterval(
            id=self.next_interval_id(),
            start_ms=round(start_ms, 1),
            end_ms=round(end_ms, 1),
            video_start_ms=round(video_start_ms, 1),
            video_end_ms=round(video_end_ms, 1),
            fade_in_ms=max(0.0, round(float(fade_in_ms), 1)),
            fade_out_ms=max(0.0, round(float(fade_out_ms), 1)),
            treatment=(copy.copy(treatment) if treatment else copy.copy(self.default_treatment)).clamped(),
            video_id=res.id if res else vid_id,
            kind=kind,
        )
        self.intervals.append(iv)
        self.sort_intervals()
        return iv

    def remove_interval(self, interval_id: str) -> bool:
        before = len(self.intervals)
        self.intervals = [iv for iv in self.intervals if iv.id != interval_id]
        return len(self.intervals) < before

    def sort_intervals(self):
        self.intervals.sort(key=lambda iv: (iv.start_ms, iv.end_ms, iv.id))


def evaluate_at(config: CinematicConfig | None, t_ms: float) -> list[ActiveInterval]:
    """Intervalos activos en `t_ms` (orden cronológico; máximo 2 si el usuario solapó dos recuerdos)."""
    if config is None or not config.enabled or not config.intervals or not config.resources:
        return []
    active: list[ActiveInterval] = []
    for iv in sorted(config.intervals, key=lambda x: (x.start_ms, x.end_ms, x.id)):
        res = config.video_for(iv)
        if res is None or not res.path:
            continue
        env = iv.envelope_at(t_ms)
        if env > 1e-4:
            active.append(ActiveInterval(iv, res, env, iv.map_source_ms(t_ms, res.duration_ms)))
    if len(active) > 2:
        active.sort(key=lambda a: (a.envelope, a.interval.start_ms), reverse=True)
        active = sorted(active[:2], key=lambda a: (a.interval.start_ms, a.interval.id))
    return active


# ---------------- serialización versionada (marea.cinematic v1) ----------------

MIGRATIONS: dict[int, Callable[[dict], dict]] = {}  # v -> transformador a v+1 cuando suba VERSION


def _pick(cls, d: dict, **extra):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names and k not in extra}, **extra)


def to_dict(config: CinematicConfig) -> dict:
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "enabled": bool(config.enabled),
        "primary_video_id": config.primary_video_id,
        "opening_trim_ms": round(max(0.0, float(config.opening_trim_ms)), 1),
        "auto_opening_trim": bool(config.auto_opening_trim),
        "default_treatment": asdict(config.default_treatment.clamped()),
        "resources": {k: asdict(r) for k, r in config.resources.items()},
        "intervals": [
            {
                **{k: v for k, v in asdict(iv).items() if k != "treatment"},
                "treatment": asdict(iv.treatment.clamped()),
            }
            for iv in config.intervals
        ],
    }


def migrate_dict(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise CinematicError("La configuración cinemática no es un objeto JSON válido.")
    schema = raw.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise CinematicError(f"Esquema cinemático no soportado: {schema!r} (se esperaba {SCHEMA!r}).")
    ver = int(raw.get("version", 1))
    if ver < 1 or ver > VERSION:
        raise CinematicError(f"Versión cinemática no soportada: v{ver} (esta app lee hasta v{VERSION}).")
    cur = dict(raw)
    while ver < VERSION:
        cur = MIGRATIONS[ver](cur)
        ver = int(cur.get("version", ver + 1))
    return cur


def from_dict(raw: dict | None) -> CinematicConfig:
    if not raw:
        return CinematicConfig()
    d = migrate_dict(raw)
    default_tr = _pick(CinematicTreatment, d.get("default_treatment") or {}).clamped()
    resources = {}
    for k, rd in (d.get("resources") or {}).items():
        if isinstance(rd, dict) and rd.get("path") is not None:
            res = _pick(VideoResource, rd)
            res.id = res.id or str(k)
            res.fps_num = max(1, int(res.fps_num))
            res.fps_den = max(1, int(res.fps_den))
            res.sar_num = max(1, int(res.sar_num))
            res.sar_den = max(1, int(res.sar_den))
            res.visual_start_ms = max(0, int(res.visual_start_ms or 0))
            res.visual_end_ms = max(0, int(res.visual_end_ms or 0))
            res.scene_cuts_ms = sorted({max(0, int(x)) for x in (res.scene_cuts_ms or []) if isinstance(x, (int, float))})[:64]
            res.salience_peaks_ms = sorted({max(0, int(x)) for x in (res.salience_peaks_ms or []) if isinstance(x, (int, float))})[:32]
            resources[res.id] = res
    intervals = []
    for i, ivd in enumerate(d.get("intervals") or []):
        if not isinstance(ivd, dict):
            continue
        tr = _pick(CinematicTreatment, ivd.get("treatment") or asdict(default_tr)).clamped()
        iv = _pick(CinematicInterval, ivd, treatment=tr)
        iv.id = iv.id or f"iv_{i + 1}"
        iv.start_ms = max(0.0, float(iv.start_ms))
        iv.end_ms = max(iv.start_ms + 1.0, float(iv.end_ms))
        iv.video_start_ms = max(0.0, float(iv.video_start_ms))
        iv.video_end_ms = max(iv.video_start_ms, float(iv.video_end_ms))
        iv.fade_in_ms = max(0.0, float(iv.fade_in_ms))
        iv.fade_out_ms = max(0.0, float(iv.fade_out_ms))
        intervals.append(iv)
    cfg = CinematicConfig(
        version=VERSION,
        enabled=bool(d.get("enabled", False)),
        primary_video_id=str(d.get("primary_video_id", "")),
        resources=resources,
        default_treatment=default_tr,
        intervals=intervals,
        opening_trim_ms=max(0.0, round(float(d.get("opening_trim_ms", 0.0) or 0.0), 1)),
        auto_opening_trim=bool(d.get("auto_opening_trim", True)),
    )
    if cfg.primary_video_id not in cfg.resources and cfg.resources:
        cfg.primary_video_id = next(iter(cfg.resources))
    cfg.sort_intervals()
    return cfg


# ---------------- análisis musical cacheado y sugerencia de momentos ----------------

@dataclass
class AudioAnalysis:
    """Curvas acústicas a ~100 Hz para sugerir momentos sin recalcular FFT ni RMS."""
    rate: float                              # muestras por segundo de las envolventes (~100 Hz)
    duration_ms: float
    rms: np.ndarray                          # energía RMS normalizada (0..1+)
    onset: np.ndarray                        # flujo espectral de ataque (marea.audio.align)
    contrast: np.ndarray                     # cambio de intensidad futuro - pasado (+subida / -caída, ~1.6 s)
    pauses_ms: list[tuple[float, float]]     # valles sostenidos de baja energía
    accents_ms: list[tuple[float, float]]    # [(t_ms, fuerza)] acentos rítmicos/musicales
    macro_contrast: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    tempo_bpm: float = 110.0
    beat_period_ms: float = 545.5
    beats_ms: list[float] = field(default_factory=list)


_ANALYSIS_CACHE: OrderedDict[tuple[int, tuple, int], AudioAnalysis] = OrderedDict()
_ANALYSIS_CACHE_MAX = 4
_analysis_computations = 0  # contador observable en tests para verificar que la caché evita recalcular


def _moving_mean(x: np.ndarray, w: int) -> np.ndarray:
    if len(x) == 0 or w <= 1:
        return x.astype(np.float32, copy=True)
    w = min(w, len(x))
    csum = np.concatenate([[0.0], np.cumsum(x, dtype=np.float64)])
    core = ((csum[w:] - csum[:-w]) / w).astype(np.float32)
    pad_l = w // 2
    pad_r = len(x) - len(core) - pad_l
    return np.concatenate([np.full(pad_l, core[0], np.float32), core, np.full(pad_r, core[-1], np.float32)])


def _estimate_tempo_and_beats(novelty: np.ndarray, env_rate: float) -> tuple[float, float, list[float]]:
    """Estima tempo (BPM), periodo de pulso (ms) y rejilla de golpes reales por autocorrelación de ataques."""
    n = len(novelty)
    if n < round(2.2 * env_rate) or float(novelty.max()) < 0.14:
        return 110.0, 545.5, []

    # Ventana central de hasta 90 s para estimar el pulso en < 0.5 ms incluso en canciones largas
    max_bins = round(90.0 * env_rate)
    if n > max_bins:
        i0 = (n - max_bins) // 2
        seg = novelty[i0:i0 + max_bins]
    else:
        seg = novelty
    centered = (seg - float(seg.mean())).astype(np.float32)
    energy = float(np.dot(centered, centered))
    if energy <= 1e-6:
        return 110.0, 545.5, []

    lag_lo = max(4, round(env_rate * 60.0 / 185.0))
    lag_hi = min(len(centered) // 2, round(env_rate * 60.0 / 62.0))
    best_lag = max(lag_lo, round(env_rate * 60.0 / 110.0))
    best_score = -1e9
    for lag in range(lag_lo, lag_hi + 1):
        ac = float(np.dot(centered[:-lag], centered[lag:])) / max(1, len(centered) - lag)
        bpm = 60.0 * env_rate / lag
        prior = math.exp(-0.5 * ((bpm - 114.0) / 58.0) ** 2)
        score = ac * (0.65 + 0.35 * prior)
        if score > best_score:
            best_score = score
            best_lag = lag

    beat_period_ms = best_lag / env_rate * 1000.0
    tempo_bpm = 60000.0 / max(1.0, beat_period_ms)

    min_beat_sep = max(3, round(0.62 * best_lag))
    beat_thr = max(0.11, float(np.percentile(novelty, 74)))
    beats_ms: list[float] = []
    last_b = -min_beat_sep
    for idx in range(1, n - 1):
        v = float(novelty[idx])
        if v >= beat_thr and v >= novelty[idx - 1] and v >= novelty[idx + 1]:
            if idx - last_b >= min_beat_sep:
                beats_ms.append(idx / env_rate * 1000.0)
                last_b = idx
            elif beats_ms and v > float(novelty[last_b]):
                beats_ms[-1] = idx / env_rate * 1000.0
                last_b = idx
    return round(tempo_bpm, 2), round(beat_period_ms, 2), beats_ms


def analyze_audio(samples: np.ndarray, sample_rate: int = 48000) -> AudioAnalysis:
    """Extrae RMS, pulso/tempo, cambios de sección, pausas y acentos reutilizando `align.onset_envelope`, con caché."""
    global _analysis_computations
    key = (id(samples), tuple(samples.shape), int(sample_rate))
    cached = _ANALYSIS_CACHE.get(key)
    if cached is not None:
        _ANALYSIS_CACHE.move_to_end(key)
        return cached

    _analysis_computations += 1
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    mono = np.asarray(mono, dtype=np.float32)
    duration_ms = len(mono) / max(1, sample_rate) * 1000.0
    hop = max(1, round(sample_rate / 100))
    env_rate = sample_rate / hop
    if len(mono) < hop * 4:
        empty = np.zeros(0, np.float32)
        res = AudioAnalysis(env_rate, duration_ms, empty, empty, empty, [], [], empty)
        _ANALYSIS_CACHE[key] = res
        return res

    # RMS en ventanas de 2·hop (~20 ms) al mismo paso que align.onset_envelope
    win = 2 * hop
    n_rms = 1 + max(0, (len(mono) - win) // hop)
    sq = mono * mono
    csum = np.concatenate([[0.0], np.cumsum(sq, dtype=np.float64)])
    starts = np.arange(n_rms) * hop
    rms = np.sqrt(np.maximum(0.0, (csum[starts + win] - csum[starts]) / win)).astype(np.float32)

    onset, onset_rate = align.onset_envelope(mono, sample_rate)
    n = min(len(rms), len(onset)) if len(onset) else len(rms)
    rms = rms[:n]
    onset = onset[:n] if len(onset) else np.zeros(n, np.float32)
    env_rate = onset_rate if len(onset) else env_rate

    p95_rms = float(np.percentile(rms, 95)) if n else 0.0
    rms_norm = rms / max(p95_rms, 1e-6) if p95_rms > 1e-6 else np.zeros_like(rms)
    rms_span = float(np.percentile(rms_norm, 98) - np.percentile(rms_norm, 5)) if n else 0.0
    p95_on = float(np.percentile(onset, 95)) if n else 0.0
    on_span = float(np.percentile(onset, 99) - np.percentile(onset, 50)) if n else 0.0
    # Un tono continuo de amplitud plana (rms_span < 5 %) no tiene ataques reales (evita saltos ULP de float32 en 2^k s)
    has_attacks = rms_span >= 0.05 and p95_on > 0.05 and on_span > 0.15
    onset_norm = onset / max(p95_on, 0.25) if has_attacks else np.zeros_like(rms)

    smooth_rms = _moving_mean(rms_norm, max(3, round(0.35 * env_rate)))
    # Contraste rápido (~1.6 s: entradas de frase/drop) y contraste macro (~4.5 s: verso -> coro -> puente)
    sec_w = max(5, round(1.6 * env_rate))
    mean_sec = _moving_mean(smooth_rms, sec_w)
    half = sec_w // 2
    past = np.concatenate([np.full(half, mean_sec[0], np.float32), mean_sec[:-half]]) if half < n else mean_sec
    fut = np.concatenate([mean_sec[half:], np.full(half, mean_sec[-1], np.float32)]) if half < n else mean_sec
    contrast = (fut - past).astype(np.float32)

    mac_w = max(9, round(4.5 * env_rate))
    mean_mac = _moving_mean(smooth_rms, mac_w)
    mhalf = mac_w // 2
    mpast = np.concatenate([np.full(mhalf, mean_mac[0], np.float32), mean_mac[:-mhalf]]) if mhalf < n else mean_mac
    mfut = np.concatenate([mean_mac[mhalf:], np.full(mhalf, mean_mac[-1], np.float32)]) if mhalf < n else mean_mac
    macro_contrast = (mfut - mpast).astype(np.float32)

    # Pausas / respiros acústicos: tramos ≥ 0.8 s bastante por debajo de la energía activa mediana
    active_vals = smooth_rms[smooth_rms > 0.05]
    med_rms = float(np.median(active_vals)) if len(active_vals) else 0.0
    pauses_ms: list[tuple[float, float]] = []
    if med_rms > 0.05:
        quiet = smooth_rms < (0.38 * med_rms)
        min_bins = max(4, round(0.8 * env_rate))
        i = 0
        while i < n:
            if not quiet[i]:
                i += 1
                continue
            j = i + 1
            while j < n and quiet[j]:
                j += 1
            if j - i >= min_bins:
                pauses_ms.append((i / env_rate * 1000.0, j / env_rate * 1000.0))
            i = j

    # Acentos musicales + rejilla de pulsos: ataques espectrales y subidas rápidas de RMS frente a su media local
    d_rms = np.maximum(0.0, np.diff(smooth_rms, prepend=smooth_rms[:1])) * 4.0
    local_on = _moving_mean(onset_norm, max(3, round(0.6 * env_rate)))
    novelty = np.maximum(0.0, onset_norm - 1.1 * local_on) * 0.7 + d_rms * 0.3
    accents_ms: list[tuple[float, float]] = []
    min_sep = max(3, round(0.50 * env_rate))
    nov_thr = max(0.18, float(np.percentile(novelty, 86))) if n else 1.0
    last_idx = -min_sep
    for idx in range(1, n - 1):
        v = float(novelty[idx])
        if v >= nov_thr and v >= novelty[idx - 1] and v >= novelty[idx + 1]:
            if idx - last_idx >= min_sep:
                accents_ms.append((idx / env_rate * 1000.0, v))
                last_idx = idx
            elif accents_ms and v > accents_ms[-1][1]:
                accents_ms[-1] = (idx / env_rate * 1000.0, v)
                last_idx = idx

    tempo_bpm, beat_period_ms, beats_ms = _estimate_tempo_and_beats(novelty, env_rate)

    res = AudioAnalysis(
        env_rate, duration_ms, smooth_rms, onset_norm, contrast, pauses_ms, accents_ms,
        macro_contrast=macro_contrast, tempo_bpm=tempo_bpm, beat_period_ms=beat_period_ms, beats_ms=beats_ms,
    )
    _ANALYSIS_CACHE[key] = res
    while len(_ANALYSIS_CACHE) > _ANALYSIS_CACHE_MAX:
        _ANALYSIS_CACHE.popitem(last=False)
    return res


def _snap_to_accent(t_ms: float, accents_ms: list[tuple[float, float]], radius_ms: float = 450.0) -> float:
    best_t, best_v = t_ms, -1.0
    for acc_t, strength in accents_ms:
        if abs(acc_t - t_ms) <= radius_ms and strength > best_v:
            best_t, best_v = acc_t, strength
    return best_t


def _snap_to_beat(t_ms: float, beats_ms: list[float], radius_ms: float = 280.0) -> float:
    if not beats_ms:
        return t_ms
    nearest = min(beats_ms, key=lambda b: abs(b - t_ms))
    return nearest if abs(nearest - t_ms) <= radius_ms else t_ms


def _q10(ms: float) -> float:
    """Cuantiza a múltiplos de 10 ms (0.01 s) para coincidir exactamente con los controles del editor."""
    return round(float(ms) / 10.0) * 10.0


def compute_opening_trim_ms(
    video_duration_ms: float,
    song_duration_ms: float,
    resource: VideoResource | None = None,
) -> float:
    """Calcula el recorte inicial del video para alinearlo con la duración real de la pista musical.

    - Jamás recorta la pista de audio: si el video dura más que la canción, prioriza recortar la introducción del video
      para que el metraje restante acompañe la progresión completa de la música.
    - Salta cuadros negros o congelados al inicio y busca iniciar tras un cambio de plano limpio sin partir momentos clave.
    """
    v_total = max(0.0, float(resource.duration_ms if (resource and resource.duration_ms > 0) else video_duration_ms))
    if v_total <= 0.0:
        return 0.0

    if resource is not None and resource.duration_ms > 0:
        v_first, v_last = resource.active_bounds_ms
        cuts = [float(c) for c in resource.scene_cuts_ms if v_first <= c < v_last]
        peaks = [float(p) for p in resource.salience_peaks_ms if v_first <= p < v_last]
    else:
        v_first, v_last = 0.0, v_total
        cuts, peaks = [], []

    s_dur = max(0.0, float(song_duration_ms))
    active_span = max(0.0, v_last - v_first)
    if s_dur <= 0.0 or active_span <= s_dur:
        return _q10(v_first)

    # El video útil excede a la canción: recortamos primero el tramo inicial del video
    excess = active_span - s_dur
    target_open = v_last - s_dur
    if not cuts and not peaks:
        return _q10(_clamp(target_open, v_first, max(v_first, v_total - MIN_INTERVAL_MS)))

    tol_back = min(3200.0, 0.35 * excess)
    tol_fwd = min(1400.0, max(0.0, (v_total - v_last) * 0.5 + min(650.0, 0.02 * s_dur)))
    lo = max(v_first, target_open - tol_back)
    hi = min(max(v_first, v_last - min(s_dur * 0.85, v_last - v_first)), target_open + tol_fwd)
    if hi < lo:
        hi = lo

    entry_cands: list[tuple[float, bool]] = [(target_open, False)]
    if lo <= v_first <= hi:
        entry_cands.append((v_first, False))
    for c in cuts:
        entry = c + 40.0  # 1–2 frames tras el corte: plano nuevo ya asentado
        if lo <= entry <= hi:
            entry_cands.append((entry, True))

    best_t, best_score = target_open, -1e9
    for cand, is_shot_start in entry_cands:
        score = -abs(cand - target_open) / 1400.0
        if is_shot_start:
            score += 1.35
        # Evita arrancar justo antes de un corte brusco (< 0.9 s después de abrir)
        if any(40.0 < (c - cand) < 900.0 for c in cuts):
            score -= 1.80
        # Evita partir por la mitad un pico visual importante en la apertura
        if any(-500.0 <= (cand - p) <= 700.0 for p in peaks):
            score -= 1.15
        if score > best_score:
            best_score, best_t = score, cand

    return _q10(_clamp(best_t, v_first, max(v_first, v_total - MIN_INTERVAL_MS)))


def song_fingerprint(
    lyrics: Lyrics,
    analysis: AudioAnalysis | None = None,
    t_min: float = 0.0,
    t_max: float = 0.0,
) -> int:
    """Semilla determinista propia de la estructura de cada canción (distintas canciones => distinta firma)."""
    h = hashlib.blake2b(digest_size=8)
    h.update(f"{round(t_min)}:{round(t_max)}:{len(lyrics.lines)}:{len(lyrics.interludes)}".encode())
    if lyrics.lines:
        idxs = sorted({0, len(lyrics.lines) // 3, (2 * len(lyrics.lines)) // 3, len(lyrics.lines) - 1})
        for i in idxs:
            ln = lyrics.lines[i]
            h.update(f"|{round(ln.start)}-{round(ln.end)}:{len(ln.syllables)}:{len(ln.text)}".encode())
    if analysis is not None and len(analysis.rms):
        q = np.quantile(analysis.rms, [0.2, 0.5, 0.8])
        h.update(
            f"|bpm={round(analysis.tempo_bpm * 10)}:p={len(analysis.pauses_ms)}:a={len(analysis.accents_ms)}"
            f":q={round(float(q[0]) * 100)},{round(float(q[1]) * 100)},{round(float(q[2]) * 100)}".encode()
        )
    return int.from_bytes(h.digest(), "little")


def _estimate_pacing(lyrics: Lyrics, analysis: AudioAnalysis | None, t_min: float, t_max: float) -> tuple[float, float]:
    """Devuelve `(pacing 0..1, beat_period_ms)` combinando tempo, densidad de ataques y cadencia silábica."""
    span_s = max(1.0, (t_max - t_min) / 1000.0)
    lines = [ln for ln in lyrics.lines if ln.end > t_min and ln.start < t_max]
    n_syl = sum(max(1, len(ln.syllables)) for ln in lines)
    vocal_s = max(1.0, sum(max(0.1, (min(t_max, ln.end) - max(t_min, ln.start)) / 1000.0) for ln in lines))
    syl_rate = n_syl / vocal_s if lines else 2.6
    syl_pacing = clamp01((syl_rate - 1.6) / 3.4)

    if analysis is not None and len(analysis.rms):
        tempo_pacing = clamp01((analysis.tempo_bpm - 68.0) / 92.0)
        acc_in = sum(1 for t, _ in analysis.accents_ms if t_min <= t <= t_max)
        acc_pacing = clamp01((acc_in / span_s - 0.15) / 1.15)
        pacing = clamp01(0.45 * tempo_pacing + 0.35 * acc_pacing + 0.20 * syl_pacing)
        beat_ms = _clamp(analysis.beat_period_ms, 325.0, 960.0)
    else:
        line_rate = len(lines) / span_s
        pacing = clamp01(0.60 * syl_pacing + 0.40 * clamp01((line_rate - 0.12) / 0.35))
        beat_ms = _clamp(60000.0 / (76.0 + 72.0 * pacing), 360.0, 880.0)
    return pacing, beat_ms


def suggest_intervals(
    lyrics: Lyrics,
    video_duration_ms: int,
    audio_samples: np.ndarray | None = None,
    sample_rate: int = 48000,
    audio_offset_ms: float = 0.0,
    range_ms: tuple[float, float] | None = None,
    treatment: CinematicTreatment | None = None,
    video_id: str = "",
    *,
    resource: VideoResource | None = None,
    opening_trim_ms: float | None = None,
    song_duration_ms: float | None = None,
    seed: int | None = None,
) -> list[CinematicInterval]:
    """Genera recortes cinemáticos dinámicos sincronizados con la estructura, el ritmo y la progresión de la canción.

    - El número de recortes, sus duraciones, fundidos y puntos de corte escalan con la duración, tempo y densidad musical.
    - Introduce variación controlada (`seed`) anclada a pulsos, pausas, transiciones e interludios reales.
    - Nunca activa el video por simple umbral de volumen: una señal fuerte y plana sin contraste ni eventos devuelve `[]`.
    - Alinea la línea de tiempo del video con la progresión de la canción, aplicando `opening_trim_ms` para priorizar el
      recorte inicial del video cuando dura más que la pista musical.
    """
    if not lyrics.lines and audio_samples is None:
        return []
    if range_ms is not None:
        t_min, t_max = max(0.0, float(range_ms[0])), float(range_ms[1])
    elif song_duration_ms is not None and song_duration_ms > 0:
        t_min, t_max = 0.0, float(song_duration_ms)
    elif lyrics.lines:
        t_min, t_max = 0.0, float(max(l.end for l in lyrics.lines) + 1500.0)
    else:
        t_min, t_max = 0.0, len(audio_samples) / max(1, sample_rate) * 1000.0
    span = t_max - t_min
    if span < 1800.0:
        return []

    analysis = analyze_audio(audio_samples, sample_rate) if audio_samples is not None else None
    fp = song_fingerprint(lyrics, analysis, t_min, t_max)
    resolved_seed = fp if seed is None else ((fp ^ ((int(seed) + 1) * 0x9E3779B97F4A7C15)) & 0xFFFFFFFFFFFFFFFF)
    rng = np.random.default_rng(resolved_seed)

    pacing, beat_ms = _estimate_pacing(lyrics, analysis, t_min, t_max)
    # En el reloj de la letra: t_cancion = t_audio - audio_offset_ms
    accents = [
        (t - audio_offset_ms, s)
        for t, s in (analysis.accents_ms if analysis else [])
        if t_min - 600.0 <= t - audio_offset_ms <= t_max + 600.0
    ]
    beats = [
        t - audio_offset_ms
        for t in (analysis.beats_ms if analysis else [])
        if t_min - 400.0 <= t - audio_offset_ms <= t_max + 400.0
    ]

    # (base_score, start_ms, end_ms, kind, local_gap_ms, rising_attack)
    candidates: list[tuple[float, float, float, str, float, bool]] = []
    min_cand_dur = _clamp(2100.0 - 650.0 * pacing, 1450.0, 2150.0)
    max_cand_dur = _clamp(7800.0 - 1600.0 * pacing, 5600.0, 7800.0)
    base_gap_ms = _clamp(4100.0 - 2100.0 * pacing, MIN_GAP_MS, 4600.0)

    def quantize_beats(target_ms: float, min_beats: int = 3, max_beats: int = 14) -> float:
        n_b = int(_clamp(round(target_ms / beat_ms), min_beats, max_beats))
        return n_b * beat_ms

    def add_cand(score: float, s: float, e: float, kind: str, gap_scale: float = 1.0, rising: bool = False):
        s = _clamp(s, t_min, t_max)
        e = _clamp(e, t_min, t_max)
        if e - s > max_cand_dur:
            if kind == "interlude" and s <= t_min + 400.0:
                e = s + max_cand_dur
            else:
                mid = 0.5 * (s + e)
                s, e = mid - 0.5 * max_cand_dur, mid + 0.5 * max_cand_dur
        if e - s >= min_cand_dur:
            gap = _clamp(base_gap_ms * gap_scale * float(rng.uniform(0.84, 1.22)), MIN_GAP_MS, 5800.0)
            candidates.append((score, s, e, kind, gap, rising))

    # 1. Interludios instrumentales e intro/outro (espacios donde la letra respira)
    for il in lyrics.interludes:
        if il.end <= t_min or il.start >= t_max:
            continue
        il_s, il_e = max(t_min, float(il.start)), min(t_max, float(il.end))
        dur = il_e - il_s
        if dur >= 1700.0:
            if dur >= 11500.0:
                # Interludios largos (intro extensa o solo instrumental): permite dos respiraciones diferenciadas
                d1 = quantize_beats(_clamp(0.42 * dur * float(rng.uniform(0.88, 1.12)), 3600.0, max_cand_dur), 6, 15)
                add_cand(3.3 + min(1.0, dur / 8000.0), il_s, min(il_e, il_s + d1), "interlude", gap_scale=1.10)
                d2 = quantize_beats(_clamp(0.36 * dur * float(rng.uniform(0.88, 1.12)), 3200.0, max_cand_dur - 400.0), 5, 13)
                add_cand(3.0, max(il_s, il_e - d2 + 450.0), il_e + 500.0, "interlude", gap_scale=1.05, rising=True)
            else:
                pad_in = beat_ms * 0.65 if il.start > 0 else 0.0
                pad_out = beat_ms * float(rng.uniform(0.7, 1.3))
                add_cand(3.2 + min(1.2, dur / 5000.0), il_s - pad_in, il_e + pad_out, "interlude", gap_scale=1.08)
    if lyrics.lines:
        last_end = float(max(l.end for l in lyrics.lines))
        if t_max - last_end >= 2000.0:
            outro_dur = quantize_beats(_clamp((t_max - last_end) * float(rng.uniform(0.75, 1.0)), 2800.0, 6600.0), 5, 14)
            add_cand(2.7, last_end - 220.0, min(t_max, last_end + outro_dur), "interlude", gap_scale=1.05)

    # 2. Cambios de intensidad (rápidos y macro-secciones), pausas acústicas y golpes rítmicos del audio
    if analysis is not None and len(analysis.contrast) > 10:
        c_abs = np.abs(analysis.contrast)
        c_p85 = float(np.percentile(c_abs, 85))
        # Si la energía es plana (tono constante), c_abs.max() ≈ 0 y jamás se inventan cortes por volumen
        if float(c_abs.max()) >= 0.13 and c_p85 >= 0.035:
            min_sep = max(4, round(1.75 * analysis.rate))
            thr = max(0.13, float(np.percentile(c_abs, 82)))
            last_i = -min_sep
            for idx in range(2, len(c_abs) - 2):
                v = float(c_abs[idx])
                if v >= thr and v >= c_abs[idx - 1] and v >= c_abs[idx + 1] and idx - last_i >= min_sep:
                    t_song = idx / analysis.rate * 1000.0 - audio_offset_ms
                    if t_min <= t_song <= t_max:
                        t_anchor = _snap_to_beat(_snap_to_accent(t_song, accents, 420.0), beats, 240.0)
                        rising = bool(analysis.contrast[idx] > 0)
                        lead_beats = float(rng.uniform(0.75, 1.45)) if rising else float(rng.uniform(0.45, 0.95))
                        base_w = (4300.0 - 1400.0 * pacing) if rising else (3600.0 - 1100.0 * pacing)
                        dur_w = quantize_beats(base_w * float(rng.uniform(0.78, 1.24)), 3, 12)
                        s = t_anchor - lead_beats * beat_ms
                        add_cand(2.55 + min(1.45, v * 2.1), s, s + dur_w, "transition", gap_scale=0.92, rising=rising)
                        last_i = idx

        if len(analysis.macro_contrast) > 15:
            m_abs = np.abs(analysis.macro_contrast)
            if float(m_abs.max()) >= 0.15 and float(np.percentile(m_abs, 85)) >= 0.04:
                m_sep = max(8, round(4.0 * analysis.rate))
                m_thr = max(0.15, float(np.percentile(m_abs, 86)))
                last_m = -m_sep
                for idx in range(3, len(m_abs) - 3):
                    mv = float(m_abs[idx])
                    if mv >= m_thr and mv >= m_abs[idx - 1] and mv >= m_abs[idx + 1] and idx - last_m >= m_sep:
                        t_song = idx / analysis.rate * 1000.0 - audio_offset_ms
                        if t_min <= t_song <= t_max:
                            t_anchor = _snap_to_beat(_snap_to_accent(t_song, accents, 500.0), beats, 260.0)
                            rising = bool(analysis.macro_contrast[idx] > 0)
                            dur_w = quantize_beats((4900.0 - 1200.0 * pacing) * float(rng.uniform(0.84, 1.20)), 5, 13)
                            s = t_anchor - beat_ms * (1.2 if rising else 0.7)
                            add_cand(2.45 + min(1.3, mv * 1.9), s, s + dur_w, "transition", gap_scale=1.0, rising=rising)
                            last_m = idx

        for p0, p1 in analysis.pauses_ms:
            s_song, e_song = p0 - audio_offset_ms, p1 - audio_offset_ms
            p_dur = e_song - s_song
            if e_song > t_min and s_song < t_max and p_dur >= 1150.0:
                mid = 0.5 * (s_song + e_song)
                dur = quantize_beats(_clamp((p_dur + 1350.0) * float(rng.uniform(0.85, 1.18)), 2400.0, 5800.0), 4, 12)
                add_cand(2.35 + min(0.6, p_dur / 4500.0), mid - 0.5 * dur, mid + 0.5 * dur, "pause", gap_scale=1.02)

        # Acentos rítmicos puros del audio (cortes ágiles sincronizados al pulso incluso en tramos instrumentales o rápidos)
        if accents:
            acc_strengths = [sv for _, sv in accents]
            acc_p70 = float(np.percentile(acc_strengths, 70)) if len(acc_strengths) >= 3 else 0.25
            acc_gate = max(0.24, acc_p70)
            last_acc_t = -1e9
            min_acc_step = max(1400.0, 2.8 * beat_ms)
            for acc_t, acc_v in accents:
                if t_min <= acc_t <= t_max and acc_v >= acc_gate and (acc_t - last_acc_t) >= min_acc_step:
                    anchor = _snap_to_beat(acc_t, beats, 220.0)
                    n_beats = int(rng.choice([3, 4, 5, 6] if pacing >= 0.48 else [4, 5, 6, 8]))
                    dur_a = _clamp(n_beats * beat_ms, min_cand_dur, 4200.0)
                    lead = beat_ms * float(rng.uniform(0.25, 0.70))
                    add_cand(1.75 + min(1.25, acc_v * 1.6), anchor - lead, anchor - lead + dur_a, "accent", gap_scale=0.82, rising=True)
                    last_acc_t = acc_t

    # 3. Estructura de la letra y frases expresivas (notas sostenidas, coros repetidos, duetos, cambios de cadencia y ataques)
    lines = [l for l in lyrics.lines if l.end > t_min and l.start < t_max]
    seen_norms: set[str] = set()
    prev_norm = ""
    prev_syl_rate = 0.0
    for i, line in enumerate(lines):
        line_dur = max(120.0, float(line.end - line.start))
        max_syl = max(((s.end - s.begin) for s in line.syllables), default=0)
        duet_switch = i > 0 and line.agent != lines[i - 1].agent
        phrase_gap = (line.start - lines[i - 1].end) if i > 0 else 0
        accent_hit = any(abs(a_t - line.start) <= 290.0 and a_v >= 0.28 for a_t, a_v in accents)

        norm_txt = " ".join(line.text.lower().split())
        chorus_return = bool(len(norm_txt) >= 6 and norm_txt in seen_norms and norm_txt != prev_norm)
        cur_syl_rate = max(1, len(line.syllables)) / (line_dur / 1000.0)
        cadence_shift = (
            analysis is None
            and i > 0
            and prev_syl_rate > 0
            and (cur_syl_rate / prev_syl_rate >= 1.58 or cur_syl_rate / prev_syl_rate <= 0.62)
        )

        score = 0.0
        if max_syl >= 680 and not line.approx:
            score += 1.25 + min(0.85, (max_syl - 680) / 950.0)
        if line.bg:
            score += 0.95
        if duet_switch:
            score += 1.10
        if 550 <= phrase_gap < 2400:
            score += 0.65 + (0.25 if phrase_gap >= 1050 else 0.0)
        if accent_hit:
            score += 0.90
        if chorus_return:
            score += 1.15
        if cadence_shift:
            score += 0.85

        if norm_txt:
            seen_norms.add(norm_txt)
        prev_norm = norm_txt
        prev_syl_rate = cur_syl_rate

        if score >= 1.05:
            end_line = float(line.end)
            use_two_lines = (
                end_line - line.start < (2900.0 - 700.0 * pacing)
                and i + 1 < len(lines)
                and lines[i + 1].start - end_line < 1100.0
                and float(rng.random()) < (0.72 - 0.30 * pacing)
            )
            if use_two_lines:
                end_line = float(lines[i + 1].end)
            s_anchor = _snap_to_beat(_snap_to_accent(float(line.start), accents, 270.0), beats, 220.0)
            lead = beat_ms * float(rng.uniform(0.35, 0.85))
            s = s_anchor - lead
            min_phrase_w = quantize_beats((2900.0 - 900.0 * pacing) * float(rng.uniform(0.82, 1.18)), 3, 9)
            tail = beat_ms * float(rng.uniform(0.45, 1.05))
            e = max(s + min_phrase_w, end_line + tail)
            add_cand(1.15 + score, s, e, "transition" if chorus_return else "accent", gap_scale=0.88, rising=accent_hit or chorus_return)

    # Respaldo musical cuando solo hay letra (sin audio cargado) y ningún verso superó los rasgos expresivos previos
    if not candidates and analysis is None and span >= 2600.0:
        if lines and len(lines) >= 4 and span >= 14000.0:
            step = max(2, round(len(lines) / max(2.0, span / 11000.0)))
            offset_idx = int(rng.integers(0, min(step, len(lines))))
            for idx_ln in range(offset_idx, len(lines), step):
                ln = lines[idx_ln]
                w = quantize_beats(_clamp(float(ln.end - ln.start) + 900.0, min_cand_dur, 4600.0), 3, 10)
                add_cand(1.2, float(ln.start) - 250.0, float(ln.start) - 250.0 + w, "accent", gap_scale=0.95)
        else:
            dur = min(DEFAULT_INTERVAL_MS, 0.52 * span * float(rng.uniform(0.88, 1.10)))
            mid = t_min + float(rng.uniform(0.42, 0.58)) * span
            add_cand(1.0, mid - 0.5 * dur, mid + 0.5 * dur, "accent")

    if not candidates:
        return []

    # Selección dinámica estratificada con variación controlada y repulsión espacial (evita concentrar cortes)
    target_cov = _clamp(0.32 + 0.08 * pacing + float(rng.uniform(-0.045, 0.045)), 0.25, MAX_COVERAGE - 0.015)
    budget_ms = min(MAX_COVERAGE * span, max(min_cand_dur + 400.0, target_cov * span))
    hard_cap_ms = MAX_COVERAGE * span
    ideal_cycle_ms = _clamp(11500.0 - 4800.0 * pacing, 5200.0, 13000.0)

    scored_pool = [
        (base_sc + float(rng.normal(0.0, 0.34)), base_sc, _q10(s), _q10(e), kind, gap, rising)
        for base_sc, s, e, kind, gap, rising in candidates
        if _q10(e) - _q10(s) >= max(MIN_INTERVAL_MS, min_cand_dur - 40.0)
    ]

    chosen: list[tuple[float, float, str, float, bool]] = []  # (start_ms, end_ms, kind, local_gap, rising)
    used_ms = 0.0
    used_indices: set[int] = set()

    while True:
        best_idx = -1
        best_priority = -1e9
        for idx_c, (eff_sc, _base_sc, s, e, kind, gap, rising) in enumerate(scored_pool):
            if idx_c in used_indices:
                continue
            dur = e - s
            if chosen and (used_ms + dur > budget_ms):
                continue
            if not chosen and dur > hard_cap_ms and span >= 5000.0:
                continue
            conflict = False
            nearest_dist = span
            for cs, ce, _ck, cgap, _cr in chosen:
                req_gap = max(MIN_GAP_MS, min(gap, cgap))
                if not (e + req_gap <= cs or s >= ce + req_gap):
                    conflict = True
                    break
                d = (cs - e) if e <= cs else (s - ce)
                if d < nearest_dist:
                    nearest_dist = d
            if conflict:
                continue
            spacing_bonus = 0.90 * (1.0 - math.exp(-nearest_dist / ideal_cycle_ms)) if chosen else 0.0
            priority = eff_sc + spacing_bonus
            if priority > best_priority:
                best_priority = priority
                best_idx = idx_c

        if best_idx < 0:
            break
        used_indices.add(best_idx)
        _eff, _bsc, s, e, kind, gap, rising = scored_pool[best_idx]
        chosen.append((s, e, kind, gap, rising))
        used_ms += (e - s)
        if used_ms >= budget_ms * 0.96:
            break

    chosen.sort(key=lambda c: c[0])
    if not chosen:
        return []

    # Sincronización del video fuente con la progresión temporal de la canción + recorte inicial inteligente
    v_total = max(0.0, float(resource.duration_ms if (resource and resource.duration_ms > 0) else video_duration_ms))
    if resource is not None and resource.duration_ms > 0:
        v_first, v_last = resource.active_bounds_ms
        scene_cuts = [float(c) for c in resource.scene_cuts_ms if v_first <= c < v_last]
    else:
        eof_pad = 16.0 if v_total > 1500.0 else 0.0
        v_first, v_last = 0.0, max(0.0, v_total - eof_pad)
        scene_cuts = []

    inferred_song_end = max(
        t_max,
        float(song_duration_ms or 0.0),
        (analysis.duration_ms - audio_offset_ms) if (analysis is not None and analysis.duration_ms > 0) else 0.0,
        (float(max(l.end for l in lyrics.lines)) + 500.0) if lyrics.lines else 0.0,
    )
    song_t0 = 0.0 if t_min <= inferred_song_end else t_min
    song_span = max(span, inferred_song_end - song_t0)

    if opening_trim_ms is not None:
        open_ms = _clamp(float(opening_trim_ms), v_first, max(v_first, v_last - MIN_INTERVAL_MS))
    else:
        open_ms = compute_opening_trim_ms(v_total, song_span, resource=resource)
        open_ms = _clamp(open_ms, v_first, max(v_first, v_last - MIN_INTERVAL_MS))

    v_avail = max(0.0, v_last - open_ms)
    full_sync_mode = v_avail >= 0.65 * song_span

    base_tr = (copy.copy(treatment) if treatment else CinematicTreatment()).clamped()
    out: list[CinematicInterval] = []
    prev_vs: float | None = None

    for idx, (s, e, kind, _gap, rising) in enumerate(chosen):
        dur = _q10(e - s)
        e = _q10(s + dur)
        if v_total <= 0.0:
            vs, ve = 0.0, dur
        elif dur >= (v_last - v_first):
            vs, ve = _q10(v_first), _q10(max(v_first + 10.0, v_last))
        else:
            eff_open = open_ms if (v_last - open_ms) >= dur else max(v_first, v_last - dur)
            max_span_v = max(0.0, (v_last - eff_open) - dur)
            if full_sync_mode:
                # Bloqueado a la progresión de la pista musical (con micro-ajuste controlado coherente con los planos)
                u_song = clamp01((s - song_t0) / max(1.0, song_span - dur))
                v_sync = eff_open + u_song * max_span_v
                jitter = float(rng.uniform(-1.0, 1.0)) * min(1350.0, 0.032 * max(1.0, v_avail))
                vs = _clamp(v_sync + jitter, eff_open, v_last - dur)
                if prev_vs is not None:
                    min_fwd = min(v_last - dur, prev_vs + min(dur * 0.45, 500.0))
                    vs = max(vs, min_fwd)
            else:
                # Clip visual corto / B-roll: avanza siguiendo la progresión del tramo activo sin saltos erráticos
                u_clip = clamp01((s - t_min) / max(1.0, span - dur))
                jitter = float(rng.uniform(-0.10, 0.10)) * max_span_v
                vs = _clamp(eff_open + u_clip * max_span_v + jitter, eff_open, v_last - dur)
                if prev_vs is not None and prev_vs + 150.0 <= v_last - dur:
                    vs = max(vs, min(v_last - dur, prev_vs + 120.0))

            # Evita iniciar un recuerdo milisegundos antes de un corte brusco interno en el video fuente
            for cut_t in scene_cuts:
                if 25.0 < (cut_t - vs) < 380.0 and (cut_t + 40.0) <= (v_last - dur):
                    vs = cut_t + 40.0
                    break

            vs = _q10(_clamp(vs, v_first, max(v_first, v_last - dur)))
            ve = _q10(min(v_total, vs + dur))
            if ve <= vs:
                vs = _q10(max(0.0, min(v_total - 20.0, vs)))
                ve = _q10(min(v_total, vs + min(dur, v_total - vs)))
            prev_vs = vs

        # Fundidos dinámicos según el carácter musical del corte
        if kind in ("interlude", "pause"):
            fi_ratio = float(rng.uniform(0.24, 0.33))
            fo_ratio = float(rng.uniform(0.27, 0.36))
            fi_cap, fo_cap = 1350.0, 1500.0
        elif rising:
            fi_ratio = float(rng.uniform(0.13, 0.21))
            fo_ratio = float(rng.uniform(0.22, 0.30))
            fi_cap, fo_cap = 780.0, 1150.0
        else:
            fi_ratio = float(rng.uniform(0.18, 0.26))
            fo_ratio = float(rng.uniform(0.22, 0.31))
            fi_cap, fo_cap = 980.0, 1250.0

        fade_in = _q10(_clamp(fi_ratio * dur, 260.0, fi_cap))
        fade_out = _q10(_clamp(fo_ratio * dur, 320.0, fo_cap))
        if t_max - e <= 350.0:
            fade_out = _q10(_clamp(max(fade_out, 0.32 * dur), 450.0, min(1600.0, 0.55 * dur)))

        out.append(CinematicInterval(
            id=f"iv_{idx + 1}",
            start_ms=round(s, 1),
            end_ms=round(e, 1),
            video_start_ms=round(vs, 1),
            video_end_ms=round(ve, 1),
            fade_in_ms=fade_in,
            fade_out_ms=fade_out,
            treatment=copy.copy(base_tr),
            video_id=video_id or (resource.id if resource else ""),
            kind=kind,
        ))
    return out
