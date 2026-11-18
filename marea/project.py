"""Proyecto de clip: la letra, los datos de la canción, su portada, configuración cinemática y persistencia (.marea.json)."""
import base64
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import skia

from marea.demo_cover import demo_cover
from marea.demo_song import exact_lyrics
from marea.i18n import t
from marea.model import background as bg_model
from marea.model import cinematic as cine_model
from marea.model.background import BackgroundConfig, BackgroundError
from marea.model.cinematic import CinematicConfig, CinematicError
from marea.model.lyrics import VALID_SOURCE_TAGS, BgVocal, Line, Lyrics, Syllable, resolve_source_tag
from marea.providers.lyrics.ttml_file import TtmlFileProvider
from marea.providers.track import Track
from marea.sysutil import is_local_path


def _migrate_v1_to_v2(d: dict) -> dict:
    """Upgrade a v1 project dict to v2 with independent provider IDs and canonical lyrics source tags."""
    out = dict(d)
    td = out.get("track")
    if isinstance(td, dict) and td.get("id"):
        td = dict(td)
        prov = str(td.get("provider") or "spotify").strip().lower()
        td["provider"] = prov
        pids = td.get("provider_ids")
        if not isinstance(pids, dict) or not pids:
            td["provider_ids"] = {prov: str(td["id"])}
        out["track"] = td
    ld = out.get("lyrics")
    if isinstance(ld, dict):
        ld = dict(ld)
        has_local = bool(str(out.get("source") or "").strip())
        manual = bool(ld.get("manual_override", False))
        tag = resolve_source_tag(str(ld.get("source") or ""), str(ld.get("provider_tag") or ""),
                                 manual_override=manual, has_local_file=has_local)
        ld["provider_tag"] = tag
        ld["manual_override"] = manual or tag == "manual"
        out["lyrics"] = ld
    out.setdefault("album", td.get("album", "") if isinstance(td, dict) else "")
    out["version"] = 2
    return out


PROJECT_SCHEMA = "marea.project"
PROJECT_VERSION = 2
PROJECT_MIGRATIONS: dict[int, Callable[[dict], dict]] = {1: _migrate_v1_to_v2}


class ProjectError(ValueError):
    pass


@dataclass
class ClipProject:
    lyrics: Lyrics
    title: str
    artist: str
    cover: skia.Image
    source: Path | None = None
    track: Track | None = None  # canción de origen (Spotify/Deezer)
    audio_file: Path | None = None  # audio propio elegido por el usuario
    audio_offset_ms: float = 0.0  # >0 = el audio del archivo va retrasado respecto a la letra
    audio_samples: object = None  # (n, 2) float32 ya decodificado; solo en memoria
    cinematic: CinematicConfig = field(default_factory=CinematicConfig)
    background: BackgroundConfig = field(default_factory=BackgroundConfig)  # portada | video propio | transparente
    background_warning: str = ""  # aviso (no bloqueante) al abrir: el video de fondo falta o cambió
    project_path: Path | None = None  # archivo .marea.json del que se abrió o donde se guardó
    album: str = ""

    def cover_png(self) -> bytes:
        return bytes(self.cover.encodeToData())  # PNG; viaja al proceso de export y al .marea.json


def demo_project() -> ClipProject:
    return ClipProject(exact_lyrics(), "Marea de luz", "Demo · letra original", demo_cover())


def project_from_ttml(path: str | Path) -> ClipProject:
    """Abre un .ttml. Levanta ValueError (mensaje claro) si no es válido; sin portada usa una de relleno."""
    path = Path(path)
    lyrics = TtmlFileProvider(path).fetch()
    if not lyrics.lines:
        raise ValueError(t("errors.project.ttmlNoTimedLines"))
    return ClipProject(lyrics, path.stem, "", demo_cover(label=False), path)


def load_cover(path: str | Path) -> skia.Image:
    img = skia.Image.open(str(path))
    if img is None:
        raise ValueError(t("errors.project.imageOpen"))
    return img


