"""Núcleo numérico del alineador (solo numpy; los modelos y torch entran en otras etapas).

Entrada: emisiones CTC `logp` (frames × vocabulario, log-probabilidades) y la letra ya dividida en palabras → sílabas.
Salida: diccionarios con la forma de `alignment.v1` (tiempos en ms), listos para `marea.lab.alignment.line_from_dict`.

Reglas de honestidad:
- El inicio de cada sílaba sale del primer frame de su primera letra; nada se reparte por igual.
- El fin de una sílaba solo se alarga sobre una nota sostenida si hay energía vocal que lo respalde. Sin esa evidencia el fin es
  el frame de su última letra y la sílaba queda `approximate`.
- Una pausa existe solo si la energía estuvo baja ≥ MIN_PAUSE_MS.
"""
import unicodedata
from dataclasses import dataclass

import numpy as np

FRAME_MS = 20.0  # wav2vec2: 320 muestras a 16 kHz
MIN_PAUSE_MS = 150
SUSTAIN_RATIO = 0.4  # la nota sigue mientras la energía sea ≥ 40 % (-8 dB) de la de su propia letra
MIN_DUR_MS, MAX_DUR_MS = 40, 8000  # fuera de este rango la confianza se reduce a la mitad
NEG = -1e30
VOWELS = set("aeiou")


@dataclass
class TokenSpan:
    first: int  # primer frame en el que el camino emite la letra
    last: int  # último (inclusive)
    logp: float  # media de log P(letra) en esos frames


def viterbi(logp: np.ndarray, tokens: list[int], blank: int = 0) -> list[TokenSpan]:
    """Alineación forzada CTC: el camino más probable que emite `tokens` en orden (con blanks entre medias y obligatorio entre
    letras idénticas consecutivas). Devuelve, por letra, los frames en que se emite."""
    T, L = len(logp), len(tokens)
    if L == 0:
        return []
    if T < L + sum(tokens[i] == tokens[i - 1] for i in range(1, L)):
        raise ValueError("El audio es demasiado corto para el texto.")
    lp, tok = np.asarray(logp, np.float64), np.asarray(tokens, np.int64)
    S = 2 * L + 1
    labels = np.full(S, blank, np.int64)
    labels[1::2] = tok
    skip = np.zeros(S, bool)  # salto de una letra a la siguiente sin blank, solo si son distintas
    skip[3::2] = tok[1:] != tok[:-1]
    alpha = np.full(S, NEG)
    alpha[0], alpha[1] = lp[0, blank], lp[0, labels[1]]
    back = np.zeros((T, S), np.int8)  # 0 = se queda, 1 = viene de s-1, 2 = viene de s-2
    for t in range(1, T):
        stacked = np.stack((alpha, np.concatenate(([NEG], alpha[:-1])),
                            np.where(skip, np.concatenate(([NEG, NEG], alpha[:-2])), NEG)))
        choice = stacked.argmax(0)
        alpha = np.take_along_axis(stacked, choice[None], 0)[0] + lp[t, labels]
        back[t] = choice
    s = S - 1 if alpha[S - 1] >= alpha[S - 2] else S - 2
    path = np.empty(T, np.int64)
    for t in range(T - 1, -1, -1):
        path[t] = s
        s -= int(back[t, s])  # int(): con NumPy 2, int de Python - np.int8 se calcula en int8 y desborda con más de 127 estados
    ts = np.flatnonzero(path & 1)  # estados impares = letras
    ks = (path[ts] - 1) // 2
    first, last = np.full(L, T), np.full(L, -1)
    total, count = np.zeros(L), np.zeros(L)
    np.minimum.at(first, ks, ts)
    np.maximum.at(last, ks, ts)
    np.add.at(total, ks, lp[ts, tok[ks]])
    np.add.at(count, ks, 1)
    return [TokenSpan(int(f), int(l), float(s_ / c)) for f, l, s_, c in zip(first, last, total, count)]


def _token(ch: str, vocab: dict[str, int]) -> int | None:
    c = ch.lower()
    if c in vocab:
        return vocab[c]
    return vocab.get(unicodedata.normalize("NFD", c)[:1])  # «á» -> «a» si el modelo no tiene acentos


