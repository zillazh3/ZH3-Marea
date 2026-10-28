class ProviderError(Exception):
    """Fallo de un proveedor (Spotify, LRCLIB…). El mensaje está escrito para mostrárselo al usuario: qué pasó y cómo arreglarlo."""
