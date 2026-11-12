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
