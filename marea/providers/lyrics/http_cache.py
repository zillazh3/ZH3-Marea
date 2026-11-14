"""Shared throttled HTTP GET client + JSON disk TTL cache for lyrics providers."""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from marea.paths import app_data
from marea.providers.errors import ProviderError


class ThrottledHttpCache:
    """Thread-safe throttled HTTP client with per-item JSON TTL caching on disk."""

    def __init__(
        self,
        cache_subdir: str,
        user_agent: str,
        *,
        http: httpx.Client | None = None,
        cache_dir: Path | None = None,
        min_interval: float = 0.25,
        found_ttl: float = 7 * 86400,
        miss_ttl: float = 86400,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http = http or httpx.Client(timeout=10, headers={"User-Agent": user_agent})
        self.cache_dir = cache_dir if cache_dir is not None else app_data() / "cache" / cache_subdir
        self.min_interval = min_interval
        self.found_ttl = found_ttl
        self.miss_ttl = miss_ttl
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0
        self._blocked_until = 0.0

    def close(self) -> None:
        self.http.close()

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        service_name: str,
        on_response: Callable[[httpx.Response], None] | None = None,
    ) -> httpx.Response:
        with self._lock:
            now = self.clock()
            if now < self._blocked_until:
                wait_s = math.ceil(self._blocked_until - now)
                raise ProviderError(f"{service_name} pidió esperar. Intenta de nuevo en {wait_s} s.")
            self.sleep(max(0.0, self._last + self.min_interval - now))
            try:
                r = self.http.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                raise ProviderError(f"{service_name} no respondió. Revisa tu conexión y vuelve a intentar.") from e
            finally:
                self._last = self.clock()
            if on_response is not None:
                on_response(r)
        return r

    def block_for(self, seconds: float) -> None:
        self._blocked_until = self.clock() + seconds

    def read_cache(self, path: Path) -> tuple[bool, dict | None]:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            ttl = self.found_ttl if cached["data"] else self.miss_ttl
            if time.time() - cached["t"] < ttl:
                return True, cached["data"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return False, None

    @staticmethod
    def write_cache(path: Path, data: dict | None) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"t": time.time(), "data": data}), encoding="utf-8")
        except OSError:
            pass
