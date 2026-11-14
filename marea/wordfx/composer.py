"""Compositor: mezcla el vector de emoción de UNA aparición de palabra (categorías propias, vecinas, verso, canción) y lo
traduce a `FxParams`. Puro y determinista: toda la variación sale de `seed_for` (sha1), nunca de `random`/`hash()`/reloj.
"""
import colorsys
import hashlib
from collections.abc import Mapping, Sequence

from marea.wordfx._math import MAX_AMBIENT_AMP, ambient_vector_color, clamp as _clamp, lerp_rgb as _mix, luma
from marea.wordfx.envelope import duration_mode
from marea.wordfx.fxtypes import AXES, RGB, FxParams
from marea.wordfx.library import AXES as RANGES
from marea.wordfx.library import Library
from marea.wordfx.matcher import Match
from marea.wordfx.signatures import CUSTOM_PARTICLE_KINDS, signature_for

Vector = tuple[float, ...]  # en el orden de fxtypes.AXES
EPS = 1e-6
GRAY: RGB = (154, 154, 154)
# (valencia, temperatura, color): puntos del plano sobre los que se interpola el color
ANCHORS = (
    (-0.7, -0.6, (0x5B, 0x7A, 0x99)),  # negativo + frío: azul acero
    (-0.7, 0.6, (0x8B, 0x2E, 0x3C)),  # negativo + cálido: carmesí oscuro
    (0.7, -0.7, (0x9F, 0xD8, 0xE0)),  # positivo + frío: cian suave
    (0.4, -0.2, (0xB9, 0xA7, 0xE8)),  # lavanda
    (0.6, 0.4, (0xFF, 0xC5, 0x6B)),  # positivo + cálido: ámbar
    (0.8, 0.9, (0xFF, 0x8F, 0xA3)),  # rosa cálido
    (0.0, 0.0, GRAY),  # neutro / entumecido
)
# tope |valor| de cada campo de amplitud (los 0..1 no pasan de 1; el desenfoque, de ~4 px a 42 px de letra)
LIMITS = {"drift_em": 0.45, "jitter_em": 0.08, "wave_em": 0.15, "spacing_em": 0.2, "scale_amp": 0.2, "rotation_deg": 20.0,
          "letter_offset_em": 0.6, "blur_em": 0.1, "chromatic_em": 0.05, "glow": 1.0, "grain": 1.0, "trail": 1.0,
          "erode": 1.0, "fracture": 1.0}

MIN_LUMA = 0.6  # la letra es blanca sobre una portada difuminada: un color más oscuro que esto se pierde junto a las vecinas
POSITIVE_LUMA = 0.22  # lo positivo/colorido (estrellas, brillo) sube el mínimo hasta MIN_LUMA + esto: más luz para que no se opaque
AUTHORED_WEIGHT = 0.92  # un color de autor se mezcla con la portada mucho menos que uno derivado del vector (0.6)
VIBRANCE = 1.35

# Compound category color tints applied when no contrast partner is present:
# (required categories, from_target, from_weight, to_target, to_weight, apply_legible)
_COLOR_COMBOS: tuple[tuple[frozenset[str], RGB, float, RGB, float, bool], ...] = (
    (frozenset({"sadness", "anger"}), (217, 43, 75), 0.72, (110, 18, 36), 0.78, False),
    (frozenset({"sadness", "healing"}), (230, 194, 122), 0.65, (143, 168, 184), 0.50, False),
    (frozenset({"memory", "craft"}), (228, 178, 96), 0.62, (192, 152, 108), 0.50, False),
    (frozenset({"celestial", "shadow"}), (178, 166, 238), 0.58, (142, 132, 204), 0.62, True),
    (frozenset({"growth", "fire"}), (255, 180, 36), 0.80, (255, 122, 18), 0.85, False),
)


def _u(seed: int, key: str) -> float:
    """Uniforme estable en [0, 1) para (seed, key)."""
    return int.from_bytes(hashlib.sha1(f"{seed}:{key}".encode()).digest()[:4], "big") / 2**32


