"""Package-level lookup helpers so monkeypatches on marea.ui.lab propagate to submodules."""
import sys


def pkg_attr(name: str, default):
    """Return an attribute from marea.ui.lab when imported, or default otherwise."""
    mod = sys.modules.get("marea.ui.lab")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default
