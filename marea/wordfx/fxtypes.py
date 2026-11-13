"""Contrato entre el compositor (`composer.py`) y el renderizador (`anim.py`/`render.py`): datos simples, inmutables y picklables.

Todas las longitudes `_em` son múltiplos del tamaño de letra (se escalan solas a 1080p o a la preview); los tiempos van en segundos.
Cero (o 1.0 en `opacity_min`) significa «efecto apagado». Nada aquí depende de skia.
"""
from dataclasses import dataclass, field

import numpy as np

from marea.wordfx.matcher import Match

RGB = tuple[int, int, int]
AXES = ("valence", "arousal", "gravity", "temperature", "fragility", "blur")  # orden de FxParams.vector


@dataclass(frozen=True)
class FxParams:
    seed: int  # estable: sha1(track_id:verso:palabra); toda la aleatoriedad del render sale de aquí
    vector: tuple[float, ...]  # vector final de emoción, en el orden de AXES
    primitives: tuple[str, ...]  # primitivos activos (subconjunto de library.PRIMITIVES)
    bleed: float = 0.0  # peso total de contagio de vecinas (solo depuración)
    bleed_from: str = ""  # lemas que contagian (solo depuración)
    repeat_n: int = 0  # cuántas veces apareció antes esta palabra/verso (memoria de repetición)
    hold_s: float = 0.0  # cuánto se sostiene la palabra (fin - inicio)
    color_from: RGB = (255, 255, 255)  # color de la primera letra
    color_to: RGB = (255, 255, 255)  # color de la última (≠ color_from si una vecina contagia)
    # --- movimiento ---
    drift_em: float = 0.0  # desplazamiento vertical al final de la palabra: + baja, - sube; se alcanza durante el hold y se queda
    jitter_em: float = 0.0  # amplitud pico del temblor por letra (x e y)
    jitter_hz: float = 0.0
    wave_em: float = 0.0  # onda vertical que recorre las letras
    wave_hz: float = 0.0
    spacing_em: float = 0.0  # espaciado extra entre letras; negativo comprime
    scale_amp: float = 0.0  # «respira»: escala 1 → 1+amp → 1
    scale_period_s: float = 1.2
    rotation_deg: float = 0.0  # rotación pico por letra
    stagger_ms: float = 0.0  # retraso entre letras de la entrada (mask_reveal / letter_offset)
    letter_offset_em: float = 0.0  # cada letra entra esta distancia por debajo y se asienta
    # --- aspecto ---
    opacity_min: float = 1.0  # las letras oscilan/bajan hasta este alpha (1 = sin efecto)
    blur_em: float = 0.0  # desenfoque inicial que se aclara a 0 durante el hold
    glow: float = 0.0  # 0..1
    grain: float = 0.0  # 0..1 grano de película
    chromatic_em: float = 0.0  # separación RGB (aberración cromática)
    trail: float = 0.0  # 0..1 estela fantasma tras cada letra
    # --- destrucción / destellos ---
    erode: float = 0.0  # 0..1 fracción de letras (las últimas) que se disuelven
    vanish_s: float = 0.0  # segundo en que empiezan a disolverse (el verso siguiente); 0 = al final de la palabra
    particles: int = 0  # partículas totales que sueltan las letras erosionadas
    fracture: float = 0.0  # 0..1 las letras se parten en dos mitades que se separan
    sparkle: int = 0  # destellos alrededor de la palabra
    flash: float = 0.45  # glow extra del mini destello al empezar cada letra (más fuerte en lo positivo y brillante)
    audio_gain: float = 0.0  # 0..1 cuánto modula el RMS el temblor y el brillo
    # --- par de contraste (cura/veneno): el color oscila entre el propio y el de la pareja, y en el estado de la pareja tiembla ---
    osc_hz: float = 0.0  # 0 = sin oscilación; ~2 Hz en un par
    osc_color: RGB = (255, 255, 255)  # color del otro polo (el de la palabra pareja)
    osc_phase: float = 0.0  # rad; 0 en la primera palabra del par y π en la segunda (se intercambian los colores)
    osc_jitter_em: float = 0.0  # amplitud de temblor que se suma cuando el color está en el polo de la pareja
    note: str = ""  # por qué se eligió (frase, negación…), solo depuración
    strain_gain: float = 0.0  # 0..1 cuánto sigue la tensión vocal (raw_vocals) al temblor y a la aberración cromática
    # --- Undertow continuity & envelope metadata ---
    release_s: float = 0.65  # 0.40..0.90 s decay into residue
    residue_level: float = 0.16  # 0.10..0.25 settled level while line is active
    zeta: float = 1.0  # spring damping ratio (1.0 critically damped; 0.75..0.9 for joy/love)
    is_hero: bool = True  # hero words get pre-onset anticipation
    is_bg: bool = False  # background vocal (30-50% amplitude)
    tier: str = "hero"  # hero | support | ambient
    bleed_donor_s: float = -1.0  # donor begin_s (>=0 enables spring-eased bleed arrival)
    bleed_donor_end_s: float = -1.0
    handoff_overlap_s: float = 0.32  # 0.25..0.40 s line release/anticipation overlap
    duration_mode: str = "normal"  # accent | normal | held
    # --- Motion signature parameters ---
    waveform: str = "sine"  # sine | flicker | step | tick | orbit | breath | erode
    sig_freq_hz: float = 1.2
    sig_axis: str = "y"  # y | x | scale | rot
    sig_amp_em: float = 0.03
    fill_tint: float = 0.42  # 0.25..0.60 category color mix on sung fill
    fill_edge_softness: float = 1.0  # FW multiplier
    glow_spread: float = 0.25
    particle_kind: str = "dust"  # dust | petal | firefly | leaf | snow | ember | raindrop | tear | bubble | mist | shard | wisp | seed