def seed_for(track_id: str, line: int, word: int, repeat_n: int = 0) -> int:
    """Deterministic 64-bit integer seed for `(track_id, line, word, repeat_n)`."""
    raw = f"{track_id}:{line}:{word}" if repeat_n == 0 else f"{track_id}:{line}:{word}:{repeat_n}"
    return int.from_bytes(hashlib.sha1(raw.encode()).digest()[:8], "big")


def blend_categories(cats: Sequence[str], lib: Library) -> Vector:
    """Media ponderada de los vectores; peso = prioridad x 1.5 si es emoción (las emociones pesan más que los elementos)."""
    ws = [(max(EPS, lib.categories[c].priority) * (1.5 if lib.categories[c].kind == "emotion" else 1.0), lib.categories[c])
          for c in cats if c in lib.categories]
    tot = sum(w for w, _ in ws) or 1.0
    return tuple(sum(w * c.vector[a] for w, c in ws) / tot for a in AXES)


def anchor_color(valence: float, temperature: float, arousal: float) -> RGB:
    """Color por interpolación inversa a la distancia² entre anclas; la arousal baja pulla a gris y la valencia da brillo."""
    ws = [(1.0 / ((valence - v) ** 2 + (temperature - t) ** 2 + 1e-4), rgb) for v, t, rgb in ANCHORS]
    tot = sum(w for w, _ in ws)
    r, g, b = (sum(w * rgb[i] for w, rgb in ws) / tot / 255 for i in range(3))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    s *= 0.45 + 0.55 * _clamp(arousal, 0, 1)
    v = min(1.0, v * (0.85 + 0.15 * _clamp(valence, -1, 1)))
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))


def blend_palette(rgb: RGB, palette: Sequence[RGB], weight: float = 0.6) -> RGB:
    """`weight` de `rgb` + el resto del color medio de la paleta (dominantes de la carátula). Paleta vacía: sin cambio."""
    if not palette:
        return rgb
    mean = tuple(sum(c[i] for c in palette) / len(palette) for i in range(3))
    return tuple(round(weight * a + (1 - weight) * b) for a, b in zip(rgb, mean))


def _legible(rgb: RGB, floor: float = MIN_LUMA) -> RGB:
    """Sube el color hacia el blanco, sin cambiar su tono, hasta una luma mínima (las anclas oscuras como #5B7A99 se perderían)."""
    y = luma(rgb)
    return rgb if y >= floor else _mix(rgb, (255, 255, 255), (floor - y) / (1 - y))


def _floor(valence: float) -> float:
    return MIN_LUMA + POSITIVE_LUMA * _clamp(valence, 0.0, 1.0)


def _authored(cats: Sequence[str], lib: Library) -> tuple[RGB, RGB] | None:
    """Color de autor (primera y última letra) de la categoría de más prioridad que lo defina; `cats` ya viene por prioridad."""
    for c in cats:
        col = lib.categories[c].color if c in lib.categories else ()
        if col:
            return col[0], col[-1]
    return None


def _finish(rgb: RGB, vivid: bool, valence: float = 0.0) -> RGB:
    """Vibrancia (sube la saturación x1.35 salvo en «desaturate», arousal baja o color de autor) y luego la luma mínima."""
    if vivid:
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, min(1.0, s * VIBRANCE), v))
    return _legible(rgb, _floor(valence))


def _hue(rgb: RGB, deg: float) -> RGB:
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb((h + deg / 360) % 1.0, s, v))


