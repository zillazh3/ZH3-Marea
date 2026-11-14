"""Modelo cinemático (marea.cinematic v1): recursos de video, intervalos, estética de recuerdo y sugerencia musical.

Sin dependencias de Qt ni de Skia para que `ExportJob` cruce `multiprocessing` (`spawn`) sin problemas.
"""
from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field, fields
from typing import Callable

from marea.audio import analysis as _audio_analysis_mod
from marea.audio.analysis import _ANALYSIS_CACHE, AudioAnalysis, analyze_audio, cached_audio_analysis
from marea.engine.curves import clamp, clamp01, smoothstep

SCHEMA = "marea.cinematic"
VERSION = 1

MIN_INTERVAL_MS = 800.0
DEFAULT_INTERVAL_MS = 4500.0
MIN_GAP_MS = 1600.0
MAX_COVERAGE = 0.45


class CinematicError(ValueError):
    pass


@dataclass
class CinematicTreatment:
    """Parámetros estéticos de un recuerdo visual; priorizan que la letra blanca siga siendo legible."""
    opacity: float = 1.0
    blur: float = 0.35
    vignette: float = 0.45
    grain: float = 0.08
    saturation: float = 0.82
    contrast: float = 0.92
    dim: float = 0.32

    def clamped(self) -> "CinematicTreatment":
        return CinematicTreatment(
            opacity=clamp(self.opacity, 0.20, 1.00),
            blur=clamp(self.blur, 0.00, 1.00),
            vignette=clamp(self.vignette, 0.00, 1.00),
            grain=clamp(self.grain, 0.00, 0.35),
            saturation=clamp(self.saturation, 0.00, 1.40),
            contrast=clamp(self.contrast, 0.60, 1.30),
            dim=clamp(self.dim, 0.00, 0.75),
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
        v0 = clamp(float(self.visual_start_ms), 0.0, max(0.0, eof_safe - MIN_INTERVAL_MS))
        raw_v1 = float(self.visual_end_ms) if self.visual_end_ms > 0 else total
        v1 = clamp(raw_v1, min(eof_safe, v0 + MIN_INTERVAL_MS), eof_safe)
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
    kind: str = "manual"

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
            src = clamp(src, 0.0, float(max_duration_ms))
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
    envelope: float
    src_ms: float


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
            self.opening_trim_ms = round(clamp(self.opening_trim_ms, 0.0, max(0.0, max_ms - MIN_INTERVAL_MS)), 1)
        for iv in self.intervals:
            if not iv.video_id or iv.video_id == old_id:
                iv.video_id = resource.id
            if iv.video_id == resource.id and max_ms > 0:
                seg_len = max(MIN_INTERVAL_MS, iv.video_end_ms - iv.video_start_ms or iv.duration_ms)
                seg_len = min(seg_len, max_ms)
                iv.video_start_ms = clamp(iv.video_start_ms, 0.0, max(0.0, max_ms - seg_len))
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
            video_start_ms = clamp(video_start_ms, 0.0, max(0.0, max_v - seg))
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


def _iter_sorted_intervals(ivs: list[CinematicInterval]) -> list[CinematicInterval]:
    if len(ivs) > 1:
        prev = (ivs[0].start_ms, ivs[0].end_ms, ivs[0].id)
        for iv in ivs[1:]:
            cur = (iv.start_ms, iv.end_ms, iv.id)
            if cur < prev:
                return sorted(ivs, key=lambda x: (x.start_ms, x.end_ms, x.id))
            prev = cur
    return ivs


def evaluate_at(config: CinematicConfig | None, t_ms: float) -> list[ActiveInterval]:
    """Intervalos activos en `t_ms` (orden cronológico; máximo 2 si el usuario solapó dos recuerdos)."""
    if config is None or not config.enabled or not config.intervals or not config.resources:
        return []
    active: list[ActiveInterval] = []
    for iv in _iter_sorted_intervals(config.intervals):
        if iv.start_ms >= t_ms:
            break
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


MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


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


from marea.model.cinematic_suggest import compute_opening_trim_ms, song_fingerprint, suggest_intervals  # noqa: E402, F401


def __getattr__(name: str):
    if name == "_analysis_computations":
        return _audio_analysis_mod._analysis_computations
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