def _is_vowel(ch: str) -> bool:
    return unicodedata.normalize("NFD", ch.lower())[:1] in VOWELS


def _vowel(rec: dict, spans: list[TokenSpan], end_ms: int, frame_ms: float) -> dict | None:
    """Núcleo de la sílaba (primer grupo de vocales: diptongos y triptongos juntos). Empieza en el frame de su primera letra y
    termina donde empieza la primera consonante que le sigue dentro de la sílaba o, si no hay, donde termina la sílaba."""
    chars = rec["chars"]  # [(carácter, índice de letra o None)]
    idx = [i for i, (c, _) in enumerate(chars) if _is_vowel(c) and chars[i][1] is not None]
    if not idx:
        return None
    run = [idx[0]]
    for i in idx[1:]:
        if i != run[-1] + 1:
            break
        run.append(i)
    after = next((t for _, t in chars[run[-1] + 1:] if t is not None), None)
    start = round(spans[chars[run[0]][1]].first * frame_ms)
    end = round(spans[after].first * frame_ms) if after is not None else end_ms
    return {"text": "".join(chars[i][0] for i in run), "start": start, "end": end, "confidence": rec["conf"],
            "approximate": True, "source": "ctc-char"}


def align_lines(logp: np.ndarray, lines: list[list[list[str]]], vocab: dict[str, int], *, blank: int = 0, frame_ms: float = FRAME_MS,
                energy: np.ndarray | None = None, lang: str = "", syllables: bool = True) -> list[dict]:
    """Alinea TODA la letra de una vez (`lines` = líneas → palabras → sílabas) con las emisiones de la canción: un solo Viterbi no
    depende de que los tiempos de partida sean buenos ni se confunde con estribillos repetidos. `energy` = energía vocal por frame
    (misma rejilla que `logp`); sin ella no se alarga ninguna nota ni se detectan pausas. Vocales solo para español (`lang="es"`).
    Devuelve un dict por línea (start, end, words, pauses). Las pausas entre líneas no se calculan aquí."""
    recs, tokens, by_word, flat = [], [], [], []  # by_word[g] = recs de la palabra global g
    for li, line in enumerate(lines):
        for w in line:
            by_word.append([])
            flat.append(li)
            for si, text in enumerate(w):
                chars = []
                for ch in text:
                    t = _token(ch, vocab)
                    chars.append((ch, None if t is None else len(tokens)))
                    if t is not None:
                        tokens.append(t)
                recs.append({"li": li, "s": si, "text": text, "chars": chars})
                by_word[-1].append(recs[-1])
    if not tokens:
        raise ValueError("Ninguna letra del texto está en el vocabulario del modelo.")
    spans = viterbi(logp, tokens, blank)

    tokened = [r for r in recs if any(t is not None for _, t in r["chars"])]
    for r in tokened:
        ids = [t for _, t in r["chars"] if t is not None]
        r["f0"], r["f1"] = spans[ids[0]].first, spans[ids[-1]].last + 1  # f1 exclusivo
        r["conf"] = float(np.exp(np.mean([spans[t].logp for t in ids])))
    for k, r in enumerate(tokened):
        limit = tokened[k + 1]["f0"] if k + 1 < len(tokened) else len(logp)
        r["approx"] = True
        ref = float(np.max(energy[r["f0"]:r["f1"]])) if energy is not None and r["f1"] > r["f0"] else 0.0
        if ref > 0:  # la nota sigue mientras haya voz con energía comparable; se corta al llegar a la siguiente sílaba
            f = r["f1"]
            while f < min(limit, len(energy)) and energy[f] >= SUSTAIN_RATIO * ref:
                f += 1
            r["f1"], r["approx"], r["ref"] = f, False, ref
        r["start"], r["end"] = round(r["f0"] * frame_ms), round(r["f1"] * frame_ms)

    # texto sin ninguna letra del modelo (puntuación suelta, cifras): sin evidencia acústica. Si es una sola unidad entre dos
    # vecinas ocupa el hueco que dejan; en cualquier otro caso queda de largo cero para que alguien lo fije a mano.
    k = 0
    while k < len(recs):
        if "f0" in recs[k]:
            k += 1
            continue
        j = k
        while j < len(recs) and "f0" not in recs[j]:
            j += 1
        prev = recs[k - 1]["end"] if k else None
        nxt = recs[j]["start"] if j < len(recs) else None
        a = prev if prev is not None else nxt if nxt is not None else 0
        b = nxt if nxt is not None else a
        for r in recs[k:j]:
            full = j - k == 1 and prev is not None and nxt is not None and b > a
            r.update(start=a, end=b if full else a, conf=0.0, approx=True)
        k = j

    out_words = []
    for group in by_word:
        sy = []
        for r in group:
            dur = r["end"] - r["start"]
            # una duración absurda solo desconfía si el fin está medido; si no, 1 frame es el piso y no dice nada
            conf = r["conf"] * (0.5 if not r["approx"] and not MIN_DUR_MS <= dur <= MAX_DUR_MS else 1.0)
            vowel = _vowel(r, spans, r["end"], frame_ms) if lang == "es" and "f0" in r else None
            sy.append({"text": r["text"], "start": r["start"], "end": r["end"], "confidence": round(conf, 4),
                       "approximate": r["approx"], "source": "ctc", "vowels": [vowel] if vowel else []})
        out_words.append({"text": "".join(s["text"] for s in sy), "start": sy[0]["start"], "end": sy[-1]["end"],
                          "confidence": round(float(np.mean([s["confidence"] for s in sy])), 4),
                          "approximate": any(s["approximate"] for s in sy), "source": "ctc",
                          "syllables": sy if syllables else []})

    pauses = [[] for _ in lines]
    if energy is not None:
        for a, b in zip(tokened, tokened[1:]):
            if a["li"] != b["li"] or a["approx"] or b["f0"] <= a["f1"]:
                continue  # el hueco entre líneas lo marca add_line_pauses, con la geometría de las líneas ya armadas
            f = a["f1"]
            while f < min(b["f0"], len(energy)) and energy[f] < SUSTAIN_RATIO * a["ref"]:
                f += 1
            if (f - a["f1"]) * frame_ms >= MIN_PAUSE_MS:
                pauses[a["li"]].append({"start": a["end"], "end": round(f * frame_ms), "kind": "intra_line"})
    out, g = [], 0
    for li, line in enumerate(lines):
        ws = out_words[g:g + len(line)]
        g += len(line)
        out.append({"start": ws[0]["start"], "end": max(w["end"] for w in ws), "words": ws, "pauses": pauses[li]})
    return out


