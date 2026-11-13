"""Continuous motion signatures for Semantic Word FX ("Undertow").

Each category defines a continuous parameter-space `MotionSignature` instead of a discrete
list of animations, allowing smooth interpolation across blended categories, bleed, and contrast pairs.
"""
import math
from collections.abc import Sequence
from dataclasses import dataclass

from marea.wordfx.fxtypes import RGB

WAVEFORMS = ("sine", "flicker", "step", "tick", "orbit", "breath", "erode")
CUSTOM_PARTICLE_KINDS = frozenset({
    "petal", "firefly", "leaf", "snow",
    "ember", "raindrop", "tear", "bubble", "mist", "shard", "wisp", "seed",
})


@dataclass(frozen=True)
class MotionSignature:
    waveform: str = "sine"  # sine | flicker | step | tick | orbit | breath | erode
    freq_range_hz: tuple[float, float] = (0.8, 1.8)
    axis: str = "y"  # y | x | scale | rot
    amp_range_em: tuple[float, float] = (0.02, 0.06)
    glow_spread: float = 0.25  # 0..1 extra glow radius/intensity factor
    glow_color: RGB | None = None  # None = inherit word color
    fill_edge_softness: float = 1.0  # multiplier on base wipe edge FW (<1 sharper/stepped, >1 softer)
    fill_tint: float = 0.42  # 0.25..0.60 mix of category color into sung fill
    particle_kind: str = "none"  # none | dust | spark | ember
    particle_max: int = 0
    release_ms: float = 650.0  # 400..900 ms
    residue_level: float = 0.16  # 0.10..0.25
    zeta: float = 1.0  # 1.0 critically damped; 0.75..0.9 for joy/love/tenderness


