"""Perfil de ánimo de una canción: etiquetas globales + arco por secciones + intensidad, todo local y determinista.

`classify` lo propone a partir de la letra (categorías de la biblioteca), el audio (`AudioStats`, opcional) y las notas de la
persona; ella puede editarlo antes de grabar, así que `from_dict` valida con mensajes claros. `vector_at` da el vector de emoción
base en un instante (para el compositor): el ánimo del tramo del arco, con fundido en las fronteras.
"""
import math
import re
import unicodedata
from dataclasses import dataclass, field

from marea.model.lyrics import Lyrics
from marea.wordfx.composer import blend_categories
from marea.wordfx.fxtypes import AXES, AudioStats
from marea.wordfx.library import Library
from marea.wordfx.matcher import match_lyrics

TAGS = ("sad", "nostalgic", "longing", "anguished", "tender", "hopeful", "joyful", "euphoric", "bittersweet", "tribute",
        "healing", "numb", "raw_vocals", "calm")
ARC_MOODS = tuple(t for t in TAGS if t not in ("raw_vocals", "tribute"))
ZERO = (0.0,) * len(AXES)
# vector base por ánimo, en el orden de AXES: (valence, arousal, gravity, temperature, fragility, blur)
MOOD_VECTORS = {
    "sad": (-0.6, 0.3, 0.6, -0.5, 0.5, 0.3), "nostalgic": (-0.1, 0.25, 0.25, 0.4, 0.55, 0.5),
    "longing": (-0.4, 0.35, 0.15, -0.1, 0.5, 0.4), "anguished": (-0.7, 0.85, 0.3, 0.2, 0.6, 0.2),
    "tender": (0.55, 0.25, -0.1, 0.45, 0.5, 0.25), "hopeful": (0.6, 0.45, -0.4, 0.3, 0.35, 0.2),
    "joyful": (0.7, 0.6, -0.5, 0.5, 0.3, 0.2), "euphoric": (0.9, 0.9, -0.7, 0.6, 0.2, 0.1),
    "bittersweet": (0.0, 0.35, 0.1, 0.2, 0.55, 0.35), "healing": (0.6, 0.3, -0.2, 0.15, 0.4, 0.3),
    "numb": (-0.3, 0.05, 0.2, -0.3, 0.3, 0.2), "calm": (0.4, 0.1, -0.1, -0.05, 0.3, 0.4),
}

# --- constantes de classify ---
# categoría de la biblioteca -> pesos por ánimo (los elementos pesan menos que las emociones)
CATEGORY_MOODS: dict[str, dict[str, float]] = {
    "sadness": {"sad": 1.0}, "loss": {"sad": 0.6, "longing": 0.6}, "longing": {"longing": 1.0}, "memory": {"nostalgic": 1.0},
    "anguish": {"anguished": 1.0}, "fear": {"anguished": 0.8}, "anger": {"anguished": 0.5}, "numbness": {"numb": 1.0},
    "tenderness": {"tender": 1.0}, "love": {"tender": 0.6, "joyful": 0.3}, "joy": {"joyful": 1.0},
    "healing": {"healing": 0.7, "hopeful": 0.6}, "growth": {"hopeful": 0.7, "joyful": 0.3}, "calm": {"calm": 1.0},
    "time": {"nostalgic": 0.15}, "celestial": {"hopeful": 0.15}, "air": {"hopeful": 0.15}, "cloud": {"nostalgic": 0.2}, "photo": {"nostalgic": 0.5},
    "water": {"sad": 0.2}, "dream": {"nostalgic": 0.25}, "shadow": {"sad": 0.2}, "dryness": {"sad": 0.2}, "craft": {"tender": 0.15},
    "broken": {"anguished": 0.4, "sad": 0.2}, "intoxication": {"numb": 0.2}, "fire": {"anguished": 0.1},
    "january": {"hopeful": 0.2}, "february": {"tender": 0.2}, "march": {"hopeful": 0.2},
    "april": {"hopeful": 0.15, "tender": 0.1}, "may": {"joyful": 0.2}, "june": {"joyful": 0.2},
    "july": {"joyful": 0.2}, "august": {"calm": 0.15, "hopeful": 0.1}, "september": {"nostalgic": 0.2},
    "october": {"nostalgic": 0.15, "longing": 0.1}, "november": {"nostalgic": 0.25},
    "december": {"nostalgic": 0.2, "calm": 0.1},
}  # shortcut: categorías propias del usuario no puntúan ánimo (solo arousal); upgrade: derivar el ánimo de su vector
EXTRA_CAT_WEIGHT = 0.5  # la 2ª, 3ª… categoría de una misma palabra pesa la mitad
TAG_MIN_SHARE = 0.15  # cuota mínima (tras sesgos) para ser etiqueta; la mayor entra con >= TOP_MIN_SHARE
TOP_MIN_SHARE = 0.08
MAX_MOOD_TAGS = 4
POSITIVE = ("joyful", "tender", "healing", "hopeful")
NEGATIVE = ("sad", "longing", "anguished", "nostalgic")
BITTERSWEET_MIN = 0.2  # cuota mínima de cada polo para derivar «bittersweet»
HOPE_RISE = 0.35  # subida de valencia media (último tercio vs resto) para derivar «hopeful»; y su valencia final mínima
HOPE_FINAL = 0.15
RAW_STRAIN = 0.6  # tensión vocal global para «raw_vocals» (y refuerzo de «anguished»)
RAW_ANGUISH = 0.2
MODE_MIN = 0.15  # |modo| menor que esto no sesga
EUPHORIC = (0.65, 115.0)  # (energía, bpm) mínimos con «joyful» para derivar «euphoric»
LOW_ENERGY = (0.3, 90.0)  # energía y bpm máximos que sesgan a calm/numb (tempo desconocido (0) no cuenta)
NOTE_BIAS = 0.3  # peso de una palabra clave de las notas (cuotas sobre 1)
FAMILY = frozenset({"familia", "madre", "padre", "papa", "mama", "abuelo", "abuela", "hermano", "hermana", "hijo", "hija",
                    "family", "mother", "father", "dad", "mom", "grandma", "grandpa", "brother", "sister", "son", "daughter"})