def _primitives(match: Match, lib: Library, vec: Vector, repeat_n: int, seed: int) -> tuple[list[str], float]:
    """Primitivos activos (categorías + implícitos + repetición + variación por seed) y el extra de ascenso por repetición."""
    prims: list[str] = []

    def add(p: str):
        if p not in prims:
            prims.append(p)

    for c in match.categories:
        for p in lib.categories[c].primitives if c in lib.categories else ():
            add(p)
    if "mask_reveal" in prims:
        add("letter_offset")  # las letras se revelan entrando desde abajo (la biblioteca solo pide mask_reveal en «love»)
        if abs(vec[2]) >= 0.28 and not {"displacement", "gravity_drift"} & set(prims):
            add("displacement")  # el asiento de la palabra sigue la gravedad del vector
    rise = 0.0
    if repeat_n >= 1:
        if _u(seed, "rep") < 0.5:
            add("scale")
        else:
            add("displacement")
            rise = 0.07
    if repeat_n >= 2:
        add("sparkle")
    if _u(seed, "extra") < 0.4:  # una pieza extra compatible con el vector: dos apariciones de la misma palabra difieren
        pool = [p for p, ok in (("glow", vec[0] > 0), ("jitter", vec[1] > 0.5), ("trail", vec[5] > 0.3)) if ok and p not in prims]
        if pool:
            add(pool[int(_u(seed, "pick") * len(pool))])
    return prims, rise


def _blend_word_vector(
    match: Match,
    lib: Library,
    bleed_donors: Sequence[tuple[Match, float]] | None,
    neighbors: Sequence[tuple[Match, int]],
    bleed: float,
    line_mood: Vector | None,
    song_mood: Vector | None,
    vector_override: Mapping[str, float] | None,
) -> tuple[Vector, float, str, Match | None]:
    if bleed_donors is not None and bleed > 0:
        weighted_near = [(m, float(w)) for m, w in bleed_donors if w > 0.0]
        parts = [(1.0, blend_categories(match.categories, lib))] + [(w, blend_categories(m.categories, lib)) for m, w in weighted_near]
        bleed_total = round(sum(w for _, w in weighted_near), 6)
        bleed_from = ",".join(m.lemma for m, _ in weighted_near)
        strongest_donor = max(weighted_near, key=lambda x: x[1])[0] if weighted_near else None
    else:
        near = [(m, d) for m, d in neighbors if d <= 2 and bleed > 0]
        parts = [(1.0, blend_categories(match.categories, lib))] + [(bleed, blend_categories(m.categories, lib)) for m, _ in near]
        bleed_total = round(bleed * len(near), 6)
        bleed_from = ",".join(m.lemma for m, _ in near)
        strongest_donor = min(near, key=lambda x: x[1])[0] if near else None
    parts += [(0.15, v) for v in (line_mood, song_mood) if v is not None]
    tot = sum(w for w, _ in parts)
    vec = tuple(_clamp(sum(w * v[i] for w, v in parts) / tot, *RANGES[a]) for i, a in enumerate(AXES))
    if vector_override:
        if bad := set(vector_override) - set(AXES):
            raise ValueError(f"vector_override: ejes desconocidos {sorted(bad)}")
        vec = tuple(_clamp(vector_override[a], *RANGES[a]) if a in vector_override else x for a, x in zip(AXES, vec))
    return vec, bleed_total, bleed_from, strongest_donor


def _apply_raw_vocals(
    match: Match, lib: Library, on: set[str], prims: list[str], raw_vocals: bool, intensity: float, aro: float,
) -> tuple[float, bool]:
    if not raw_vocals or intensity <= 0:
        return 0.0, False
    cats = [lib.categories[c] for c in match.categories if c in lib.categories]
    strain = 0.0
    if {"anguish", "fear"} & set(match.categories) or on & {"jitter", "chromatic"}:
        strain = 0.85
    elif aro >= 0.5 and any(c.kind == "emotion" for c in cats):
        strain = 0.5
    forced = False
    if strain and not on & {"jitter", "chromatic"}:  # que haya algo que temblar
        prims += ["chromatic", "jitter"]
        on |= {"chromatic", "jitter"}
        forced = True
    return strain, forced


def _category_base_color(cats: Sequence[str], lib: Library, palette: Sequence[RGB], desaturate: bool) -> RGB:
    """Compute finished base color for a single match/partner category list."""
    v = blend_categories(cats, lib)
    auth = _authored(cats, lib)
    base = auth[0] if auth else anchor_color(v[0], v[3], v[1])
    if desaturate and not auth:
        base = _mix(base, GRAY, 0.2)
    blended = blend_palette(base, palette, AUTHORED_WEIGHT if auth else 0.6)
    return _finish(blended, not auth and not desaturate and v[1] >= 0.15, v[0])