# Hand-authored core signatures (Checkpoint 2: time, loss, love; Checkpoint 5 expands all 28)
SIGNATURES: dict[str, MotionSignature] = {
    "time": MotionSignature(
        waveform="tick",
        freq_range_hz=(1.0, 2.0),
        axis="y",
        amp_range_em=(0.010, 0.022),
        glow_spread=0.22,
        fill_edge_softness=0.55,  # crisp / stepped edge
        fill_tint=0.38,
        particle_kind="none",
        particle_max=0,
        release_ms=550.0,
        residue_level=0.15,
        zeta=1.0,
    ),
    "loss": MotionSignature(
        waveform="erode",
        freq_range_hz=(0.4, 0.8),
        axis="y",
        amp_range_em=(0.012, 0.028),
        glow_spread=0.15,
        fill_edge_softness=1.15,
        fill_tint=0.45,
        particle_kind="dust",
        particle_max=8,
        release_ms=850.0,
        residue_level=0.22,
        zeta=1.0,
    ),
    "love": MotionSignature(
        waveform="breath",
        freq_range_hz=(0.7, 1.2),
        axis="scale",
        amp_range_em=(0.015, 0.032),
        glow_spread=0.55,
        fill_edge_softness=1.20,
        fill_tint=0.48,
        particle_kind="spark",
        particle_max=5,
        release_ms=700.0,
        residue_level=0.20,
        zeta=0.82,
    ),
    "spring": MotionSignature(
        waveform="breath",
        freq_range_hz=(0.8, 1.3),
        axis="scale",
        amp_range_em=(0.014, 0.028),
        glow_spread=0.55,
        fill_edge_softness=1.15,
        fill_tint=0.48,
        particle_kind="petal",
        particle_max=18,
        release_ms=750.0,
        residue_level=0.20,
        zeta=0.82,
    ),
    "summer": MotionSignature(
        waveform="flicker",
        freq_range_hz=(1.1, 2.0),
        axis="y",
        amp_range_em=(0.012, 0.026),
        glow_spread=0.72,
        fill_edge_softness=1.10,
        fill_tint=0.52,
        particle_kind="firefly",
        particle_max=18,
        release_ms=650.0,
        residue_level=0.20,
        zeta=0.85,
    ),
    "autumn": MotionSignature(
        waveform="sine",
        freq_range_hz=(0.45, 0.85),
        axis="rot",
        amp_range_em=(0.014, 0.030),
        glow_spread=0.30,
        fill_edge_softness=1.20,
        fill_tint=0.50,
        particle_kind="leaf",
        particle_max=18,
        release_ms=850.0,
        residue_level=0.22,
        zeta=0.95,
    ),
    "winter": MotionSignature(
        waveform="breath",
        freq_range_hz=(0.35, 0.70),
        axis="y",
        amp_range_em=(0.008, 0.018),
        glow_spread=0.45,
        fill_edge_softness=1.30,
        fill_tint=0.45,
        particle_kind="snow",
        particle_max=20,
        release_ms=900.0,
        residue_level=0.24,
        zeta=1.0,
    ),
    "sadness": MotionSignature(
        waveform="erode",
        freq_range_hz=(0.35, 0.65),
        axis="y",
        amp_range_em=(0.010, 0.022),
        glow_spread=0.20,
        fill_edge_softness=1.25,
        fill_tint=0.45,
        particle_kind="raindrop",
        particle_max=16,
        release_ms=900.0,
        residue_level=0.22,
        zeta=1.0,
    ),
    "anguish": MotionSignature(
        waveform="flicker",
        freq_range_hz=(1.8, 3.0),
        axis="y",
        amp_range_em=(0.012, 0.024),
        glow_spread=0.25,
        fill_edge_softness=0.65,
        fill_tint=0.50,
        particle_kind="none",
        particle_max=0,
        release_ms=520.0,
        residue_level=0.16,
        zeta=1.0,
    ),
    "fear": MotionSignature(
        waveform="flicker",
        freq_range_hz=(2.0, 3.4),
        axis="x",
        amp_range_em=(0.010, 0.022),
        glow_spread=0.20,
        fill_edge_softness=0.70,
        fill_tint=0.42,
        particle_kind="none",
        particle_max=0,
        release_ms=460.0,
        residue_level=0.14,
        zeta=1.0,
    ),
    "anger": MotionSignature(
        waveform="step",
        freq_range_hz=(2.2, 3.6),
        axis="x",
        amp_range_em=(0.014, 0.028),
        glow_spread=0.40,
        fill_edge_softness=0.45,
        fill_tint=0.55,
        particle_kind="none",
        particle_max=0,
        release_ms=420.0,
        residue_level=0.15,
        zeta=1.0,
    ),
    "joy": MotionSignature(
        waveform="orbit",
        freq_range_hz=(1.2, 2.0),
        axis="scale",
        amp_range_em=(0.018, 0.034),
        glow_spread=0.68,
        fill_edge_softness=1.05,
        fill_tint=0.52,
        particle_kind="spark",
        particle_max=6,
        release_ms=600.0,
        residue_level=0.20,
        zeta=0.78,
    ),
    "healing": MotionSignature(
        waveform="breath",
        freq_range_hz=(0.6, 1.0),
        axis="scale",
        amp_range_em=(0.014, 0.026),
        glow_spread=0.55,
        fill_edge_softness=1.25,
        fill_tint=0.46,
        particle_kind="wisp",
        particle_max=10,
        release_ms=820.0,
        residue_level=0.21,
        zeta=0.85,
    ),
    "calm": MotionSignature(
        waveform="breath",
        freq_range_hz=(0.4, 0.75),
        axis="y",
        amp_range_em=(0.010, 0.020),
        glow_spread=0.45,
        fill_edge_softness=1.35,
        fill_tint=0.38,
        particle_kind="none",
        particle_max=0,
        release_ms=880.0,
        residue_level=0.20,
        zeta=1.0,
    ),
    "intoxication": MotionSignature(
        waveform="sine",
        freq_range_hz=(0.75, 1.3),
        axis="rot",
        amp_range_em=(0.016, 0.030),
        glow_spread=0.52,
        fill_edge_softness=1.35,
        fill_tint=0.50,
        particle_kind="none",
        particle_max=0,
        release_ms=780.0,
        residue_level=0.20,
        zeta=0.86,
    ),
    "celestial": MotionSignature(
        waveform="orbit",
        freq_range_hz=(0.6, 1.1),
        axis="y",
        amp_range_em=(0.014, 0.028),
        glow_spread=0.75,
        fill_edge_softness=1.25,
        fill_tint=0.50,
        particle_kind="spark",
        particle_max=7,
        release_ms=800.0,
        residue_level=0.22,
        zeta=0.88,
    ),
    "air": MotionSignature(
        waveform="sine",
        freq_range_hz=(0.6, 1.1),
        axis="x",
        amp_range_em=(0.016, 0.030),
        glow_spread=0.35,
        fill_edge_softness=1.25,
        fill_tint=0.40,
        particle_kind="seed",
        particle_max=14,
        release_ms=800.0,
        residue_level=0.18,
        zeta=0.90,
    ),
    "cloud": MotionSignature(
        waveform="breath",
        freq_range_hz=(0.35, 0.70),
        axis="y",
        amp_range_em=(0.014, 0.028),
        glow_spread=0.58,
        fill_edge_softness=1.40,
        fill_tint=0.40,
        particle_kind="mist",
        particle_max=14,
        release_ms=900.0,
        residue_level=0.22,
        zeta=1.0,
    ),
    "water": MotionSignature(
        waveform="sine",
        freq_range_hz=(0.65, 1.15),
        axis="y",
        amp_range_em=(0.016, 0.032),
        glow_spread=0.42,
        fill_edge_softness=1.22,
        fill_tint=0.48,
        particle_kind="bubble",
        particle_max=16,
        release_ms=780.0,
        residue_level=0.20,
        zeta=0.88,
    ),
    "fire": MotionSignature(
        waveform="flicker",
        freq_range_hz=(1.8, 3.0),
        axis="y",
        amp_range_em=(0.014, 0.028),
        glow_spread=0.80,
        fill_edge_softness=0.75,
        fill_tint=0.55,
        particle_kind="ember",
        particle_max=18,
        release_ms=480.0,
        residue_level=0.18,
        zeta=0.92,
    ),
    "dream": MotionSignature(
        waveform="orbit",
        freq_range_hz=(0.45, 0.85),
        axis="y",
        amp_range_em=(0.014, 0.030),
        glow_spread=0.62,
        fill_edge_softness=1.35,
        fill_tint=0.45,
        particle_kind="wisp",
        particle_max=14,
        release_ms=880.0,
        residue_level=0.22,
        zeta=0.86,
    ),
    "broken": MotionSignature(
        waveform="step",
        freq_range_hz=(1.5, 2.6),
        axis="rot",
        amp_range_em=(0.014, 0.032),
        glow_spread=0.25,
        fill_edge_softness=0.55,
        fill_tint=0.46,
        particle_kind="shard",
        particle_max=16,
        release_ms=520.0,
        residue_level=0.16,
        zeta=1.0,
    ),
}


