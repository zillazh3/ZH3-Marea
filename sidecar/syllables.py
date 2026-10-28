"""Texto → palabras → sílabas, por idioma.

Solo español se divide en sílabas (silabeador, LGPL: vive en el entorno del sidecar, nunca en la app). Para el resto no hay un silabificador
ortográfico fiable (el inglés, p. ej., no es fonémico), así que cada palabra queda entera: sin evidencia no se inventan sílabas.
Garantía: unir las sílabas de una palabra devuelve EXACTAMENTE la palabra (signos, mayúsculas y apóstrofes incluidos).
"""
import re

_EDGE = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)  # signos al borde de la palabra («¿Qué…?»)

try:
    from silabeador import syllabify as _syllabify
except ImportError:  # sin la librería no se dividen sílabas; el resto del pipeline funciona por palabra
    _syllabify = None


def available(lang: str) -> bool:
    return lang == "es" and _syllabify is not None


def syllabify_word(word: str, lang: str) -> list[str]:
    if not available(lang):
        return [word]
    pre, core, post = _EDGE.match(word).groups()
    if not core or not core.isalpha():  # cifras, apóstrofes o guiones internos: no se adivina
        return [word]
    try:
        parts = list(_syllabify(core))
    except Exception:
        return [word]
    if not parts or "".join(parts) != core:  # ante cualquier incoherencia, la palabra entera
        return [word]
    parts[0], parts[-1] = pre + parts[0], parts[-1] + post
    return parts


def split_text(text: str, lang: str) -> list[list[str]]:
    """Una línea de letra → palabras → sílabas."""
    return [syllabify_word(w, lang) for w in text.split()]