# ---------------- persistencia versionada (.marea.json, marea.project v1) ----------------

def _syl_to_dict(s: Syllable) -> dict:
    return {"text": s.text, "begin": s.begin, "end": s.end, "word_end": bool(s.word_end)}


def _obj(v, where: str) -> dict:
    if not isinstance(v, dict):
        raise ProjectError(t("errors.project.notObject", where=where))
    return v


def _list(v, where: str) -> list:
    if not isinstance(v, list):
        raise ProjectError(t("errors.project.notList", where=where))
    return v


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ProjectError(t("errors.project.notNumber", where=where, value=repr(v)))
    return v


def _span(d: dict, a: str, b: str, where: str) -> tuple[float, float]:
    """Intervalo obligatorio, numérico y finito, que no termina antes de empezar."""
    for key in (a, b):
        if key not in d:
            raise ProjectError(t("errors.project.missingKey", where=where, field=key))
    start, end = _num(d[a], f"{where}.{a}"), _num(d[b], f"{where}.{b}")
    if end < start:
        raise ProjectError(t("errors.project.endsBeforeStart", where=where, start=start, end=end))
    return start, end


def _syl_from_dict(d, where: str) -> Syllable:
    d = _obj(d, where)
    if not isinstance(d.get("text"), str):
        raise ProjectError(t("errors.project.textMissing", where=where))
    begin, end = _span(d, "begin", "end", where)
    return Syllable(d["text"], begin, end, bool(d.get("word_end", False)))


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
        "provider_tag": lyrics.resolved_source_tag,
        "manual_override": bool(lyrics.manual_override),
        "lines": [_line_to_dict(l) for l in lyrics.lines],
    }


def lyrics_from_dict(d: dict) -> Lyrics:
    if not isinstance(d, dict) or not isinstance(d.get("lines"), list):
        raise ProjectError(t("errors.project.noLyrics"))
    lines: list[Line] = []
    # Solo se valida cada intervalo: versos solapados, duetos y voces de fondo son datos válidos.
    for i, ld in enumerate(d["lines"], 1):
        where = t("errors.project.whereLine", i=i)
        ld = _obj(ld, where)
        syls = [_syl_from_dict(s, t("errors.project.whereSyl", where=where, j=j))
                for j, s in enumerate(_list(ld.get("syllables") or [], f"{where}.syllables"), 1)]
        bgs = []
        for k, bd in enumerate(_list(ld.get("bg") or [], f"{where}.bg"), 1):
            bw = t("errors.project.whereBg", where=where, k=k)
            bd = _obj(bd, bw)
            if not bd.get("syllables"):
                continue
            bs, be = _span(bd, "start", "end", bw)
            bgs.append(BgVocal(bs, be, [_syl_from_dict(s, t("errors.project.whereSyl", where=bw, j=j))
                                        for j, s in enumerate(_list(bd["syllables"], f"{bw}.syllables"), 1)],
                               role=str(bd.get("role", "backing")), agent=str(bd.get("agent", ""))))
        if not syls and not bgs:
            continue
        start, end = _span(ld, "start", "end", where)
        lines.append(Line(start, end, str(ld.get("agent", "v1")), syls, bgs, bool(ld.get("approx", False)),
                          role=str(ld.get("role", "")), raw_agent=str(ld.get("raw_agent", ""))))
    if not lines:
        raise ProjectError(t("errors.project.noTimedLines"))
    src = str(d.get("source", ""))
    raw_tag = str(d.get("provider_tag") or "").strip().lower()
    manual = bool(d.get("manual_override", False))
    tag = raw_tag if raw_tag in VALID_SOURCE_TAGS else resolve_source_tag(src, manual_override=manual)
    return Lyrics(lines, src, str(d.get("credit", "")), [str(w) for w in (d.get("writers") or [])],
                  provider_tag=tag, manual_override=manual)


