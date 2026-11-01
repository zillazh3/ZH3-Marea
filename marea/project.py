"""Proyecto de clip: la letra, los datos de la canción, su portada, configuración cinemática y persistencia (.marea.json)."""
import base64
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import skia

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.model import cinematic as cine_model
from marea.model.cinematic import CinematicConfig, CinematicError
from marea.model.lyrics import BgVocal, Line, Lyrics, Syllable
from marea.providers.lyrics.ttml_file import TtmlFileProvider
from marea.providers.spotify import Track

PROJECT_SCHEMA = "marea.project"
PROJECT_VERSION = 1
PROJECT_MIGRATIONS: dict[int, Callable[[dict], dict]] = {}  # v -> transformador a v+1 cuando suba PROJECT_VERSION


class ProjectError(ValueError):
    pass


@dataclass
class ClipProject:
    lyrics: Lyrics
    title: str
    artist: str
    cover: skia.Image
    source: Path | None = None
    track: Track | None = None  # canción de Spotify de la que viene (permite grabar su audio por loopback)
    audio_file: Path | None = None  # audio propio elegido por el usuario
    audio_offset_ms: float = 0.0  # >0 = el audio del archivo va retrasado respecto a la letra
    audio_samples: object = None  # (n, 2) float32 ya decodificado; solo en memoria
    cinematic: CinematicConfig = field(default_factory=CinematicConfig)
    project_path: Path | None = None  # archivo .marea.json del que se abrió o donde se guardó

    def cover_png(self) -> bytes:
        return bytes(self.cover.encodeToData())  # PNG; viaja al proceso de export y al .marea.json


def demo_project() -> ClipProject:
    return ClipProject(exact_lyrics(), "Marea de luz", "Demo · letra original", demo_cover())


def project_from_ttml(path: str | Path) -> ClipProject:
    """Abre un .ttml. Levanta ValueError (mensaje claro) si no es válido; sin portada usa una de relleno."""
    path = Path(path)
    lyrics = TtmlFileProvider(path).fetch()
    if not lyrics.lines:
        raise ValueError("El archivo no tiene versos con tiempos.")
    return ClipProject(lyrics, path.stem, "", demo_cover(label=False), path)


def load_cover(path: str | Path) -> skia.Image:
    img = skia.Image.open(str(path))
    if img is None:
        raise ValueError("No pude abrir esa imagen. Usa un PNG, JPG o WebP.")
    return img


# ---------------- persistencia versionada (.marea.json, marea.project v1) ----------------

def _syl_to_dict(s: Syllable) -> dict:
    return {"text": s.text, "begin": s.begin, "end": s.end, "word_end": bool(s.word_end)}


def _syl_from_dict(d: dict) -> Syllable:
    return Syllable(str(d["text"]), d["begin"], d["end"], bool(d.get("word_end", False)))


def _bg_to_dict(b: BgVocal) -> dict:
    d = {"start": b.start, "end": b.end, "syllables": [_syl_to_dict(s) for s in b.syllables]}
    if b.role and b.role != "backing":
        d["role"] = b.role
    if b.agent:
        d["agent"] = b.agent
    return d


def _line_to_dict(l: Line) -> dict:
    d = {
        "start": l.start,
        "end": l.end,
        "agent": l.agent,
        "approx": bool(l.approx),
        "syllables": [_syl_to_dict(s) for s in l.syllables],
        "bg": [_bg_to_dict(b) for b in l.bg],
    }
    if l.role:
        d["role"] = l.role
    if l.raw_agent:
        d["raw_agent"] = l.raw_agent
    return d


def lyrics_to_dict(lyrics: Lyrics) -> dict:
    return {
        "source": lyrics.source,
        "credit": lyrics.credit,
        "writers": list(lyrics.writers),
        "lines": [_line_to_dict(l) for l in lyrics.lines],
    }


