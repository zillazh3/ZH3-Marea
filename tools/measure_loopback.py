"""Mide la captura por loopback con hardware real: reproduce clics por un dispositivo y los captura con su loopback.

    python tools/measure_loopback.py ["nombre del dispositivo loopback"]     (sin nombre: la salida por defecto)

Reporta si los clics llegan íntegros, su separación (debe ser exactamente la pedida) y la latencia
«reproducir -> capturar» del equipo. Suena ~3 s de clics muy bajos.
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from marea.audio import align  # noqa: E402
from marea.audio.loopback import LoopbackRecorder  # noqa: E402

CLICK_EVERY = 0.5
N_CLICKS = 5
AMP = 0.15
SUFFIX = " [Loopback]"


def measure(loopback_name: str = "") -> dict:
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        if not loopback_name:
            loopback_name = p.get_default_wasapi_loopback()["name"]
        # el dispositivo de salida que corresponde a ese loopback: mismo nombre sin el sufijo
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        out = next(p.get_device_info_by_index(i) for i in range(p.get_device_count())
                   if p.get_device_info_by_index(i)["name"] == loopback_name.removesuffix(SUFFIX)
                   and p.get_device_info_by_index(i)["maxOutputChannels"] > 0
                   and p.get_device_info_by_index(i)["hostApi"] == wasapi)
        rate, ch = int(out["defaultSampleRate"]), out["maxOutputChannels"]
        rec = LoopbackRecorder(loopback_name)
        rec.start()
        stream = p.open(format=pa.paFloat32, channels=ch, rate=rate, output=True, output_device_index=out["index"],
                        frames_per_buffer=256)
        n = int(0.004 * rate)
        click = (AMP * np.hanning(n) * np.sin(2 * np.pi * 1500 * np.arange(n) / rate)).astype(np.float32)
        wrote = []
        time.sleep(0.3)
        t_start = time.perf_counter()
        for k in range(N_CLICKS):
            while time.perf_counter() < t_start + k * CLICK_EVERY - 0.002:
                time.sleep(0.001)
            block = np.zeros((rate // 10, ch), np.float32)
            block[:n, :] = click[:, None]
            wrote.append(time.perf_counter())
            stream.write(block.tobytes())
        time.sleep(0.4)
        stream.stop_stream()
        stream.close()
        recording = rec.stop()
    finally:
        p.terminate()
    mono = recording.samples.max(axis=1) if recording.samples.size else np.zeros(1, np.float32)
    onsets = align.find_onsets(mono, recording.rate, threshold=0.4, min_gap_s=0.2)
    times = [recording.t0 + o for o in onsets]
    k = min(len(times), len(wrote))
    return {"device": loopback_name, "rate": rate, "clicks_sent": N_CLICKS, "clicks_captured": len(onsets),
            "latency_ms": [(times[i] - wrote[i]) * 1000 for i in range(k)],
            "spacing_ms": [(times[i + 1] - times[i]) * 1000 for i in range(len(times) - 1)],
            "overflows": rec.overflows, "captured_s": recording.duration, "peak": float(np.abs(mono).max())}


if __name__ == "__main__":
    r = measure(sys.argv[1] if len(sys.argv) > 1 else "")
    print(f"dispositivo: {r['device']} @ {r['rate']} Hz | capturó {r['captured_s']:.2f} s | pico {r['peak']:.3f} | desbordes: {r['overflows']}")
    print(f"clics enviados {r['clicks_sent']} -> capturados {r['clicks_captured']}")
    if r["spacing_ms"]:
        print("separación entre clics (ms):", [round(s, 1) for s in r["spacing_ms"]], f"(pedida {CLICK_EVERY * 1000:.0f})")
    if r["latency_ms"]:
        lat = np.array(r["latency_ms"])
        print(f"latencia reproducir->capturar: media {lat.mean():.1f} ms, desviación {lat.std():.1f} ms, mín {lat.min():.1f}, máx {lat.max():.1f}")
