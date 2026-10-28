"""Punto de entrada del .exe. freeze_support va antes que cualquier import pesado: el proceso de export (spawn) vuelve a abrir este mismo exe."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--selftest" in sys.argv:
        from marea.selftest import run
        raise SystemExit(run())
    from marea.app import main

    raise SystemExit(main())