def lyrics_from_dict(d: dict) -> Lyrics:
    if not isinstance(d, dict) or not isinstance(d.get("lines"), list):
        raise ProjectError("El proyecto no contiene una letra válida.")
    lines: list[Line] = []
    for ld in d["lines"]:
        syls = [_syl_from_dict(s) for s in ld.get("syllables") or []]
        bgs = [
            BgVocal(bd["start"], bd["end"], [_syl_from_dict(s) for s in bd.get("syllables") or []],
                    role=str(bd.get("role", "backing")), agent=str(bd.get("agent", "")))
            for bd in (ld.get("bg") or [])
            if bd.get("syllables")
        ]
        if not syls and not bgs:
            continue
        lines.append(Line(ld["start"], ld["end"], str(ld.get("agent", "v1")), syls, bgs, bool(ld.get("approx", False)),
                          role=str(ld.get("role", "")), raw_agent=str(ld.get("raw_agent", ""))))
    if not lines:
        raise ProjectError("El proyecto no tiene versos con tiempos.")
    return Lyrics(lines, str(d.get("source", "")), str(d.get("credit", "")), [str(w) for w in (d.get("writers") or [])])


def project_to_dict(project: ClipProject, editor_state: dict | None = None) -> dict:
    return {
        "schema": PROJECT_SCHEMA,
        "version": PROJECT_VERSION,
        "title": project.title,
        "artist": project.artist,
        "cover_png_b64": base64.b64encode(project.cover_png()).decode("ascii"),
        "source": str(project.source) if project.source else "",
        "track": asdict(project.track) if project.track else None,
        "audio": {
            "path": str(project.audio_file) if project.audio_file else "",
            "offset_ms": float(project.audio_offset_ms),
        },
        "lyrics": lyrics_to_dict(project.lyrics),
        "cinematic": cine_model.to_dict(project.cinematic),
        "editor": editor_state or {},
    }


def project_from_dict(raw: dict) -> tuple[ClipProject, dict]:
    if not isinstance(raw, dict):
        raise ProjectError("El archivo de proyecto no es un JSON válido.")
    if raw.get("schema") != PROJECT_SCHEMA:
        raise ProjectError(f"Formato de proyecto no soportado: {raw.get('schema')!r}.")
    ver = int(raw.get("version", 0))
    if ver < 1 or ver > PROJECT_VERSION:
        raise ProjectError(f"Versión de proyecto no soportada: v{ver} (esta app lee hasta v{PROJECT_VERSION}).")
    d = dict(raw)
    while ver < PROJECT_VERSION:
        d = PROJECT_MIGRATIONS[ver](d)
        ver = int(d.get("version", ver + 1))

    lyrics = lyrics_from_dict(d.get("lyrics") or {})
    cover = None
    b64 = d.get("cover_png_b64") or ""
    if b64:
        try:
            png = base64.b64decode(b64)
            cover = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(png))
        except Exception:
            cover = None
    if cover is None:
        cover = demo_cover(label=False)

    td = d.get("track")
    track = None
    if isinstance(td, dict) and td.get("id"):
        track = Track(
            id=str(td["id"]),
            title=str(td.get("title", "")),
            artists=[str(a) for a in (td.get("artists") or [])],
            album=str(td.get("album", "")),
            duration_ms=int(td.get("duration_ms", 0)),
            cover_url=td.get("cover_url"),
            uri=str(td.get("uri", "")),
        )

    audio = d.get("audio") or {}
    audio_path_str = str(audio.get("path") or "").strip()
    audio_file = Path(audio_path_str) if audio_path_str else None
    audio_offset_ms = float(audio.get("offset_ms", 0.0))

    try:
        cinematic = cine_model.from_dict(d.get("cinematic"))
    except CinematicError as e:
        raise ProjectError(str(e)) from e

    src_str = str(d.get("source") or "").strip()
    project = ClipProject(
        lyrics=lyrics,
        title=str(d.get("title", "")),
        artist=str(d.get("artist", "")),
        cover=cover,
        source=Path(src_str) if src_str else None,
        track=track,
        audio_file=audio_file,
        audio_offset_ms=audio_offset_ms,
        cinematic=cinematic,
    )
    editor_state = d.get("editor") if isinstance(d.get("editor"), dict) else {}
    return project, editor_state


def save_project(project: ClipProject, path: str | Path, editor_state: dict | None = None) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = project_to_dict(project, editor_state=editor_state)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    project.project_path = out
    return out


def load_project_file(path: str | Path) -> tuple[ClipProject, dict]:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProjectError(f"No pude leer el proyecto: {e}") from e
    project, editor_state = project_from_dict(raw)
    project.project_path = p
    return project, editor_state
