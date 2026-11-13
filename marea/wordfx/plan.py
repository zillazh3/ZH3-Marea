"""Plan de efectos de un clip: qué palabras llevan efecto y sus parámetros ya compuestos.

Datos simples y picklables (viajan al proceso de export); el render solo lee, nunca decide: así preview y export coinciden.
"""
import hashlib
import math
from dataclasses import dataclass, field, replace

import numpy as np

from marea.model.lyrics import Line, Lyrics
from marea.wordfx import library, songs
from marea.wordfx.bleed import handoff_overlap_s, line_bleed_donors
from marea.wordfx.composer import blend_categories, compose, compose_ambient
from marea.wordfx.fxtypes import RGB, FxWord
from marea.wordfx.lexicon import normalize
from marea.wordfx.matcher import Match, match_lyrics, word_groups
from marea.wordfx.momentum import MomentumCurve, build_momentum
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
    ambient: dict[tuple[int, int], FxWord] = field(default_factory=dict)
    bg_words: dict[tuple[int, int], FxWord] = field(default_factory=dict)
    momentum: MomentumCurve | None = None

    def get(self, line: int, word: int) -> FxWord | None:
        return self.words.get((line, word))

    def word_fx(self, line: int, word: int, *, is_bg: bool = False) -> FxWord | None:
        """Return semantic or Undertow ambient FxWord for `(line, word)`."""
        if is_bg:
            return self.bg_words.get((line, word)) or self.ambient.get((line, word))
        return self.words.get((line, word)) or self.ambient.get((line, word))

    def interlude_color(self, t_s: float) -> RGB:
        """Return <= 20% momentum-tinted color for instrumental interlude dots."""
        if self.momentum is None or self.intensity <= 0.0:
            return (255, 255, 255)
        return self.momentum.interlude_color(t_s, min(1.0, self.intensity))

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
    ambient: dict[tuple[int, int], FxWord] = {}
    bg_words: dict[tuple[int, int], FxWord] = {}
    momentum_events: list[tuple[float, float, tuple[float, ...], float]] = []
    raw_vocals = profile is not None and profile.has("raw_vocals")
    beat = getattr(analysis, "beat_period_ms", None) if analysis is not None and getattr(analysis, "beats_ms", None) else None
    prev_last_hero: Match | None = None
    for li, line in enumerate(lyrics.lines):
        lk = _line_key(line.text)
        line_rep = seen_line.get(lk, 0)  # coros: el verso entero ya sonó antes
        seen_line[lk] = line_rep + 1
        ms = by_line.get(li, [])
        nxt_start = lyrics.lines[li + 1].start if li + 1 < len(lyrics.lines) else None
        overlap_s = handoff_overlap_s(line.end, nxt_start)
        moods = [blend_categories(m.categories, lib) for m in ms]
        line_mood = tuple(sum(v[i] for v in moods) / len(moods) for i in range(len(moods[0]))) if moods else None
        for m in ms:  # ya vienen en orden de palabra
            donors = line_bleed_donors(li, m.word, ms, prev_last_hero, line.start)
            rep = max(seen_lemma.get(m.lemma, 0), line_rep)
            seen_lemma[m.lemma] = seen_lemma.get(m.lemma, 0) + 1
            key = (m.line, m.word)
            partner = matches.get((m.line, m.pair)) if m.pair >= 0 else None  # par de contraste: los dos se intercambian los colores
            params = compose(
                m, lib, track_id=track, bleed_donors=donors, line_mood=line_mood, palette=palette, repeat_n=rep,
                intensity=intensity * mult.get(key, 1.0), beat_period_ms=beat, partner=partner,
                osc_phase=0.0 if partner is None or m.word < partner.word else math.pi, vector_override=vec_over.get(key),
                song_mood=vector_at(profile, m.begin_ms / 1000) if profile is not None else None,
                raw_vocals=raw_vocals, handoff_overlap_s=overlap_s)
            if params.erode > 0.0:  # las letras que se deshacen esperan al verso siguiente (o al fin del último)
                nxt = nxt_start if nxt_start is not None else line.end
                params = replace(params, vanish_s=max(nxt, m.end_ms) / 1000.0)
            words[key] = FxWord(m, params)
            momentum_events.append((m.begin_ms / 1000.0, m.end_ms / 1000.0, params.vector, max(0.2, min(1.0, m.score))))
        prev_last_hero = ms[-1] if ms else None

        # Match background vocals / ad-libs at 40% amplitude (after main words in L.words order)
        main_groups = word_groups(line.syllables)
        wi_offset = len(main_groups)
        for bg_line in line.bg:
            bg_as_line = Line(bg_line.start, bg_line.end, syllables=bg_line.syllables)
            bg_groups = word_groups(bg_line.syllables)
            bg_matches = match_lyrics(Lyrics([bg_as_line]), lib)
            bg_by_w = {bm.word: bm for bm in bg_matches.values()}
            for bi, grp in enumerate(bg_groups):
                b_key = (li, wi_offset + bi)
                bm = bg_by_w.get(bi)
                if bm is not None:
                    b_match = replace(bm, line=li, word=wi_offset + bi)
                    b_params = compose(
                        b_match, lib, track_id=track, line_mood=line_mood, palette=palette,
                        beat_period_ms=beat, intensity=intensity, is_bg=True,
                        song_mood=vector_at(profile, b_match.begin_ms / 1000) if profile is not None else None,
                        raw_vocals=raw_vocals, handoff_overlap_s=overlap_s,
                    )
                    bg_words[b_key] = FxWord(b_match, b_params)
            wi_offset += len(bg_groups)

    song_end_s = max((l.end for l in lyrics.lines), default=0) / 1000.0
    momentum = build_momentum(momentum_events, song_end_s, profile=profile)

    # Populate Undertow ambient modulation (<= 15%) and cross-line bleed on non-semantic words
    prev_hero_for_amb: Match | None = None
    for li, line in enumerate(lyrics.lines):
        ms = by_line.get(li, [])
        nxt_start = lyrics.lines[li + 1].start if li + 1 < len(lyrics.lines) else None
        overlap_s = handoff_overlap_s(line.end, nxt_start)
        main_groups = word_groups(line.syllables)
        for wi, grp in enumerate(main_groups):
            key = (li, wi)
            if key in words:
                continue
            b0, b1 = grp[0].begin, grp[-1].end
            text = "".join(s.text for s in grp)
            donors = line_bleed_donors(li, wi, ms, prev_hero_for_amb, line.start)
            donor, b_w = max(donors, key=lambda x: x[1]) if donors else (None, 0.0)
            bleed_col = None
            if donor is not None:
                dfx = words.get((donor.line, donor.word))
                if dfx is not None:
                    bleed_col = dfx.params.color_from
            vec = momentum.vector_at(b0 / 1000.0)
            amb_p = compose_ambient(
                vec, track_id=track, line_i=li, word_i=wi,
                hold_s=max(0.05, (b1 - b0) / 1000.0), intensity=intensity,
                palette=palette, bleed_col=bleed_col, bleed_weight=b_w,
                donor=donor, is_bg=False, handoff_overlap_s=overlap_s,
            )
            amb_m = Match(li, wi, text, text.lower(), "es", (), b0, b1, score=0.0, note="ambient")
            ambient[key] = FxWord(amb_m, amb_p)
        wi_offset = len(main_groups)
        for bg_line in line.bg:
            bg_groups = word_groups(bg_line.syllables)
            for bi, grp in enumerate(bg_groups):
                b_key = (li, wi_offset + bi)
                if b_key in bg_words:
                    continue
                b0, b1 = grp[0].begin, grp[-1].end
                text = "".join(s.text for s in grp)
                vec = momentum.vector_at(b0 / 1000.0)
                amb_p = compose_ambient(
                    vec, track_id=track, line_i=li, word_i=wi_offset + bi,
                    hold_s=max(0.05, (b1 - b0) / 1000.0), intensity=intensity,
                    palette=palette, is_bg=True, handoff_overlap_s=overlap_s,
                )
                amb_m = Match(li, wi_offset + bi, text, text.lower(), "es", (), b0, b1, score=0.0, note="ambient")
                ambient[b_key] = FxWord(amb_m, amb_p)
            wi_offset += len(bg_groups)
        prev_hero_for_amb = ms[-1] if ms else None

    rms = rate = None
    if analysis is not None and len(analysis.rms):
        rms, rate = np.asarray(analysis.rms, dtype=np.float32), float(analysis.rate)
    strain = getattr(audio_stats, "strain_curve", None)
    return FxPlan(
        words, intensity, rms, rate or 100.0, strain,
        float(getattr(audio_stats, "strain_rate", 50.0)), adapt_bg,
        ambient=ambient, bg_words=bg_words, momentum=momentum,
    )
