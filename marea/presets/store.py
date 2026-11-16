"""Corrientes en disco: integradas (solo lectura, assets/presets) y del usuario (data/presets)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from marea.presets.schema import MAX_BYTES, SECTIONS, PresetError, validate
from marea.sysutil import safe_filename

BUILTIN_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "presets"
BUILTIN_ORDER = ("Bajamar", "Pleamar", "Niebla", "Abisal")


@dataclass
class PresetInfo:
    name: str
    path: Path
    builtin: bool
    preset: dict
    flags: list[str] = field(default_factory=list)

    @property
    def signature(self) -> str:
        """Hash del look (sin nombre ni fechas): cambia solo si cambia lo que se ve; sirve de clave de la miniatura."""
        body = json.dumps({s: self.preset.get(s) for s in SECTIONS}, sort_keys=True)
        return hashlib.sha1(body.encode()).hexdigest()[:16]


def user_dir() -> Path:
    from marea.paths import app_data
    return app_data() / "presets"


def load_file(path: Path, installed_fonts=None):
    """Lee y valida un archivo de Corriente. Rechaza lo que pese más de 256 KB antes de leerlo."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise PresetError("El archivo pesa más de 256 KB: no parece una Corriente.")
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as e:  # ValueError cubre JSON y UnicodeDecodeError
        raise PresetError(f"No pude leer la Corriente: {e}") from e
    return validate(raw, installed_fonts)


class PresetStore:
    def __init__(self, builtin_dir: Path | None = None, user: Path | None = None):
        self.builtin_dir = Path(builtin_dir) if builtin_dir else BUILTIN_DIR
        self.user_dir = Path(user) if user else user_dir()

    # ---- lectura ----
    def list(self, installed_fonts=None) -> list[PresetInfo]:
        """Integradas en su orden, luego las del usuario por nombre. Los archivos inválidos se omiten (no rompen la lista)."""
        found: list[PresetInfo] = []
        for folder, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                try:
                    v = load_file(p, installed_fonts)
                except PresetError:
                    continue
                name = v.preset["name"] or p.stem
                if not builtin and any(i.name.casefold() == name.casefold() for i in found):
                    continue  # una del usuario no puede esconder a una existente
                found.append(PresetInfo(name, p, builtin, {**v.preset, "name": name}, v.flags))
        rank = {n.casefold(): i for i, n in enumerate(BUILTIN_ORDER)}
        found.sort(key=lambda i: (not i.builtin, rank.get(i.name.casefold(), 99), i.name.casefold()))
        return found

    def get(self, name: str, installed_fonts=None) -> PresetInfo | None:
        return next((i for i in self.list(installed_fonts) if i.name.casefold() == name.casefold()), None)

    def files_to_watch(self) -> tuple[Path, ...]:
        return tuple(p for d in (self.builtin_dir, self.user_dir) if d.is_dir() for p in d.glob("*.json"))

    # ---- escritura (solo del usuario) ----
    def _check_name(self, name: str, ignore: str = "") -> str:
        name = name.strip()
        if not name:
            raise PresetError("Ponle un nombre a la Corriente.")
        if len(name) > 60:
            raise PresetError("El nombre es demasiado largo (máximo 60 letras).")
        for i in self.list():
            if i.name.casefold() == name.casefold() and i.name.casefold() != ignore.casefold():
                raise PresetError(f"Ya existe una Corriente llamada «{i.name}».")
        return name

    def _user_info(self, name: str) -> PresetInfo:
        info = self.get(name)
        if info is None:
            raise PresetError(f"No encuentro la Corriente «{name}».")
        if info.builtin:
            raise PresetError("Las Corrientes integradas son de solo lectura: duplícala para editarla.")
        return info

    def _write(self, path: Path, preset: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".preset_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(preset, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save(self, name: str, preset: dict) -> PresetInfo:
        """Guarda como nueva Corriente del usuario (`preset`: salida de `capture` o `validate`)."""
        name = self._check_name(name)
        now = datetime.now().isoformat(timespec="seconds")
        data = validate({**preset, "name": name, "created": now, "updated": now}).preset
        path = self.user_dir / (safe_filename(name, "Corriente") + ".json")
        n = 2
        while path.exists():
            path = self.user_dir / f"{safe_filename(name, 'Corriente')} ({n}).json"
            n += 1
        self._write(path, data)
        return PresetInfo(name, path, False, data)

    def update(self, name: str, preset: dict) -> PresetInfo:
        """Reemplaza el look de una Corriente del usuario conservando nombre, descripción y fecha de creación."""
        info = self._user_info(name)
        data = validate({**preset, "name": info.name, "description": info.preset.get("description", ""),
                         "created": info.preset.get("created"), "updated": datetime.now().isoformat(timespec="seconds")}).preset
        self._write(info.path, data)
        return PresetInfo(info.name, info.path, False, data)

    def rename(self, name: str, new_name: str) -> PresetInfo:
        info = self._user_info(name)
        new_name = self._check_name(new_name, ignore=info.name)
        data = {**info.preset, "name": new_name, "updated": datetime.now().isoformat(timespec="seconds")}
        self._write(info.path, data)
        return PresetInfo(new_name, info.path, False, data)

    def duplicate(self, name: str, new_name: str = "") -> PresetInfo:
        info = self.get(name)
        if info is None:
            raise PresetError(f"No encuentro la Corriente «{name}».")
        if new_name.strip():
            return self.save(new_name, info.preset)
        base, n, taken = f"{info.name} (copia)", 2, {i.name.casefold() for i in self.list()}
        candidate = base
        while candidate.casefold() in taken:
            candidate, n = f"{base} {n}", n + 1
        return self.save(candidate, info.preset)

    def delete(self, name: str):
        self._user_info(name).path.unlink(missing_ok=True)

    def import_file(self, path: Path, installed_fonts=None) -> PresetInfo:
        """Importa un .json: se valida y se guarda una copia normalizada (nunca el archivo original tal cual)."""
        v = load_file(path, installed_fonts)
        name = v.preset["name"] or Path(path).stem
        n, candidate = 2, name
        while any(i.name.casefold() == candidate.casefold() for i in self.list()):
            candidate, n = f"{name} ({n})", n + 1
        info = self.save(candidate, v.preset)
        info.flags = v.flags
        return info

    def export_file(self, name: str, dest: Path):
        info = self.get(name)
        if info is None:
            raise PresetError(f"No encuentro la Corriente «{name}».")
        self._write(Path(dest), info.preset)
