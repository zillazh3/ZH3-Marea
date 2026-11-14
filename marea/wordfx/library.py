"""Biblioteca de categorías semánticas (JSON editable): vocabulario + vector de emoción + primitivos permitidos.

Se carga la copia del usuario (`app_data()/wordfx/library.json`) si existe; si no, la incluida en `assets/wordfx/`.
Solo vocabulario: nunca letras de canciones. Los lemas se normalizan (minúsculas, sin acentos) al cargar.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from marea.wordfx.lexicon import normalize

BUNDLED = Path(__file__).resolve().parent.parent.parent / "assets" / "wordfx" / "library.json"
LANGS = ("es", "en")
KINDS = ("emotion", "element", "month")  # las emociones pesan más que los elementos y meses al mezclar categorías
PRIMITIVES = frozenset({
    "displacement", "letter_offset", "scale", "rotation", "opacity", "blur", "letter_spacing", "gravity_drift", "jitter",
    "wave", "glow", "hue_shift", "desaturate", "chromatic", "grain", "particles", "mask_reveal", "erode", "fracture",
    "trail", "sparkle"})
# (mín, máx) de cada eje del vector de emoción
AXES = {"valence": (-1.0, 1.0), "arousal": (0.0, 1.0), "gravity": (-1.0, 1.0), "temperature": (-1.0, 1.0),
        "fragility": (0.0, 1.0), "blur": (0.0, 1.0)}


class LibraryError(ValueError):
    pass


@dataclass(frozen=True)
class Category:
    name: str
    lemmas: dict[str, frozenset[str]]  # lang -> lemas normalizados
    vector: dict[str, float]
    primitives: tuple[str, ...]
    priority: int
    kind: str = "emotion"
    color: tuple[tuple[int, int, int], ...] = ()  # color de autor (1 = fijo, 2 = de la primera a la última letra); vacío = por vector


@dataclass
class Library:
    categories: dict[str, Category]
    source: Path | None = None
    index: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # lang -> lema -> categorías (por prioridad desc.)

    def __post_init__(self):
        for lang in LANGS:
            idx: dict[str, list[str]] = {}
            for c in self.categories.values():
                for lemma in c.lemmas.get(lang, ()):
                    idx.setdefault(lemma, []).append(c.name)
            for names in idx.values():
                names.sort(key=lambda n: -self.categories[n].priority)
            self.index[lang] = idx

    @property
    def known(self) -> dict[str, set[str]]:
        """Lemas por idioma, para `lexicon.guess_lang`."""
        # shortcut: los lemas de varias palabras («para siempre») viven aquí pero no casan por token; frases = fase 3
        return {lang: set(idx) for lang, idx in self.index.items()}


def _num(name: str, key: str, v, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise LibraryError(f"«{name}»: `{key}` debe ser un número entre {lo:g} y {hi:g} (llegó {v!r}).")
    return float(v)


def _colors(name: str, raw) -> tuple[tuple[int, int, int], ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or len(raw) > 2 or not all(isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in raw):
        raise LibraryError(f"«{name}»: `color` debe ser una lista de 1 o 2 colores «#rrggbb» (llegó {raw!r}).")
    return tuple((int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)) for c in raw)


def parse(raw: dict, source: Path | None = None) -> Library:
    """Valida el JSON con mensajes claros (el archivo lo edita la persona) y devuelve la biblioteca."""
    if not isinstance(raw, dict) or not raw:
        raise LibraryError("La biblioteca debe ser un objeto JSON con una categoría por clave.")
    cats = {}
    for name, d in raw.items():
        if name.startswith("_"):  # claves de comentario
            continue
        if not isinstance(d, dict):
            raise LibraryError(f"«{name}»: debe ser un objeto.")
        lemmas = {}
        for lang, words in (d.get("lemmas") or {}).items():
            if lang not in LANGS:
                raise LibraryError(f"«{name}»: idioma desconocido «{lang}» (usa {', '.join(LANGS)}).")
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                raise LibraryError(f"«{name}»: `lemmas.{lang}` debe ser una lista de textos.")
            lemmas[lang] = frozenset(n for w in words if (n := " ".join(normalize(p) for p in w.split() if normalize(p))))
        if not any(lemmas.values()):
            raise LibraryError(f"«{name}»: no tiene lemas.")
        vec = d.get("vector")
        if not isinstance(vec, dict) or set(vec) != set(AXES):
            raise LibraryError(f"«{name}»: `vector` necesita exactamente estos ejes: {', '.join(AXES)}.")
        vector = {k: _num(name, f"vector.{k}", vec[k], *AXES[k]) for k in AXES}
        prims = d.get("primitives") or []
        bad = [p for p in prims if p not in PRIMITIVES]
        if bad:
            raise LibraryError(f"«{name}»: primitivos desconocidos {bad}. Válidos: {', '.join(sorted(PRIMITIVES))}.")
        kind = d.get("kind", "emotion")
        if kind not in KINDS:
            raise LibraryError(f"«{name}»: `kind` debe ser uno de {KINDS}.")
        prio = d.get("priority", 5)
        if isinstance(prio, bool) or not isinstance(prio, int) or not 0 <= prio <= 10:
            raise LibraryError(f"«{name}»: `priority` debe ser un entero de 0 a 10.")
        cats[name] = Category(name, lemmas, vector, tuple(prims), prio, kind, _colors(name, d.get("color", [])))
    if not cats:
        raise LibraryError("La biblioteca no tiene categorías.")
    return Library(cats, source)


def user_path() -> Path:
    from marea.paths import app_data  # perezoso: los tests de la biblioteca no necesitan Qt
    return app_data() / "wordfx" / "library.json"


def load(path: Path | None = None) -> Library:
    """`path` explícito, o la copia del usuario si existe, o la incluida. LibraryError si el JSON no es válido."""
    if path is None:
        path = user_path()
        if not path.is_file():
            path = BUNDLED
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")), path)
    except (OSError, ValueError) as e:
        if isinstance(e, LibraryError):
            raise LibraryError(f"{path.name}: {e}") from e
        raise LibraryError(f"No pude leer {path.name}: {e}") from e