# ventanas del arco: cierran al llegar a WIN_S segundos con >= WIN_MIN_LINES versos, o a WIN_MAX_LINES versos
WIN_S, WIN_MIN_LINES, WIN_MAX_LINES = 12.0, 3, 8
MIN_SEG_S = 8.0  # un tramo más corto se funde con su vecino mayor
NEIGHBOR_W = 0.3  # cuánto cuenta la ventana vecina al elegir el ánimo de una ventana
PRIOR_W = 0.15  # cuánto cuenta el ánimo global de la canción (letra + audio + notas) en cada ventana
# intensidad = a*arousal de la letra + b*energía + c*tensión (sin audio: solo la letra)
INTENSITY_W = (0.35, 0.3, 0.35)
NEUTRAL_INTENSITY = 0.3
# claves de las notas (prefijos tras normalizar) -> ánimo y sesgo; «tribute» fuerza la etiqueta
NOTE_KEYS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("alzheimer", "memoria", "olvid", "recuerd", "demencia", "memory", "forget", "remember", "dementia"), ("nostalgic", "tender")),
    (("triste", "tristeza", "sad", "llor", "luto", "duelo", "grief", "cry", "perdida"), ("sad",)),
    (("angusti", "ahog", "ansiedad", "anxiety", "panic", "desespera", "asfixi", "drown"), ("anguished",)),
    (("esperanza", "hope", "esperanzador"), ("hopeful",)),
    (("alegr", "feliz", "happy", "joy", "celebr"), ("joyful",)),
    (("extran", "lejos", "distancia", "ausencia", "longing", "miss", "nostalg"), ("longing", "nostalgic")),
    (("sanar", "sanacion", "curar", "heal", "terapia"), ("healing",)),
    (("calma", "paz", "tranquil", "calm", "peace"), ("calm",)),
    (("entumec", "vacio", "numb", "apatia"), ("numb",)),
    (("euforia", "euphori"), ("euphoric",)),
    (("tribut", "homenaje", "papa", "mama", "padre", "madre", "abuel", "familia", "family", "parents", "dedicad"), ("tribute",)),
)


class ProfileError(ValueError):
    pass


@dataclass
class ArcSegment:
    start: float  # segundos
    end: float
    mood: str