def fallback_signature(categories: Sequence[str], vector: Sequence[float]) -> MotionSignature:
    """Derive a continuous signature for categories not yet explicitly authored in `SIGNATURES`."""
    val, aro, gra, tem, fra, blu = (vector[i] if i < len(vector) else 0.0 for i in range(6))
    waveform = "sine"
    axis = "y"
    softness = 1.0 + 0.35 * blu - 0.25 * max(0.0, aro - 0.5)
    if fra >= 0.75:
        waveform = "step" if aro >= 0.5 else "erode"
    elif aro >= 0.7 and tem >= 0.4:
        waveform = "flicker"
    elif gra <= -0.4 and val > 0.3:
        waveform = "orbit"
    elif val >= 0.4 and aro < 0.5:
        waveform = "breath"
        axis = "scale"
    release_ms = 850.0 if val < -0.2 or blu > 0.4 else 450.0 if aro >= 0.7 else 650.0
    residue = 0.22 if val < -0.2 or val > 0.5 else 0.14
    zeta = 0.84 if any(c in ("joy", "love", "tenderness") for c in categories) else 1.0
    return MotionSignature(
        waveform=waveform,
        freq_range_hz=(0.5 + 0.45 * aro, 0.9 + 1.0 * aro),
        axis=axis,
        amp_range_em=(0.010 + 0.010 * aro, 0.020 + 0.016 * aro),
        glow_spread=min(0.8, max(0.15, 0.25 + 0.3 * max(0.0, val))),
        fill_edge_softness=min(1.5, max(0.45, softness)),
        fill_tint=min(0.60, max(0.25, 0.35 + 0.20 * abs(val))),
        particle_kind="spark" if val > 0.4 else ("dust" if fra > 0.6 else "none"),
        particle_max=6 if (val > 0.4 or fra > 0.6) else 0,
        release_ms=release_ms,
        residue_level=residue,
        zeta=zeta,
    )