def _resolve_word_colors(
    match: Match,
    lib: Library,
    on: set[str],
    palette: Sequence[RGB],
    val: float,
    aro: float,
    tem: float,
    strongest_donor: Match | None,
    bleed_total: float,
    partner: Match | None,
) -> tuple[RGB, RGB, RGB | None]:
    auth = _authored(match.categories, lib)
    base = auth[0] if auth else anchor_color(val, tem, aro)
    if "desaturate" in on and not auth:
        base = _mix(base, GRAY, 0.2)
    c_from = blend_palette(base, palette, AUTHORED_WEIGHT if auth else 0.6)
    c_to = blend_palette(auth[1], palette, AUTHORED_WEIGHT) if auth else c_from
    if strongest_donor is not None:
        nv = blend_categories(strongest_donor.categories, lib)
        nauth = _authored(strongest_donor.categories, lib)
        n_col = blend_palette(nauth[0], palette, AUTHORED_WEIGHT) if nauth else blend_palette(anchor_color(nv[0], nv[3], nv[1]), palette, 0.6)
        c_to = _mix(c_to, n_col, min(1.0, 2 * bleed_total))
    if not auth:
        if "desaturate" in on:
            c_to = _mix(c_to, GRAY, 0.5)
        if "hue_shift" in on:
            c_to = _hue(c_to, -25.0 if tem >= 0 else 25.0)
    vivid = not auth and "desaturate" not in on and aro >= 0.15
    color_from, color_to = _finish(c_from, vivid, val), _finish(c_to, vivid, val)
    if partner is not None:
        pdes = any("desaturate" in lib.categories[c].primitives for c in partner.categories if c in lib.categories)
        return color_from, color_to, _category_base_color(partner.categories, lib, palette, pdes)
    cats_set = set(match.categories)
    for req, cf_tgt, w_from, ct_tgt, w_to, keep_legible in _COLOR_COMBOS:
        if req <= cats_set:
            color_from = _mix(color_from, cf_tgt, w_from)
            color_to = _mix(color_to, ct_tgt, w_to)
            if keep_legible:
                color_from = _legible(color_from)
                color_to = _legible(color_to)
            break
    return color_from, color_to, None


def _build_amplitudes(
    on: set[str], match: Match, val: float, aro: float, gra: float, tem: float, fra: float, blu: float,
    hold_s: float, rise: float, raw_vocals: bool, intensity: float, repeat_n: int, base_seed: int,
) -> dict[str, float]:
    hf = _clamp(hold_s / 1.2, 0.3, 1.5)  # shortcut: erosión/partículas calibradas con «perdido» a 1.2 s
    amp: dict[str, float] = {}
    if on & {"displacement", "gravity_drift"}:
        amp["drift_em"] = gra * 0.26 * (1 + 0.3 * _clamp(hold_s / 2.4, 0, 1)) - rise
    if "letter_offset" in on:
        amp["letter_offset_em"] = 0.15 + 0.25 * fra
    if "scale" in on:
        amp["scale_amp"] = 0.04 + 0.08 * aro
    if "rotation" in on:
        amp["rotation_deg"] = 3 + 7 * aro
    if "blur" in on:
        amp["blur_em"] = 0.12 * blu
    if "letter_spacing" in on:
        amp["spacing_em"] = 0.06 * val - 0.1 * gra
    if "jitter" in on:
        amp["jitter_em"] = 0.01 + 0.035 * aro
    if "wave" in on:
        amp["wave_em"] = 0.03 + 0.06 * aro
    if "glow" in on:
        amp["glow"] = _clamp(0.45 + 0.45 * val + 0.4 * _clamp((tem - 0.5) / 0.4, 0.0, 1.0), 0.15, 1.0)
    if "chromatic" in on:
        amp["chromatic_em"] = 0.006 + 0.02 * aro
    if "grain" in on:
        amp["grain"] = 0.2 + 0.5 * fra
    if "trail" in on:
        amp["trail"] = 0.15 + blu
    if "erode" in on:
        amp["erode"] = 0.4 * fra * hf
    if "fracture" in on:
        amp["fracture"] = 0.6 * fra * hf
    if raw_vocals and "anguish" in match.categories:
        for key in ("jitter_em", "chromatic_em"):
            if key in amp:
                amp[key] *= 1.2
    k = intensity * min(2.0, 1 + 0.25 * repeat_n) * (0.85 + 0.3 * _u(base_seed, "mag"))
    return {key: _clamp(v * k, -LIMITS[key], LIMITS[key]) for key, v in amp.items()}, k  # type: ignore[return-value]


