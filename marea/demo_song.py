"""Canción demo del prototipo (marea-de-luz.html). Fuente única del fixture TTML y de los tests del motor."""
from marea.model.lyrics import BgVocal, Line, Lyrics, Syllable

# (inicio s, duración s, texto, dueto, voz de fondo (inicio, duración, texto) | None)
# "|" separa sílabas, "~" sílaba sostenida (peso 4)
SONG = [
    (6.0, 3.0, "Ca|mi|no so|lo por la ciu|dad", False, None),
    (9.4, 3.6, "las lu|ces se de|rri|ten en el mar~", False, None),
    (13.4, 3.1, "y tu voz~ me lle|ga sin pa|la|bras", False, (16.5, 1.3, "sin pa|la|bras")),
    (17.9, 3.2, "co|mo un e|co a|zul~", False, None),
    (21.4, 3.0, "Si te pier|do en|tre la gen|te", True, None),
    (24.8, 3.2, "te en|cuen|tro en ca|da can|ción", True, None),
    (33.5, 2.6, "Ma|re|a de luz~", False, (36.2, 1.4, "ma|re|a~")),
    (37.8, 3.0, "que no se a|pa|ga~", False, None),
    (41.2, 3.8, "ba|ja la ma|re|a, su|be el co|ra|zón", False, None),
    (45.4, 3.4, "y yo me que|do a|quí~", False, None),
    (49.2, 3.2, "a la de|ri|va~", False, None),
]


def words(x, t0, d):
    """-> palabras, cada una lista de (texto, inicio s, fin s); tiempos sin redondear."""
    ws = [[(s.rstrip("~"), 4 if s.endswith("~") else 1) for s in w.split("|")] for w in x.split(" ")]
    unit = d / sum(w for word in ws for _, w in word)
    cur, out = t0, []
    for word in ws:
        out.append([])
        for text, w in word:
            out[-1].append((text, cur, cur + w * unit))
            cur += w * unit
    return out


def _syllables(ws):
    return [Syllable(text, b * 1000, e * 1000, word_end=si == len(w) - 1 and wi < len(ws) - 1)
            for wi, w in enumerate(ws) for si, (text, b, e) in enumerate(w)]


def exact_lyrics() -> Lyrics:
    """Lyrics con tiempos en ms sin redondear (el TTML redondea a ms; el prototipo no)."""
    lines = []
    for t, d, x, duet, bg in SONG:
        syl = _syllables(words(x, t, d))
        bgs = []
        if bg:
            bsyl = _syllables(words(bg[2], bg[0], bg[1]))
            bgs = [BgVocal(bsyl[0].begin, bsyl[-1].end, bsyl)]
        lines.append(Line(t * 1000, max([syl[-1].end] + [b.end for b in bgs]), "v2" if duet else "v1", syl, bgs))
    return Lyrics(lines)
