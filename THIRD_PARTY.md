# Avisos de terceros

ZH3 Marea es software libre bajo **GNU AGPL-3.0** (ver `LICENSE`), © 2026 Zilla-ZH3. Esta lista resume de dónde viene lo que no es mío.

## Ideas y valores portados al motor (AGPL-3.0)

| Proyecto | Qué se tomó |
|---|---|
| [Apple Music-like Lyrics (AMLL)](https://github.com/amll-dev/applemusic-like-lyrics) | valores de scroll, blur e interludio |
| [Spicy Lyrics](https://github.com/Spikerko/spicy-lyrics) | curvas de progreso de la sílaba, springs por sílaba y letras; también es una fuente de letras (API) |
| `spr` de Fraktality | spring analítico por frecuencia y ratio de amortiguación |

Por eso este proyecto también es AGPL-3.0: si lo modificas y lo ofreces (incluso como servicio por red), publica tu código bajo la misma licencia y conserva estos créditos.

## Se distribuye dentro del portable

| Componente | Licencia |
|---|---|
| [Inter Tight](https://fonts.google.com/specimen/Inter+Tight) | SIL OFL 1.1 (`assets/fonts/OFL.txt`) |
| [Lucide](https://lucide.dev) (íconos) | ISC (`assets/icons/LICENSE.txt`) |
| [Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/) | LGPL-3.0 |
| [skia-python](https://github.com/kyamagu/skia-python) | BSD-3-Clause |
| [ffmpeg](https://ffmpeg.org), build de [Gyan](https://www.gyan.dev/ffmpeg/builds/) | **GPL-3.0**; el código fuente está en <https://www.gyan.dev/ffmpeg/builds/> y <https://ffmpeg.org/download.html> |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | MIT |
| keyring, numpy, httpx, defusedxml, fonttools, PyYAML | MIT / BSD (cada paquete trae su licencia en `_internal/*.dist-info/`) |
| Python y PyInstaller | PSF / GPL con excepción para el bootloader |

## Sincronía experimental (se instala solo si lo pides, no viaja en el portable)

torch, CTranslate2, faster-whisper, onnxruntime y sus dependencias (BSD / MIT / Apache-2.0).
Modelos que se descargan bajo demanda, cada uno con **su propia licencia**: `facebook/wav2vec2-base-960h`,
`jonatasgrosman/wav2vec2-large-xlsr-53-spanish`, Whisper (OpenAI) y los pesos de separación de voz (BS-Roformer), que vienen de un repositorio
comunitario **sin licencia declarada**: úsalos solo para pruebas personales.

## Datos de evaluación

[JamendoLyrics](https://huggingface.co/datasets/jamendolyrics/jamendolyrics) (CC con NC/ND) se usó solo para medir, en local; no se redistribuye.

## Marcas

Apple Music, Spotify, TikTok y Tidal son marcas de sus dueños. Este proyecto no está afiliado ni respaldado por ellos.