@dataclass
class SongProfile:
    track_id: str = ""
    tags: list[str] = field(default_factory=list)
    arc: list[ArcSegment] = field(default_factory=list)
    intensity: float = 0.5
    notes: str = ""

    def has(self, tag: str) -> bool:
        return tag in self.tags

    def to_dict(self) -> dict:
        return {"track_id": self.track_id, "tags": list(self.tags),
                "arc": [{"start": s.start, "end": s.end, "mood": s.mood} for s in self.arc],
                "intensity": self.intensity, "notes": self.notes}

    @staticmethod
    def from_dict(raw: dict) -> "SongProfile":
        """Valida lo que la persona pudo editar a mano; ProfileError con el motivo en español."""
        if not isinstance(raw, dict):
            raise ProfileError("El perfil debe ser un objeto JSON.")
        tid, notes = raw.get("track_id", ""), raw.get("notes", "")
        if not isinstance(tid, str) or not isinstance(notes, str):
            raise ProfileError("`track_id` y `notes` deben ser texto.")
        tags_raw = raw.get("tags", [])
        if not isinstance(tags_raw, list):
            raise ProfileError("`tags` debe ser una lista.")
        for t in tags_raw:
            if t not in TAGS:
                raise ProfileError(f"Etiqueta desconocida {t!r}. Válidas: {', '.join(TAGS)}.")
        arc_raw = raw.get("arc", [])
        if not isinstance(arc_raw, list):
            raise ProfileError("`arc` debe ser una lista de tramos.")
        arc, prev_end = [], -math.inf
        for i, d in enumerate(arc_raw):
            if not isinstance(d, dict):
                raise ProfileError(f"Tramo {i + 1} del arco: debe ser un objeto con start, end y mood.")
            s, e, m = d.get("start"), d.get("end"), d.get("mood")
            if not all(_is_num(x) for x in (s, e)):
                raise ProfileError(f"Tramo {i + 1} del arco: `start` y `end` deben ser segundos (números).")
            if not s < e:
                raise ProfileError(f"Tramo {i + 1} del arco: `start` ({s}) debe ser menor que `end` ({e}).")
            if m not in ARC_MOODS:
                raise ProfileError(f"Tramo {i + 1} del arco: ánimo desconocido {m!r}. Válidos: {', '.join(ARC_MOODS)}.")
            if s < prev_end:
                raise ProfileError(f"Tramo {i + 1} del arco: debe empezar después de que acabe el anterior (orden y sin solaparse).")
            arc.append(ArcSegment(float(s), float(e), m))
            prev_end = e
        inten = raw.get("intensity", 0.5)
        if not _is_num(inten) or not 0 <= inten <= 1:
            raise ProfileError(f"`intensity` debe ser un número entre 0 y 1 (llegó {inten!r}).")
        return SongProfile(tid, list(dict.fromkeys(tags_raw)), arc, float(inten), notes)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def mood_vector(mood: str) -> tuple[float, ...]:
    """Vector base del ánimo (orden de AXES); `tribute` y `raw_vocals` no tienen uno propio (cero)."""
    if mood in ("tribute", "raw_vocals"):
        return ZERO
    try:
        return MOOD_VECTORS[mood]
    except KeyError:
        raise ProfileError(f"Ánimo desconocido {mood!r}. Válidos: {', '.join(TAGS)}.") from None


def _global_vector(profile: SongProfile) -> tuple[float, ...] | None:
    vs = [MOOD_VECTORS[t] for t in profile.tags if t in MOOD_VECTORS]
    return tuple(sum(v[i] for v in vs) / len(vs) for i in range(len(AXES))) if vs else None


def vector_at(profile: SongProfile, t_s: float, smooth_s: float = 1.0) -> tuple[float, ...] | None:
    """Vector base en `t_s`: ánimo del tramo del arco, con fundido lineal de `smooth_s` centrado en cada frontera.

    Fuera del arco (o sin él) se usa la media de las etiquetas globales; None si no hay ni arco ni etiquetas con vector.
    En los bordes del arco el fundido es contra ese vector global.
    """
    smooth = max(smooth_s, 1e-6)
    h = smooth / 2
    ws = [(max(0.0, min(1.0, (t_s - (s.start - h)) / smooth)) * max(0.0, min(1.0, (s.end + h - t_s) / smooth)), s.mood)
          for s in profile.arc]
    total = sum(w for w, _ in ws)
    g = _global_vector(profile)
    if total <= 0:
        return g
    vec = [sum(w * MOOD_VECTORS[m][i] for w, m in ws) for i in range(len(AXES))]
    if total < 1 and g is not None:
        return tuple(v + (1 - total) * g[i] for i, v in enumerate(vec))
    return tuple(v / total for v in vec) if total < 1 else tuple(vec)


def mood_at(profile: SongProfile, t_s: float) -> str | None:
    return next((s.mood for s in profile.arc if s.start <= t_s < s.end), None)


# ---------------------------------------------------------------- classify

def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _note_signals(notes: str) -> tuple[dict[str, float], set[str]]:
    """(sesgos por ánimo, etiquetas forzadas) de las notas libres ES/EN; los nombres de etiqueta escritos tal cual fuerzan."""
    txt = _fold(notes)
    bias: dict[str, float] = {}
    forced = {t for t in TAGS if re.search(rf"\b{t.replace('_', '[ _-]')}\b", txt)}
    for stems, moods in NOTE_KEYS:
        if any(re.search(rf"\b{s}", txt) for s in stems):
            for m in moods:
                if m == "tribute":
                    forced.add(m)
                else:
                    bias[m] = bias.get(m, 0.0) + NOTE_BIAS
    return bias, forced


