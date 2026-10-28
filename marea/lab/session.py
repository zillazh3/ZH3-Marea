"""Sesión de revisión (sin Qt): la alineación que se está corrigiendo y todo lo que se puede hacer con ella. La UI solo la dibuja.

- Editar tiempos de una línea, palabra o sílaba: se valida primero y solo se aplica si no introduce errores nuevos. Lo editado queda
  `manual_override` y ya no cuenta como «por revisar».
- El desfase global es aparte: nunca toca los tiempos de las unidades.
- Recalcular una sección usa un tramo acotado de las emisiones ya cacheadas (sin reprocesar la canción) y `merge` conserva lo fijado a mano.
"""
import copy
from pathlib import Path

from marea.export.job import unique_path
from marea.lab import alignment as A
from marea.lab.align_job import needs_review, remap_line
from marea.lab.alignment import Alignment, AlignmentError, Line, Unit, line_from_dict
from marea.lab.ttml_out import export_ttml, write_new
from marea.model.lyrics import Lyrics, parse_ttml

UNDO_LIMIT = 50
REALIGN_MARGIN_MS = 1500  # cuánto se mira antes y después de la sección al recalcularla


class Session:
    def __init__(self, alignment: Alignment, original: bytes | None = None):
        self.alignment, self.original = alignment, original
        self._undo: list[Alignment] = []

    # ---- consulta ----
    def find(self, uid: str) -> tuple[str, Unit | Line, Line] | None:
        """(tipo, unidad, línea) de un id de línea, palabra o sílaba; None si no existe."""
        for line in self.alignment.lines:
            if line.id == uid:
                return "line", line, line
            for w in line.words:
                if w.id == uid:
                    return "word", w, line
                for s in w.syllables:
                    if s.id == uid:
                        return "syllable", s, line
        return None

    def review_ids(self) -> list[str]:
        return needs_review(self.alignment)

    @property
    def text_editable(self) -> bool:
        """Con TTML de origen el texto es el del archivo (y sus referencias); sin él (Modo B) se puede reescribir."""
        return self.original is None

    # ---- edición ----
    def _push(self):
        self._undo.append(copy.deepcopy(self.alignment))
        del self._undo[:-UNDO_LIMIT]

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.alignment = self._undo.pop()
        return True

    def set_times(self, uid: str, start: int, end: int) -> list[str]:
        """Fija inicio/fin (ms) de una línea, palabra o sílaba. Devuelve los errores NUEVOS (lista vacía = aplicado)."""
        found = self.find(uid)
        if found is None:
            return [f"No existe la unidad {uid!r}."]
        if start >= end:
            return [f"El inicio ({start} ms) debe ser menor que el fin ({end} ms)."]
        cand = copy.deepcopy(self.alignment)
        kind, unit, line = Session(cand).find(uid)
        unit.start, unit.end = int(start), int(end)
        unit.manual_override, unit.approximate, unit.confidence, unit.source = True, False, 1.0, "manual"
        if kind == "syllable":  # la palabra es lo que cubren sus sílabas, y la línea lo que cubren sus palabras
            word = next(w for w in line.words if unit in w.syllables)
            unit.vowels = [v for v in unit.vowels if unit.start <= v.start and v.end <= unit.end]
            word.start, word.end = min(s.start for s in word.syllables), max(s.end for s in word.syllables)
        if kind in ("syllable", "word"):
            line.start, line.end = min(w.start for w in line.words), max(w.end for w in line.words)
        before = set(A.validate(self.alignment))
        errors = [e for e in A.validate(cand) if e not in before]
        if errors:
            return errors
        self._push()
        self.alignment = cand
        return []

    def set_offset(self, ms: int):
        """El desfase global es un ajuste aparte: se guarda como manual y no mueve ninguna unidad."""
        self._push()
        self.alignment.global_offset = {"ms": int(ms), "confidence": 1.0, "method": "manual"}

    # ---- recalcular una sección ----
    def realign_request(self, line_id: str, text: str | None = None) -> dict:
        """Parámetros (`lines`, `window`) del comando `align` para recalcular una línea con las emisiones cacheadas."""
        found = self.find(line_id)
        if found is None or found[0] != "line":
            raise ValueError(f"No existe la línea {line_id!r}.")
        line = found[1]
        if text is not None and not self.text_editable:
            raise ValueError("Con un TTML de origen el texto no se edita aquí (cámbialo en el archivo).")
        entry = ({"id": line.id, "text": text} if text is not None and text != line.text
                 else {"id": line.id, "words": [[s.text for s in w.syllables] or [w.text] for w in line.words]})
        a, b = max(0, line.start - REALIGN_MARGIN_MS), min(self.alignment.duration_ms, line.end + REALIGN_MARGIN_MS)
        return {"lines": [entry], "window": [a, b]}

    def apply_realign(self, line_id: str, data: dict) -> list[str]:
        """Integra el resultado del sidecar para esa línea. Devuelve errores (lista vacía = aplicado)."""
        old = self.find(line_id)[1]
        res = next((l for l in data["lines"] if l["id"] == line_id), None)
        if res is None:
            return ["El resultado no trae la línea pedida."]
        if self.original is not None:
            new = remap_line(old, res)
        else:
            new = line_from_dict({**res, "id": old.id, "agent": old.agent, "ref": old.ref, "source": "ctc"})
            for n, w in enumerate(new.words):
                w.id = f"{old.id}.W{n}"
                for k, s in enumerate(w.syllables):
                    s.id = f"{w.id}.S{k}"
            ws = [w.confidence for w in new.words]
            new.confidence, new.approximate = sum(ws) / len(ws), any(w.approximate for w in new.words)
        try:
            merged = A.merge(self.alignment, [line_id], [new])
        except AlignmentError as e:
            return e.errors
        self._push()
        self.alignment = A.add_line_pauses(merged)
        return []

    # ---- salida ----
    def preview_lyrics(self) -> Lyrics:
        """Lo que cargaría el reproductor: se exporta y se vuelve a leer con el parser de la app (valida de paso)."""
        return parse_ttml(export_ttml(self.alignment, self.original))

    def export(self, folder: str | Path, name: str) -> tuple[Path, Path]:
        """TTML nuevo (nunca sobrescribe) y, al lado, el alignment.v1.json con todo el detalle (vocales, pausas, confianza)."""
        ttml = write_new(self.alignment, folder, name, self.original)
        js = unique_path(ttml.parent, ttml.stem + ".alignment.json")
        A.save(self.alignment, js)
        return ttml, js