def compose(match: Match, lib: Library, *, track_id: str = "", neighbors: Sequence[tuple[Match, int]] = (),
            line_mood: Vector | None = None, song_mood: Vector | None = None, palette: Sequence[RGB] = (),
            repeat_n: int = 0, intensity: float = 1.0, bleed: float = 0.3, beat_period_ms: float | None = None,
            partner: Match | None = None, osc_phase: float = 0.0, vector_override: Mapping[str, float] | None = None,
            raw_vocals: bool = False, bleed_donors: Sequence[tuple[Match, float]] | None = None,
            is_bg: bool = False, handoff_overlap_s: float = 0.32) -> FxParams:
    """Parámetros de una aparición de `match`. `neighbors` = (palabra con efecto del mismo verso, distancia en tokens)."""
    base_seed = seed_for(track_id, match.line, match.word, 0)
    seed = seed_for(track_id, match.line, match.word, repeat_n)
    intensity = _clamp(intensity, 0.0, 1.5) * (0.40 if is_bg else 1.0)
    vec, bleed_total, bleed_from, strongest_donor = _blend_word_vector(
        match, lib, bleed_donors, neighbors, bleed, line_mood, song_mood, vector_override,
    )
    val, aro, gra, tem, fra, blu = vec
    hold_s = max(0.0, (match.end_ms - match.begin_ms) / 1000)
    prims, rise = _primitives(match, lib, vec, repeat_n, base_seed)
    on = set(prims)
    strain, forced = _apply_raw_vocals(match, lib, on, prims, raw_vocals, intensity, aro)

    c_from, c_to, osc_col = _resolve_word_colors(
        match, lib, on, palette, val, aro, tem, strongest_donor, bleed_total, partner,
    )
    meta = dict(seed=seed, vector=vec, bleed=bleed_total, bleed_from=bleed_from, repeat_n=repeat_n, hold_s=hold_s,
                color_from=c_from, color_to=c_to, note=match.note)
    if osc_col is not None:
        meta["osc_color"] = osc_col
    if intensity <= 0:
        return FxParams(primitives=(), **meta)

    hf = _clamp(hold_s / 1.2, 0.3, 1.5)
    f, k = _build_amplitudes(
        on, match, val, aro, gra, tem, fra, blu, hold_s, rise, raw_vocals, intensity, repeat_n, base_seed,
    )
    if forced:
        f["chromatic_em"] = max(f.get("chromatic_em", 0.0), 0.02)
        f["jitter_em"] = max(f.get("jitter_em", 0.0), 0.01)
    if "particles" in on:
        f["particles"] = round(_clamp(30 * fra * hf * k, 0, 28))
    if "sparkle" in on:
        f["sparkle"] = round(_clamp((4 + max(0, repeat_n - 2) + int(_u(seed, "spk") * 4)) * intensity, 4, 9))
    if "opacity" in on:
        f["opacity_min"] = 1 - _clamp((0.15 + 0.5 * fra) * k, 0, 0.9)
    if "jitter" in on:
        f["jitter_hz"] = 4 + 10 * aro
    if "wave" in on:
        f["wave_hz"] = 0.6 + 2.4 * aro
    if "scale" in on:
        f["scale_period_s"] = 2.2 - 1.2 * aro
    if on & {"letter_offset", "mask_reveal"}:
        f["stagger_ms"] = _clamp(beat_period_ms / 4, 40, 140) if beat_period_ms else _clamp(140 - 220 * aro, 40, 140)
    f["glow"] = max(f.get("glow", 0.0), (0.3 + 0.2 * _clamp(val, 0.0, 1.0)) * intensity)
    f["flash"] = (0.45 + 0.3 * _clamp(val, 0.0, 1.0) + (0.2 if on & {"sparkle", "glow"} else 0.0)) * intensity
    if on & {"jitter", "glow"}:
        f["audio_gain"] = 0.3 + 0.5 * aro
    if strain:
        f["strain_gain"] = min(1.0, strain * intensity)
        f["audio_gain"] = max(f.get("audio_gain", 0.0), 0.4)
    if partner:
        f.update(osc_hz=2.0, osc_phase=osc_phase, osc_jitter_em=0.03 * intensity)
        f["audio_gain"] = max(f.get("audio_gain", 0.0), 0.3)

    d_mode, _ = duration_mode(hold_s)
    sig = signature_for(match.categories, vec)
    p_kind, p_max = sig.particle_kind, sig.particle_max
    amb = min(1.0, max(0.0, getattr(match, "ambiguity", 0.0)))
    calm_damp = 0.72 + 0.28 * _clamp(aro / 0.35, 0.0, 1.0)
    amb_damp = 1.0 - 0.15 * amb
    sig_waveform, sig_axis = sig.waveform, sig.axis
    sig_freq = sig.freq_range_hz[0] + (sig.freq_range_hz[1] - sig.freq_range_hz[0]) * aro
    sig_amp = (sig.amp_range_em[0] + (sig.amp_range_em[1] - sig.amp_range_em[0]) * max(aro, fra)) * min(1.5, intensity)
    release_s, residue_level, zeta = sig.release_ms / 1000.0, sig.residue_level, sig.zeta
    glow_spread, glow_color, sparkle_style = sig.glow_spread, sig.glow_color, sig.sparkle_style

    cats_set = set(match.categories)
    if {"sadness", "water"} <= cats_set:
        p_kind, p_max = "tear", max(p_max, 14)
    elif {"sadness", "anger"} <= cats_set:
        p_kind, p_max = "crimson", max(p_max, 14)
        sig_waveform, sig_axis = "breath", "scale"
        sig_freq = 1.05 + 0.35 * aro
        sig_amp = max(sig_amp, 0.024 * min(1.5, intensity))
        f["fracture"] = max(f.get("fracture", 0.0), 0.42 * min(1.3, intensity))
        f["chromatic_em"] = max(f.get("chromatic_em", 0.0), 0.014 * min(1.3, intensity))
    elif {"sadness", "healing"} <= cats_set:
        p_kind, p_max = "wisp", max(p_max, 8)
        f["fracture"] = max(f.get("fracture", 0.0), 0.16 * min(1.2, intensity))
        if d_mode != "accent":
            f["sparkle"] = max(f.get("sparkle", 0), 4)
    elif {"memory", "craft"} <= cats_set:
        release_s = max(release_s, 0.92)
        residue_level = max(residue_level, 0.28)
        glow_color = (236, 194, 118)
    elif {"celestial", "shadow"} <= cats_set:
        sig_waveform, sig_axis = "orbit", "xy"
        f["spacing_em"] = _clamp(min(f.get("spacing_em", 0.0), -0.035 * min(1.3, intensity)), -LIMITS["spacing_em"], LIMITS["spacing_em"])
        glow_spread, glow_color = max(glow_spread, 0.35), (128, 116, 206)
    elif {"growth", "fire"} <= cats_set:
        p_kind, p_max, sparkle_style = "ember", max(p_max, 18), "ember"
        sig_waveform, sig_axis = "flicker", "y"
        glow_spread, glow_color = max(glow_spread, 0.8), (255, 146, 48)

    if d_mode == "accent":
        f["particles"] = 0
        f["sparkle"] = 0
    elif "particles" in on and p_kind in CUSTOM_PARTICLE_KINDS and intensity > 0.0:
        scaled_pmax = p_max * min(1.5, intensity) * calm_damp * amb_damp
        f["particles"] = round(_clamp(max(f.get("particles", 0) * amb_damp, scaled_pmax), 0, 28))

    donor_b0 = (strongest_donor.begin_ms / 1000.0) if strongest_donor is not None else -1.0
    donor_b1 = (strongest_donor.end_ms / 1000.0) if strongest_donor is not None else -1.0
    f.update(
        release_s=release_s, residue_level=residue_level, zeta=zeta, is_hero=not is_bg, is_bg=is_bg,
        tier="support" if is_bg else "hero", bleed_donor_s=donor_b0, bleed_donor_end_s=donor_b1,
        handoff_overlap_s=handoff_overlap_s, duration_mode=d_mode, waveform=sig_waveform, sig_freq_hz=sig_freq,
        sig_axis=sig_axis, sig_amp_em=sig_amp, fill_tint=sig.fill_tint, fill_edge_softness=sig.fill_edge_softness,
        glow_spread=glow_spread, glow_color=glow_color,
        particle_kind=p_kind if p_kind in CUSTOM_PARTICLE_KINDS else "dust",
        sparkle_style=sparkle_style, motif=sig.motif,
    )
    return FxParams(primitives=tuple(prims), **meta, **f)