def _audio_bias(a: AudioStats) -> dict[str, float]:
    b: dict[str, float] = {}

    def add(m, v):
        b[m] = b.get(m, 0.0) + v
    if abs(a.mode) >= MODE_MIN:
        k = min(1.0, abs(a.mode))
        for m, v in ((("sad", 0.08), ("nostalgic", 0.05), ("anguished", 0.05)) if a.mode < 0 else (("joyful", 0.08), ("hopeful", 0.06))):
            add(m, v * k)
    if a.strain >= RAW_STRAIN:
        add("anguished", RAW_ANGUISH)
    if a.energy < LOW_ENERGY[0] and (a.tempo_bpm <= 0 or a.tempo_bpm < LOW_ENERGY[1]):
        add("calm", 0.10)
        add("numb", 0.06)
    return b


def _norm(d: dict[str, float]) -> dict[str, float]:
    tot = sum(d.values())
    return {k: v / tot for k, v in d.items()} if tot > 0 else {}


def _neutral(track_id: str, notes: str) -> SongProfile:
    return SongProfile(track_id, [], [], NEUTRAL_INTENSITY, notes)


def _segments(wins: list[tuple[float, float, str]], t_end: float) -> list[ArcSegment]:
    """Ventanas (inicio, fin del último verso, ánimo) -> tramos contiguos: fronteras en el hueco entre ventanas, mismos ánimos
    fundidos, tramos < MIN_SEG_S fundidos con el vecino mayor."""
    segs: list[ArcSegment] = []
    start = wins[0][0]
    for i, (_, last_end, mood) in enumerate(wins):
        end = t_end if i == len(wins) - 1 else (last_end + wins[i + 1][0]) / 2
        segs.append(ArcSegment(start, max(end, start), mood))
        start = segs[-1].end
    while True:
        merged = [segs[0]]
        for s in segs[1:]:
            if s.mood == merged[-1].mood:
                merged[-1] = ArcSegment(merged[-1].start, s.end, s.mood)
            else:
                merged.append(s)
        segs = merged
        short = [i for i, s in enumerate(segs) if s.end - s.start < MIN_SEG_S]
        if not short or len(segs) == 1:
            return [s for s in segs if s.end > s.start]
        i = min(short, key=lambda k: segs[k].end - segs[k].start)  # el más corto primero
        nb = [j for j in (i - 1, i + 1) if 0 <= j < len(segs)]
        j = max(nb, key=lambda k: segs[k].end - segs[k].start)
        lo, hi = sorted((i, j))
        segs[lo:hi + 1] = [ArcSegment(segs[lo].start, segs[hi].end, segs[j].mood)]


