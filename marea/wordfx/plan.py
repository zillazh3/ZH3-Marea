"""Plan de efectos de un clip: qué palabras llevan efecto y sus parámetros ya compuestos.

Datos simples y picklables (viajan al proceso de export); el render solo lee, nunca decide: así preview y export coinciden.
"""
import hashlib
import math
from dataclasses import dataclass, replace

import numpy as np

from marea.model.lyrics import Lyrics
from marea.wordfx import library, songs
from marea.wordfx.composer import blend_categories, compose
from marea.wordfx.fxtypes import RGB, FxWord
from marea.wordfx.lexicon import normalize
from marea.wordfx.matcher import Match, match_lyrics
from marea.wordfx.profile import vector_at

NEIGHBOR_TOKENS = 2  # una vecina con efecto a ≤2 palabras contagia


@dataclass
class FxPlan:
    words: dict[tuple[int, int], FxWord]
    intensity: float = 1.0  # 0..1.5, ya aplicada por el compositor; se guarda para saber con qué se armó
    rms: np.ndarray | None = None  # energía normalizada del audio (~100 Hz); None = sin audio local
    rms_rate: float = 100.0
    strain: np.ndarray | None = None  # tensión vocal 0..1 (de AudioStats.strain_curve); None = desconocida
    strain_rate: float = 50.0
    adapt_bg: bool = True  # el color de cada palabra se corrige según el fondo que tiene detrás (ver `contrast.py`)

    def get(self, line: int, word: int) -> FxWord | None:
        return self.words.get((line, word))

    @property
    def matches(self) -> dict[tuple[int, int], Match]:
        return {k: w.match for k, w in self.words.items()}

    def strain_at(self, t_s: float) -> float:
        """Tensión vocal en t (0..1); 0 si no se conoce: los efectos que la siguen quedan en reposo."""
        if self.strain is None or len(self.strain) == 0:
            return 0.0
        return float(self.strain[min(max(int(t_s * self.strain_rate), 0), len(self.strain) - 1)])

    def rms_at(self, t_s: float) -> float:
        """Energía del audio en t (0..1+); 0.5 neutro si no hay audio, para que el efecto no dependa de él."""
        if self.rms is None or len(self.rms) == 0:
            return 0.5
        return float(self.rms[min(max(int(t_s * self.rms_rate), 0), len(self.rms) - 1)])


def track_key(lyrics: Lyrics, track_id: str = "") -> str:
    """Clave de la canción para la semilla y para `songs/<clave>.json`; sin pista (TTML), un hash de la letra."""
    return track_id or hashlib.sha1("\n".join(l.text for l in lyrics.lines).encode()).hexdigest()[:16]


def _line_key(text: str) -> str:
    return " ".join(w for w in (normalize(p) for p in text.split()) if w)


def build_plan(lyrics: Lyrics, lib: library.Library | None = None, intensity: float = 1.0, *, track_id: str = "",
               palette: tuple[RGB, ...] = (), analysis=None, overrides: list[songs.Override] | None = None,
               profile=None, audio_stats=None, adapt_bg: bool = True) -> FxPlan:
    """analysis: `marea.model.cinematic.AudioAnalysis` del audio local (RMS y pulso) o None.
    profile: `marea.wordfx.profile.SongProfile` (ánimo de la canción: línea base por tramo del arco y raw_vocals) o None.
    audio_stats: `fxtypes.AudioStats` (aporta la curva de tensión vocal) o None.
    overrides: de la canción; None = leer `songs/<pista>.json` (así un archivo editado entra en el siguiente armado)."""
    lib = lib or library.load()
    track = track_key(lyrics, track_id)
    matches, vec_over, mult = songs.apply(match_lyrics(lyrics, lib), lyrics, lib,
                                          songs.load(track) if overrides is None else overrides)
    by_line: dict[int, list[Match]] = {}
    for m in matches.values():
        by_line.setdefault(m.line, []).append(m)
    words, seen_lemma, seen_line = {}, {}, {}
    raw_vocals = profile is not None and profile.has("raw_vocals")
    beat = getattr(analysis, "beat_period_ms", None) if analysis is not None and getattr(analysis, "beats_ms", None) else None
    for li, line in enumerate(lyrics.lines):
        lk = _line_key(line.text)
        line_rep = seen_line.get(lk, 0)  # coros: el verso entero ya sonó antes
        seen_line[lk] = line_rep + 1
        ms = by_line.get(li, [])
        moods = [blend_categories(m.categories, lib) for m in ms]
        line_mood = tuple(sum(v[i] for v in moods) / len(moods) for i in range(len(moods[0]))) if moods else None
        for m in ms:  # ya vienen en orden de palabra
            near = [(o, abs(o.word - m.word)) for o in ms if o is not m and abs(o.word - m.word) <= NEIGHBOR_TOKENS]
            rep = max(seen_lemma.get(m.lemma, 0), line_rep)
            seen_lemma[m.lemma] = seen_lemma.get(m.lemma, 0) + 1
            key = (m.line, m.word)
            partner = matches.get((m.line, m.pair)) if m.pair >= 0 else None  # par de contraste: los dos se intercambian los colores
            params = compose(
                m, lib, track_id=track, neighbors=near, line_mood=line_mood, palette=palette, repeat_n=rep,
                intensity=intensity * mult.get(key, 1.0), beat_period_ms=beat, partner=partner,
                osc_phase=0.0 if partner is None or m.word < partner.word else math.pi, vector_override=vec_over.get(key),
                song_mood=vector_at(profile, m.begin_ms / 1000) if profile is not None else None, raw_vocals=raw_vocals)
            if params.erode > 0.0:  # las letras que se deshacen esperan al verso siguiente (o al fin del último)
                nxt = lyrics.lines[li + 1].start if li + 1 < len(lyrics.lines) else line.end
                params = replace(params, vanish_s=max(nxt, m.end_ms) / 1000.0)
            words[key] = FxWord(m, params)
    rms = rate = None
    if analysis is not None and len(analysis.rms):
        rms, rate = np.asarray(analysis.rms, dtype=np.float32), float(analysis.rate)
    strain = getattr(audio_stats, "strain_curve", None)
    return FxPlan(words, intensity, rms, rate or 100.0, strain, float(getattr(audio_stats, "strain_rate", 50.0)), adapt_bg)
