"""Re-exported UI screen blocks and settings for backwards compatibility."""
from marea.audio.loopback import list_capture_devices, probe_level, scan_levels
from marea.ui.settings import SettingsBody
from marea.ui.widgets import EmptyState, FadeStack, Page, scrollable

__all__ = [
    "EmptyState",
    "FadeStack",
    "Page",
    "SettingsBody",
    "list_capture_devices",
    "probe_level",
    "scan_levels",
    "scrollable",
]
