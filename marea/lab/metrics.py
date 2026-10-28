"""Métricas de alineación contra una referencia (tiempos en ms)."""
import statistics


def word_errors(pred: list[tuple[int, int]], truth: list[tuple[int, int]]) -> dict:
    """Error por palabra de inicio y de fin. `bias` es el error con signo (>0 = el alineador llega tarde): si es grande y estable no
    hay palabras mal alineadas sino un desfase sistemático. `within50/100` = fracción con |error| ≤ 50 / 100 ms."""
    if len(pred) != len(truth) or not truth:
        raise ValueError("pred y truth deben tener la misma longitud (y no estar vacíos).")
    out = {"n": len(truth)}
    for name, i in (("start", 0), ("end", 1)):
        err = [p[i] - t[i] for p, t in zip(pred, truth)]
        ab = [abs(e) for e in err]
        out[name] = {"mae": round(sum(ab) / len(ab), 1), "median_abs": round(statistics.median(ab), 1), "bias": round(statistics.median(err), 1),
                     "within50": round(sum(a <= 50 for a in ab) / len(ab), 3), "within100": round(sum(a <= 100 for a in ab) / len(ab), 3)}
    return out