def classify(lyrics: Lyrics, lib: Library, audio: AudioStats | None = None, notes: str = "", track_id: str = "",
             duration_ms: float | None = None, rules=None) -> SongProfile:
    """Propone el perfil: etiquetas (<= 4 de ánimo + derivadas + forzadas por notas), arco contiguo e intensidad 0..1."""
    lines = lyrics.lines
    if not lines:
        return _neutral(track_id, notes)
    matches = match_lyrics(lyrics, lib, max_fx=6, rules=rules)
    mass: list[dict[str, float]] = [{} for _ in lines]
    vals: list[list[float]] = [[] for _ in lines]
    arousal, n_arousal, family = 0.0, 0, 0
    for (li, _), m in sorted(matches.items()):
        vec = blend_categories(m.categories, lib)
        vals[li].append(vec[0])
        arousal += vec[1]
        n_arousal += 1
        family += m.lemma in FAMILY
        for rank, cat in enumerate(m.categories):
            for mood, w in CATEGORY_MOODS.get(cat, {}).items():
                mass[li][mood] = mass[li].get(mood, 0.0) + w * (1.0 if rank == 0 else EXTRA_CAT_WEIGHT)
    totals: dict[str, float] = {}
    for d in mass:
        for k, v in d.items():
            totals[k] = totals.get(k, 0.0) + v
    note_bias, forced = _note_signals(notes)
    scores = _norm(totals)
    for src in ((_audio_bias(audio) if audio else {}), note_bias):
        for k, v in src.items():
            scores[k] = scores.get(k, 0.0) + v
    shares = _norm(scores)

    ranked = sorted(shares, key=lambda m: (-shares[m], TAGS.index(m)))
    tags = [m for m in ranked[:MAX_MOOD_TAGS] if shares[m] >= TAG_MIN_SHARE or (m == ranked[0] and shares[m] >= TOP_MIN_SHARE)]
    pos, neg = (sum(shares.get(m, 0.0) for m in g) for g in (POSITIVE, NEGATIVE))
    if min(pos, neg) >= BITTERSWEET_MIN:
        tags.append("bittersweet")
    flat = [v for vs in vals if vs for v in [sum(vs) / len(vs)]]
    if len(flat) >= 6:
        cut = len(flat) * 2 // 3
        early, late = sum(flat[:cut]) / cut, sum(flat[cut:]) / (len(flat) - cut)
        if late - early >= HOPE_RISE and late >= HOPE_FINAL:
            tags.append("hopeful")
    if audio and "joyful" in tags and audio.energy >= EUPHORIC[0] and audio.tempo_bpm >= EUPHORIC[1]:
        tags.append("euphoric")
    if "tribute" in forced or ("tender" in tags and "nostalgic" in tags and family):
        tags.append("tribute")
    if (audio and audio.strain >= RAW_STRAIN) or "raw_vocals" in forced:
        tags.append("raw_vocals")
    tags += sorted(forced - {"tribute", "raw_vocals"}, key=TAGS.index)

    arc = _arc(lines, mass, shares, audio, duration_ms)
    # shortcut: sin ninguna palabra casada el arco queda vacío y `vector_at` usa la media de las etiquetas; upgrade: arco solo por audio
    lyr = (arousal + 0.3) / (n_arousal + 1)  # a priori débil de 1 palabra neutra: pocas palabras no mandan
    wa, we, ws = INTENSITY_W
    intensity = lyr if audio is None else wa * lyr + we * audio.energy + ws * audio.strain
    return SongProfile(track_id, list(dict.fromkeys(tags)), arc, round(max(0.0, min(1.0, intensity)), 2), notes)


def _arc(lines, mass, shares, audio, duration_ms) -> list[ArcSegment]:
    # 1. ventanas de versos
    wins: list[list[int]] = [[]]
    for i, ln in enumerate(lines):
        wins[-1].append(i)
        n, span = len(wins[-1]), (ln.end - lines[wins[-1][0]].start) / 1000
        if i < len(lines) - 1 and (n >= WIN_MAX_LINES or (n >= WIN_MIN_LINES and span >= WIN_S)):
            wins.append([])
    wm: list[dict[str, float]] = []
    for w in wins:
        d: dict[str, float] = {}
        for i in w:
            for k, v in mass[i].items():
                d[k] = d.get(k, 0.0) + v
        wm.append(_norm(d))
    if not any(wm):
        return []
    # 2. ánimo por ventana: propia + vecinas + a priori global (+ tensión de la ventana si hay curva)
    moods: list[str | None] = []
    for k, w in enumerate(wins):
        if not wm[k]:
            moods.append(None)
            continue
        sc = dict(wm[k])
        for nk in (k - 1, k + 1):
            if 0 <= nk < len(wins):
                for m, v in wm[nk].items():
                    sc[m] = sc.get(m, 0.0) + NEIGHBOR_W * v
        for m, v in shares.items():
            sc[m] = sc.get(m, 0.0) + PRIOR_W * v
        if audio is not None and audio.strain_curve is not None and len(audio.strain_curve):
            a, b = (int(lines[w[0]].start / 1000 * audio.strain_rate), int(lines[w[-1]].end / 1000 * audio.strain_rate) + 1)
            seg = audio.strain_curve[a:b]
            if len(seg) and float(seg.mean()) >= RAW_STRAIN:
                sc["anguished"] = sc.get("anguished", 0.0) + RAW_ANGUISH
        moods.append(min(sc, key=lambda m: (-sc[m], TAGS.index(m))))
    for k in range(len(moods)):  # huecos sin palabras casadas: heredan del anterior, y los iniciales del siguiente
        if moods[k] is None and k:
            moods[k] = moods[k - 1]
    for k in range(len(moods) - 2, -1, -1):
        if moods[k] is None:
            moods[k] = moods[k + 1]
    # 3. suavizado: una ventana aislada entre dos iguales se alinea con ellas
    for k in range(1, len(moods) - 1):
        if moods[k - 1] == moods[k + 1] != moods[k]:
            moods[k] = moods[k - 1]
    t_end = max(lines[-1].end, duration_ms or 0) / 1000
    spans = [(lines[w[0]].start / 1000, max(lines[i].end for i in w) / 1000, m) for w, m in zip(wins, moods)]
    return _segments(spans, t_end)
