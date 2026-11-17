"""Localization key check: python tools/check_i18n.py  (exit 1 if anything is wrong).

Reports: keys missing in a locale, `{param}` mismatches between locales, bad plural forms, keys used in code but not
defined, dynamic (non-literal) keys, catalog keys never referenced in code, and Spanish leaking into the English catalog.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from marea import i18n  # noqa: E402  (pure Python, no Qt)

KEY_RE = re.compile(r"^[a-z][A-Za-z0-9]*(\.[A-Za-z0-9]+)+$")
PARAM_RE = re.compile(r"\{(\w+)\}")
NOT_KEYS = {"ini", "json", "png", "svg", "py", "txt", "log", "mp4", "ttml", "xml", "zip", "exe", "ico", "webm", "mov", "md", "ttf", "wav", "mp3", "flac"}
SPANISH_IN_EN = re.compile(r"[áéíóúñ¿¡]")
CALLS = {"t": 0, "bind": 1}  # function name -> index of the key argument
SKIP = {ROOT / "marea" / "i18n.py", ROOT / "marea" / "ui" / "lang.py"}  # the machinery itself passes keys as variables


def _params(value) -> set[str]:
    texts = value.values() if isinstance(value, dict) else [value]
    return set().union(*(set(PARAM_RE.findall(v)) for v in texts))


def scan_code():
    """-> (referenced string constants, [(file, line, key)] used as call keys, [(file, line, why)] dynamic keys)."""
    consts, calls, dynamic = set(), [], []
    for path in sorted((ROOT / "marea").rglob("*.py")):
        if path in SKIP:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                consts.add(node.value)
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name not in CALLS or len(node.args) <= CALLS[name]:
                continue
            arg = node.args[CALLS[name]]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                calls.append((rel, node.lineno, arg.value))
            elif isinstance(arg, (ast.JoinedStr, ast.BinOp)):  # built keys can't be checked; names/lookups into label tables are fine
                dynamic.append((rel, node.lineno, f"{name}() key is built at runtime (use literal keys)"))
    return consts, calls, dynamic


def main() -> int:
    problems: list[str] = []
    cats = {c: i18n.catalog(c) for c in i18n.SUPPORTED}
    for code, cat in cats.items():
        if not cat:
            problems.append(f"[{code}] catalog is empty or failed to load")
    union = set().union(*cats.values())
    for code, cat in cats.items():
        for key in sorted(union - set(cat)):
            problems.append(f"[{code}] missing key: {key}")
    for key in sorted(union):
        present = {c: cats[c][key] for c in cats if key in cats[c]}
        if len({isinstance(v, dict) for v in present.values()}) > 1:
            problems.append(f"plural mismatch (some locales plural, some not): {key}")
        if len({frozenset(_params(v)) for v in present.values()}) > 1:
            problems.append(f"{{param}} mismatch between locales: {key} -> " + "; ".join(f"{c}={sorted(_params(v))}" for c, v in present.items()))
        for c, v in present.items():
            if isinstance(v, dict) and "other" not in v:
                problems.append(f"[{c}] plural without 'other' form: {key}")
            if not (v if isinstance(v, str) else all(v.values())):
                problems.append(f"[{c}] empty text: {key}")
    for key, v in cats.get("en", {}).items():
        texts = v.values() if isinstance(v, dict) else [v]
        if any(SPANISH_IN_EN.search(x) for x in texts):
            problems.append(f"[en] looks Spanish (accented characters): {key}")

    consts, calls, dynamic = scan_code()
    for rel, line, why in dynamic:
        problems.append(f"{rel}:{line}: {why}")
    for rel, line, key in calls:
        if key not in union:
            problems.append(f"{rel}:{line}: undefined key {key!r}")
    namespaces = {k.split(".")[0] for k in union}
    for c in sorted(consts):  # keys referenced from label tables rather than calls
        if KEY_RE.match(c) and c.split(".")[0] in namespaces and c.rsplit(".", 1)[1] not in NOT_KEYS and c not in union:
            if not any(c == k or k.startswith(c + ".") for k in union):
                problems.append(f"string looks like an undefined key: {c!r}")
    for key in sorted(union - consts):
        problems.append(f"unused key (never referenced in marea/): {key}")

    seen = set()
    out = [p for p in problems if not (p in seen or seen.add(p))]
    print("\n".join(out) if out else f"i18n OK: {len(union)} keys, locales {', '.join(i18n.SUPPORTED)}")
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