@dataclass(frozen=True)
class CarrierLetter:
    """Read-only base renderer carrier state for a single character."""
    fill_p: float = 0.0  # 0..1 wipe progress at this letter
    onset_s: float = 0.0  # timestamp when fill front reaches this letter
    y_em: float = 0.0  # base syllable/letter lift + character motion wave
    scale: float = 1.0  # base syllable/letter scale
    glow_a: float = 0.0
    glow_r: float = 0.0


@dataclass(frozen=True)
class CarrierWord:
    """Read-only base renderer carrier state for a word and its parent line."""
    letters: tuple[CarrierLetter, ...] = ()
    fill_front_u: float = 0.0  # 0..1 position of wipe front across word width
    line_act: float = 1.0  # L.act (0..1)
    line_on: bool = True  # L.on
    line_opacity: float = 1.0
    line_blur_px: float = 0.0
    line_start_s: float = 0.0
    line_end_s: float = 0.0
    is_bg: bool = False


@dataclass(frozen=True)
class FxWord:
    match: Match
    params: FxParams


@dataclass
class AudioStats:
    """Resumen acústico local de la canción (sin red ni endpoints de Spotify): entra al perfil de ánimo y a los efectos."""
    duration_ms: float = 0.0
    tempo_bpm: float = 0.0  # 0 = desconocido
    energy: float = 0.5  # 0..1 energía media normalizada (RMS)
    brightness: float = 0.5  # 0..1 centroide espectral normalizado (oscuro..brillante)
    mode: float = 0.0  # -1 menor .. +1 mayor (confianza)
    key: int = -1  # 0..11 (C=0), -1 = desconocida
    strain: float = 0.0  # 0..1 tensión vocal global: rugosidad, energía en agudos y picos de volumen
    strain_curve: np.ndarray | None = field(default=None, repr=False, compare=False)  # 0..1 por muestra a `strain_rate`
    strain_rate: float = 50.0  # muestras por segundo de strain_curve
    from_stem: bool = False  # True si la tensión vino de la voz separada y no de la mezcla
