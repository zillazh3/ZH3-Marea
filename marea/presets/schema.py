"""Corrientes (marea.preset v1): formato, validación y aplicación de looks guardados.

Módulo puro (sin Qt; solo usa los dataclasses de estilo): un preset es un JSON con secciones; solo las claves de la lista blanca `FIELDS` se leen,
con tipo y rango comprobados. Nunca se ejecuta ni se resuelve nada que venga del archivo: ni rutas, ni fuentes por ruta.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from marea.engine.engine import Params
from marea.engine.motion import MODES as MOTION_MODES
from marea.i18n import t
from marea.i18n import Labels
from marea.model.background import FITS, LOOPS, BackgroundConfig
from marea.render.scene import Style

SCHEMA = "marea.preset"
VERSION = 1
MAX_BYTES = 256 * 1024
BUNDLED_FONTS = ("", "Inter Tight")  # "" = la fuente incluida
SECTIONS = ("tipografia", "color", "fondo", "efectos", "marca_de_agua")
# section -> i18n key; the Mapping resolves it with t() on every read, so the UI keeps indexing it as before
SECTION_LABELS = Labels({"tipografia": "presets.section.tipografia", "color": "presets.section.color", "fondo": "presets.section.fondo",
                         "efectos": "presets.section.efectos", "marca_de_agua": "presets.section.marcaDeAgua"})


class PresetError(ValueError):
    pass


@dataclass
class FxSettings:
    """Ajustes de efectos por palabra que viven en los controles del editor (no en Style ni en Params)."""
    fx_intensity: float = 1.0
    adapt_bg: bool = True


# clave -> (sección, destino, tipo, extra). destino: style | params | bg | fx. El nombre del atributo es la clave.
#   num: (lo, hi) · bool · choice: tuple · str: largo máximo · rgb · rgb_or_none
FIELDS: dict[str, tuple[str, str, str, object]] = {
    "font_family": ("tipografia", "style", "font", 80),
    "font_frac": ("tipografia", "style", "num", (0.04, 0.10)),
    "text_rgb": ("color", "style", "rgb", None),
    "glow_rgb": ("color", "style", "rgb_or_none", None),
    "glow_gain": ("color", "style", "num", (0.0, 3.0)),
    "bloom": ("color", "style", "bool", None),
    "bloom_gain": ("color", "style", "num", (0.0, 3.0)),
    "dim_alpha": ("color", "style", "num", (0.05, 0.35)),
    "source": ("fondo", "bg", "choice", ("cover", "video", "transparent")),
    "bg_blur": ("fondo", "style", "num", (0.2, 3.0)),
    "bg_veil": ("fondo", "style", "num", (0.0, 0.8)),
    "bg_sat": ("fondo", "style", "num", (0.0, 2.0)),
    "bg_speed": ("fondo", "style", "num", (0.0, 3.0)),
    "loop": ("fondo", "bg", "choice", LOOPS),
    "fit": ("fondo", "bg", "choice", FITS),
    "zoom": ("fondo", "bg", "num", (1.0, 3.0)),
    "pan_x": ("fondo", "bg", "num", (-1.0, 1.0)),
    "pan_y": ("fondo", "bg", "num", (-1.0, 1.0)),
    "dim": ("fondo", "bg", "num", (0.0, 0.85)),
    "blur": ("fondo", "bg", "num", (0.0, 1.0)),
    "saturation": ("fondo", "bg", "num", (0.0, 1.6)),
    "scrim": ("fondo", "bg", "num", (0.0, 1.0)),
    "cover_tint": ("fondo", "bg", "num", (0.0, 1.0)),
    "fx_intensity": ("efectos", "fx", "num", (0.0, 1.5)),
    "adapt_bg": ("efectos", "fx", "bool", None),
    "motion": ("efectos", "params", "choice", tuple(MOTION_MODES)),
    "motion_amt": ("efectos", "params", "num", (0.0, 1.5)),
    "blurm": ("efectos", "params", "num", (0.0, 2.0)),
    "wob": ("efectos", "params", "num", (0.25, 1.0)),
    "vib": ("efectos", "params", "bool", None),
    "emph": ("efectos", "params", "bool", None),
    "wm_text": ("marca_de_agua", "style", "str", 60),
    "wm_side": ("marca_de_agua", "style", "choice", ("left", "right")),
    "wm_glyph": ("marca_de_agua", "style", "bool", None),
    "wm_alpha": ("marca_de_agua", "style", "num", (0.0, 1.0)),
}
# Rutas, archivos de video y recorte/desfase del video NO están en la lista: dependen del archivo, no del look.

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _rgb_to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _hex_to_rgb(s) -> tuple[int, int, int] | None:
    m = _HEX.match(s) if isinstance(s, str) else None
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else None


def _clean(kind: str, extra, v, default):
    """Valor ya validado y limitado, o `default` si el tipo no sirve."""
    if kind == "num":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return default
        lo, hi = extra
        return float(min(hi, max(lo, v)))
    if kind == "bool":
        return v if isinstance(v, bool) else default
    if kind == "choice":
        return v if isinstance(v, str) and v in extra else default
    if kind in ("str", "font"):
        return v.strip()[:extra] if isinstance(v, str) else default
    if kind == "rgb":
        return _hex_to_rgb(v) or default
    if kind == "rgb_or_none":
        return None if v is None else (_hex_to_rgb(v) or default)
    return default


def _defaults() -> dict[str, object]:
    st, pa, bg, fx = Style(), Params(), BackgroundConfig(), FxSettings()
    return {k: getattr(dict(style=st, params=pa, bg=bg, fx=fx)[tgt], k) for k, (_, tgt, _, _) in FIELDS.items()}


def _encode(kind: str, v):
    if kind in ("rgb", "rgb_or_none"):
        return None if v is None else _rgb_to_hex(v)
    return v


def capture(style: Style, params: Params, bg: BackgroundConfig, fx: FxSettings, name: str = "", description: str = "") -> dict:
    """Preset completo (todas las claves de la lista blanca) con el look actual."""
    src = dict(style=style, params=params, bg=bg, fx=fx)
    now = datetime.now().isoformat(timespec="seconds")
    out: dict = {"schema": SCHEMA, "version": VERSION, "name": name, "description": description, "created": now, "updated": now}
    for sec in SECTIONS:
        out[sec] = {}
    for key, (sec, tgt, kind, _) in FIELDS.items():
        out[sec][key] = _encode(kind, getattr(src[tgt], key))
    return out


def current_values(style: Style, params: Params, bg: BackgroundConfig, fx: FxSettings) -> dict[str, dict]:
    """Solo las secciones del look actual (sin metadatos), para comparar con un preset."""
    p = capture(style, params, bg, fx)
    return {sec: p[sec] for sec in SECTIONS}


def apply(preset: dict, style: Style, params: Params, bg: BackgroundConfig, fx: FxSettings,
          sections=SECTIONS) -> tuple[Style, Params, BackgroundConfig, FxSettings]:
    """Copias con las secciones elegidas aplicadas; solo toca las claves que el preset trae. Las entradas no se modifican."""
    style, params, bg, fx = copy.copy(style), copy.copy(params), copy.copy(bg), copy.copy(fx)
    dst = dict(style=style, params=params, bg=bg, fx=fx)
    for key, (sec, tgt, kind, extra) in FIELDS.items():
        if sec not in sections or key not in (preset.get(sec) or {}):
            continue
        v = preset[sec][key]
        setattr(dst[tgt], key, _hex_to_rgb(v) if kind in ("rgb", "rgb_or_none") and isinstance(v, str) else v)
    if bg.source == "video" and bg.video is None:  # la Corriente nunca trae el archivo: sin video propio, queda la portada
        bg.source = "cover"
    return style, params, bg, fx


MIGRATIONS: dict[int, Callable[[dict], dict]] = {}  # v -> transformador a v+1 cuando suba VERSION


def migrate(raw: dict) -> dict:
    try:
        ver = int(raw.get("version", 1))
    except (TypeError, ValueError):
        raise PresetError(t("errors.preset.badVersion")) from None
    if ver < 1 or ver > VERSION:
        raise PresetError(t("errors.preset.unsupportedVersion", ver=ver, max=VERSION))
    cur = dict(raw)
    while ver < VERSION:
        cur = MIGRATIONS[ver](cur)
        ver = int(cur.get("version", ver + 1))
    return cur


@dataclass
class Validated:
    preset: dict
    flags: list[str]  # avisos para el usuario (p. ej. fuente que no está disponible)


def validate(raw, installed_fonts=None) -> Validated:
    """Normaliza un preset leído de un archivo: ignora claves desconocidas, limita rangos, corrige tipos.
    `installed_fonts`: familias instaladas en este equipo (además de la incluida); None = solo la incluida."""
    if not isinstance(raw, dict):
        raise PresetError(t("errors.preset.notObject"))
    if raw.get("schema") != SCHEMA:
        raise PresetError(t("errors.preset.unsupportedFormat", schema=repr(raw.get("schema")), expected=repr(SCHEMA)))
    d = migrate(raw)
    defaults, flags = _defaults(), []
    name = d.get("name") if isinstance(d.get("name"), str) else ""
    desc = d.get("description") if isinstance(d.get("description"), str) else ""
    now = datetime.now().isoformat(timespec="seconds")
    out: dict = {"schema": SCHEMA, "version": VERSION, "name": name.strip()[:60], "description": desc.strip()[:300],
                 "created": d["created"] if isinstance(d.get("created"), str) else now,
                 "updated": d["updated"] if isinstance(d.get("updated"), str) else now}
    allowed = set(BUNDLED_FONTS) | set(installed_fonts or ())
    for sec in SECTIONS:
        src = d.get(sec)
        out[sec] = {}
        if not isinstance(src, dict):
            continue
        for key, (s, _tgt, kind, extra) in FIELDS.items():
            if s != sec or key not in src:
                continue
            v = _clean(kind, extra, src[key], defaults[key])
            if kind in ("rgb", "rgb_or_none"):
                v = _encode(kind, v)
            if kind == "font" and v not in allowed:
                flags.append(t("errors.preset.fontMissing", font=v))
                v = ""
            out[sec][key] = v
    return Validated(out, flags)


def from_state(state: dict) -> dict:
    """Preset parcial (solo `style`, `params` y `fx` del estado del editor guardado en el proyecto), con tipos y rangos comprobados.
    Lo que no esté en el estado queda fuera: aplicar el resultado solo toca lo que el proyecto sí recordaba."""
    groups = {"style": state.get("style"), "params": state.get("params"), "fx": state.get("fx")}
    defaults, out = _defaults(), {s: {} for s in SECTIONS}
    for key, (sec, tgt, kind, extra) in FIELDS.items():
        src = groups.get(tgt)
        if not isinstance(src, dict) or key not in src:
            continue
        v = src[key]
        if kind in ("rgb", "rgb_or_none") and isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(c, int) for c in v):
            v = _rgb_to_hex([max(0, min(255, c)) for c in v])
        v = _clean(kind, extra, v, defaults[key])
        if kind == "font" and v not in BUNDLED_FONTS:
            pass  # una fuente instalada se valida contra la lista de familias en la UI (como hace la restauración de la fuente)
        out[sec][key] = _encode(kind, v)
    return out


def changed_sections(preset: dict, current: dict[str, dict], sections=SECTIONS) -> list[str]:
    """Secciones donde el look actual difiere del preset (solo se comparan las claves que el preset trae)."""
    diff = []
    for sec in sections:
        for key, v in (preset.get(sec) or {}).items():
            cur = current.get(sec, {}).get(key)
            same = abs(cur - v) < 1e-6 if isinstance(v, float) and isinstance(cur, (int, float)) and not isinstance(cur, bool) else cur == v
            if not same:
                diff.append(sec)
                break
    return diff