def compose_ambient(
    vec: Sequence[float],
    *,
    track_id: str = "",
    line_i: int = 0,
    word_i: int = 0,
    hold_s: float = 0.4,
    intensity: float = 1.0,
    palette: Sequence[RGB] = (),
    bleed_col: RGB | None = None,
    bleed_weight: float = 0.0,
    donor: Match | None = None,
    is_bg: bool = False,
    handoff_overlap_s: float = 0.32,
) -> FxParams:
    """Compose subtle (<= 15% amplitude) Undertow ambient parameters for non-semantic words."""
    k = _clamp(intensity, 0.0, 1.0) * (0.40 if is_bg else 1.0)
    col = blend_palette(ambient_vector_color(vec), palette, 0.45)
    c_to = col
    if bleed_col is not None and bleed_weight > 0.0:
        c_to = _mix(col, bleed_col, min(0.85, bleed_weight * 2.0))
    fill_tint = _clamp(MAX_AMBIENT_AMP * k * (1.0 + 1.2 * bleed_weight), 0.0, 0.25 if bleed_weight > 0.0 else MAX_AMBIENT_AMP)
    sig_amp = _clamp(0.005 * k * (1.0 + 1.5 * bleed_weight), 0.0, 0.012)
    softness = _clamp(1.0 + 0.12 * (vec[5] if len(vec) > 5 else 0.0), 0.85, 1.25)
    d_mode, _ = duration_mode(max(0.05, hold_s))
    return FxParams(
        seed=seed_for(track_id, line_i, word_i, 0),
        vector=tuple(float(x) for x in vec[:6]),
        primitives=(),
        bleed=round(bleed_weight, 4),
        bleed_from=donor.lemma if donor is not None else "",
        hold_s=hold_s,
        color_from=col,
        color_to=c_to,
        glow=_clamp(0.12 * k, 0.0, 0.15),
        glow_spread=0.18,
        fill_tint=fill_tint,
        fill_edge_softness=softness,
        sig_amp_em=sig_amp,
        sig_freq_hz=0.8,
        sig_axis="y",
        waveform="sine",
        release_s=0.65,
        residue_level=0.14,
        is_hero=False,
        is_bg=is_bg,
        tier="ambient",
        bleed_donor_s=(donor.begin_ms / 1000.0) if donor is not None else -1.0,
        bleed_donor_end_s=(donor.end_ms / 1000.0) if donor is not None else -1.0,
        handoff_overlap_s=handoff_overlap_s,
        duration_mode=d_mode,
        note="ambient",
    )
