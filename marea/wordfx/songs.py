"""Overrides por canción: `app_data()/wordfx/songs/<track_id>.json` fuerza, cambia o desactiva palabras concretas.

```json
{"overrides": [
  {"line": 3, "word": 2, "disable": true},
  {"line": 3, "word": 2, "categories": ["sadness"], "vector": {"arousal": 0.9}, "intensity": 1.3},
  {"lemma": "tiempo", "disable": true}
]}
```
El mismo archivo guarda también el perfil de ánimo editable (`"profile": {...}`, ver `profile.py`).
`line` y `word` cuentan desde 0 (verso de la letra y palabra dentro del verso, como las ve el motor). `lemma` aplica a todas
las apariciones. Con `categories` la palabra se fuerza aunque no estuviera en la biblioteca.
"""
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from marea.model.lyrics import Lyrics
from marea.wordfx.fxtypes import AXES
from marea.wordfx.lexicon import normalize
from marea.wordfx.library import Library
from marea.wordfx.matcher import Match, word_groups


class SongError(ValueError):
    pass


@dataclass(frozen=True)
class Override:
    line: int | None = None
    word: int | None = None
    lemma: str = ""
    disable: bool = False
    categories: tuple[str, ...] = ()
    vector: dict = field(default_factory=dict)  # eje -> valor; reemplaza el eje del vector final
    intensity: float = 1.0  # multiplica la intensidad global de esa palabra


def path_for(track_id: str) -> Path:
    from marea.paths import app_data  # perezoso: los tests no necesitan Qt
    return app_data() / "wordfx" / "songs" / (re.sub(r"[^A-Za-z0-9_.-]", "_", track_id) + ".json")


def parse(raw: dict) -> list[Override]:
    items = raw.get("overrides", []) if isinstance(raw, dict) else None  # un archivo solo con perfil no trae overrides
    if not isinstance(items, list):
        raise SongError("El archivo debe ser un objeto y «overrides» una lista.")
    out = []
    for i, d in enumerate(items, 1):
        where = f"override {i}"
        if not isinstance(d, dict):
            raise SongError(f"{where}: debe ser un objeto.")
        line, word, lemma = d.get("line"), d.get("word"), normalize(str(d.get("lemma", "")))
        if not lemma and (not isinstance(line, int) or not isinstance(word, int) or line < 0 or word < 0):
            raise SongError(f"{where}: usa `line` y `word` (enteros desde 0) o `lemma`.")
        cats = d.get("categories", [])
        if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
            raise SongError(f"{where}: `categories` debe ser una lista de textos.")
        vec = d.get("vector", {})
        if not isinstance(vec, dict) or set(vec) - set(AXES) or not all(isinstance(v, (int, float)) for v in vec.values()):
            raise SongError(f"{where}: `vector` solo admite ejes numéricos de {', '.join(AXES)}.")
        inten = d.get("intensity", 1.0)
        if isinstance(inten, bool) or not isinstance(inten, (int, float)) or not 0 <= inten <= 3:
            raise SongError(f"{where}: `intensity` debe ser un número entre 0 y 3.")
        out.append(Override(line if not lemma else None, word if not lemma else None, lemma, bool(d.get("disable", False)),
                            tuple(cats), dict(vec), float(inten)))
    return out


def _read(track_id: str) -> dict:
    """JSON del archivo de la canción ({} si no existe). SongError si no se puede leer o no es un objeto."""
    p = path_for(track_id)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise SongError(f"No pude leer {p.name}: {e}") from e
    except ValueError as e:
        raise SongError(f"{p.name}: JSON inválido ({e}).") from e
    if not isinstance(raw, dict):
        raise SongError(f"{p.name}: debe ser un objeto JSON.")
    return raw


def load(track_id: str) -> list[Override]:
    """Overrides de la canción, o [] si no hay archivo. SongError si el JSON no es válido."""
    try:
        return parse(_read(track_id))
    except SongError as e:
        raise SongError(f"{path_for(track_id).name}: {e}") from e


def load_profile(track_id: str):
    """`SongProfile` guardado para la canción o None. SongError si el perfil no es válido."""
    from marea.wordfx.profile import ProfileError, SongProfile  # perezoso: solo existe con la función activa
    raw = _read(track_id).get("profile")
    if raw is None:
        return None
    try:
        return SongProfile.from_dict(raw)
    except ProfileError as e:
        raise SongError(f"{path_for(track_id).name}: perfil no válido: {e}") from e


def save_profile(track_id: str, profile) -> Path:
    """Guarda el perfil en el archivo de la canción conservando sus overrides (y cualquier otra clave)."""
    raw = _read(track_id)
    raw["profile"] = profile.to_dict()
    p = path_for(track_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # un cierre a medias no deja el archivo cortado
    return p


def apply(matches: dict[tuple[int, int], Match], lyrics: Lyrics, lib: Library, overrides: list[Override]):
    """-> (matches, vector_overrides, intensity_mult), los dos últimos por clave (verso, palabra). Las más específicas
    (verso+palabra) ganan a las de `lemma`, y dentro del mismo tipo gana la última del archivo."""
    out, vec, mult = dict(matches), {}, {}
    for o in sorted(overrides, key=lambda o: o.lemma == ""):  # estable: primero las de lemma
        keys = [k for k, m in out.items() if m.lemma == o.lemma] if o.lemma else [(o.line, o.word)]
        for key in keys:
            if o.disable:
                out.pop(key, None)
                continue
            cats = tuple(c for c in o.categories if c in lib.categories)  # categorías que no existen se ignoran
            m = out.get(key)
            if m is None and cats and key[0] < len(lyrics.lines):
                groups = word_groups(lyrics.lines[key[0]].syllables)
                if key[1] < len(groups):
                    g = groups[key[1]]
                    text = "".join(s.text for s in g)
                    m = Match(key[0], key[1], text, normalize(text), "es", cats, g[0].begin, g[-1].end, note="override")
            if m is None:
                continue
            out[key] = replace(m, categories=cats or m.categories, note="override" if cats else m.note)
            if o.vector:
                vec[key] = {**vec.get(key, {}), **o.vector}
            if o.intensity != 1.0:
                mult[key] = o.intensity
    return out, vec, mult