def project_to_dict(project: ClipProject, editor_state: dict | None = None) -> dict:
    return {
        "schema": PROJECT_SCHEMA,
        "version": PROJECT_VERSION,
        "title": project.title,
        "artist": project.artist,
        "album": project.album,
        "cover_png_b64": base64.b64encode(project.cover_png()).decode("ascii"),
        "source": str(project.source) if project.source else "",
        "track": asdict(project.track) if project.track else None,
        "audio": {
            "path": str(project.audio_file) if project.audio_file else "",
            "offset_ms": float(project.audio_offset_ms),
        },
        "lyrics": lyrics_to_dict(project.lyrics),
        "cinematic": cine_model.to_dict(project.cinematic),
        "background": bg_model.to_dict(project.background),
        "editor": editor_state or {},
    }


def project_from_dict(raw: dict) -> tuple[ClipProject, dict]:
    if not isinstance(raw, dict):
        raise ProjectError(t("errors.project.notJson"))
    if raw.get("schema") != PROJECT_SCHEMA:
        raise ProjectError(t("errors.project.unsupportedFormat", schema=repr(raw.get("schema"))))
    try:
        ver = int(raw.get("version", 0))
    except (TypeError, ValueError):
        raise ProjectError(t("errors.project.badVersion", version=repr(raw.get("version")))) from None
    if ver < 1 or ver > PROJECT_VERSION:
        raise ProjectError(t("errors.project.unsupportedVersion", ver=ver, max=PROJECT_VERSION))
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
        raw_artists = td.get("artists")
        if raw_artists is not None and not isinstance(raw_artists, list):
            raise ProjectError(t("errors.project.artistsNotList"))
        raw_pids = td.get("provider_ids")
        if raw_pids is not None and not isinstance(raw_pids, dict):
            raw_pids = None
        track = Track(
            id=str(td["id"]),
            title=str(td.get("title", "")),
            artists=[str(a) for a in (raw_artists or [])],
            album=str(td.get("album", "")),
            duration_ms=int(_num(td.get("duration_ms", 0), "track.duration_ms")),
            cover_url=td.get("cover_url"),
            uri=str(td.get("uri", "")),
            provider=str(td.get("provider") or "spotify"),
            provider_ids={str(k): str(v) for k, v in (raw_pids or {}).items() if k and v},
        )

    audio = _obj(d.get("audio") or {}, "audio")
    audio_path_str = str(audio.get("path") or "").strip()
    audio_file = Path(audio_path_str) if audio_path_str and is_local_path(audio_path_str) else None
    audio_offset_ms = float(_num(audio.get("offset_ms", 0.0), "audio.offset_ms"))

    try:
        cinematic = cine_model.from_dict(d.get("cinematic"))
    except CinematicError as e:
        raise ProjectError(str(e)) from e

    try:
        background, bg_warning = bg_model.check_video(bg_model.from_dict(d.get("background")))
    except BackgroundError as e:
        raise ProjectError(str(e)) from e

    src_str = str(d.get("source") or "").strip()
    album = str(d.get("album") or (track.album if track else "") or "")
    project = ClipProject(
        lyrics=lyrics,
        title=str(d.get("title", "")),
        artist=str(d.get("artist", "")),
        cover=cover,
        source=Path(src_str) if src_str and is_local_path(src_str) else None,
        track=track,
        audio_file=audio_file,
        audio_offset_ms=audio_offset_ms,
        cinematic=cinematic,
        background=background,
        background_warning=bg_warning or "",
        album=album,
    )
    editor_state = d.get("editor") if isinstance(d.get("editor"), dict) else {}
    return project, editor_state


def save_project(project: ClipProject, path: str | Path, editor_state: dict | None = None) -> Path:
    import os
    import tempfile

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = project_to_dict(project, editor_state=editor_state)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.stem}_", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp_path, out)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    project.project_path = out
    return out


def load_project_file(path: str | Path) -> tuple[ClipProject, dict]:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProjectError(t("errors.project.cannotRead", error=e)) from e
    project, editor_state = project_from_dict(raw)
    project.project_path = p
    return project, editor_state