def shift_times(lines: list[dict], offset_ms: int) -> list[dict]:
    """Suma `offset_ms` a todos los tiempos de las líneas de `align_lines` (para el resultado de un tramo recortado de las emisiones)."""
    for line in lines:
        line["start"] += offset_ms
        line["end"] += offset_ms
        for p in line["pauses"]:
            p["start"] += offset_ms
            p["end"] += offset_ms
        for w in line["words"]:
            for u in (w, *w["syllables"], *(v for s in w["syllables"] for v in s["vowels"])):
                u["start"] += offset_ms
                u["end"] += offset_ms
    return lines


def align_line(logp: np.ndarray, words: list[list[str]], vocab: dict[str, int], **kw) -> dict:
    """Una sola línea (palabras → sílabas); ver `align_lines`."""
    return align_lines(logp, [words], vocab, **kw)[0]


def frame_energy(pcm: np.ndarray, rate: int = 16000, band: tuple[int, int] = (200, 4000), smooth: int = 3) -> np.ndarray:
    """Energía en la banda de la voz por frame de wav2vec2 (ventana de 400 muestras cada 320, la misma rejilla que las emisiones).
    Sobre la mezcla completa también cuenta la música; la separación de voz (S6) existe para limpiar esto."""
    win, hop, nfft = 400, 320, 512
    n = (len(pcm) - win) // hop + 1
    if n <= 0:
        return np.zeros(0, np.float32)
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(pcm[idx] * np.hanning(win).astype(np.float32), nfft, axis=1))
    k0, k1 = int(np.ceil(band[0] * nfft / rate)), int(band[1] * nfft / rate) + 1
    e = np.sqrt((spec[:, k0:k1] ** 2).sum(1)).astype(np.float32)
    return np.convolve(e, np.ones(smooth, np.float32) / smooth, mode="same") if smooth > 1 else e