def signature_for(categories: Sequence[str], vector: Sequence[float], weights: Sequence[float] | None = None) -> MotionSignature:
    """Interpolate signatures in continuous parameter space across weighted categories."""
    if not categories:
        return fallback_signature((), vector)
    sigs = [SIGNATURES.get(c) or fallback_signature((c,), vector) for c in categories]
    if len(sigs) == 1:
        return sigs[0]
    ws = list(weights) if weights is not None else [1.0 / (i + 1) for i in range(len(sigs))]
    tot = sum(ws) or 1.0
    ws = [w / tot for w in ws]
    lead = max(range(len(sigs)), key=lambda i: ws[i])
    return MotionSignature(
        waveform=sigs[lead].waveform,
        freq_range_hz=(
            sum(w * s.freq_range_hz[0] for w, s in zip(ws, sigs)),
            sum(w * s.freq_range_hz[1] for w, s in zip(ws, sigs)),
        ),
        axis=sigs[lead].axis,
        amp_range_em=(
            sum(w * s.amp_range_em[0] for w, s in zip(ws, sigs)),
            sum(w * s.amp_range_em[1] for w, s in zip(ws, sigs)),
        ),
        glow_spread=sum(w * s.glow_spread for w, s in zip(ws, sigs)),
        glow_color=sigs[lead].glow_color,
        fill_edge_softness=sum(w * s.fill_edge_softness for w, s in zip(ws, sigs)),
        fill_tint=min(0.60, max(0.25, sum(w * s.fill_tint for w, s in zip(ws, sigs)))),
        particle_kind=sigs[lead].particle_kind,
        particle_max=round(sum(w * s.particle_max for w, s in zip(ws, sigs))),
        release_ms=sum(w * s.release_ms for w, s in zip(ws, sigs)),
        residue_level=sum(w * s.residue_level for w, s in zip(ws, sigs)),
        zeta=sum(w * s.zeta for w, s in zip(ws, sigs)),
    )


def eval_waveform(waveform: str, t: float, freq_hz: float, letter_idx: int, seed_u: float = 0.0,
                  beat_phase: float | None = None) -> float:
    """Return normalized oscillation in [-1, 1] (or [0, 1] for breath) for continuous motion signatures."""
    ph = 2.0 * math.pi * (freq_hz * t + seed_u) - 0.65 * letter_idx
    if waveform == "sine":
        return math.sin(ph)
    if waveform == "breath":
        # Two-pulse heartbeat-like breathing wave
        u = (freq_hz * t + seed_u - 0.12 * letter_idx) % 1.0
        p1 = math.exp(-((u - 0.18) / 0.11) ** 2)
        p2 = 0.65 * math.exp(-((u - 0.42) / 0.13) ** 2)
        return p1 + p2
    if waveform == "tick":
        # Beat-locked tick pulse with smooth rounded step (continuous at 240 Hz, no infinite slope)
        u = ((beat_phase if beat_phase is not None else freq_hz * t) - 0.06 * letter_idx) % 1.0
        return math.exp(-((u - 0.08) / 0.07) ** 2) * 2.0 - 0.3
    if waveform == "step":
        # Smooth quantized stair-step wave (tanh-softened square for C1 continuity)
        return math.tanh(2.5 * math.sin(ph))
    if waveform == "flicker":
        return 0.65 * math.sin(ph * 1.7) + 0.35 * math.sin(ph * 3.1 + 1.3 * letter_idx)
    if waveform == "orbit":
        return math.sin(ph)
    if waveform == "erode":
        return -0.5 * (1.0 - math.cos(min(math.pi, max(0.0, ph * 0.25))))
    return math.sin(ph)
