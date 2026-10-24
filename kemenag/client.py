"""HTTP client for the bimasislam.kemenag.dev API.

Zero dependencies: built on urllib. Encrypted id tokens contain ``+``, ``/``
and ``=`` so they must be percent-encoded with :func:`urllib.parse.urlencode`
(quote_plus encodes all three correctly).
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "https://bimasislam.kemenag.dev/api/sihat"

DEFAULT_HEADERS = {
    "accept": "application/json",
    "accept-language": "id-ID,id;q=0.9,en;q=0.8",
    "referer": "https://bimasislam.kemenag.dev/jadwal-sholat",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
}

RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class KemenagError(Exception):
    """Raised when a request ultimately fails (network or API-level)."""


class Throttle:
    """Global minimum interval between requests, shared across worker threads."""

    def __init__(self, min_interval: float):
        self._min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            pause = self._next_ok - now
            self._next_ok = max(now, self._next_ok) + self._min_interval
        if pause > 0:
            time.sleep(pause)


class KemenagClient:
    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 4,
        min_interval: float = 0.25,
        base_url: str = BASE_URL,
    ):
        self.timeout = timeout
        self.retries = max(1, retries)
        self.base_url = base_url.rstrip("/")
        self.throttle = Throttle(min_interval)

    # -- low level ---------------------------------------------------------

    def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(params)}"
        last_err: Exception | None = None
        for attempt in range(self.retries):
            if attempt:
                backoff = min(30.0, 1.5 ** attempt) + random.uniform(0, 0.5)
                time.sleep(backoff)
            self.throttle.wait()
            try:
                req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code not in RETRYABLE_HTTP_CODES:
                    raise KemenagError(f"HTTP {e.code} for {url}") from e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
                last_err = e
        raise KemenagError(f"giving up after {self.retries} attempts: {url} ({last_err})")

    @staticmethod
    def _entries(payload: dict[str, Any]) -> list[dict[str, str]]:
        data = payload.get("data")
        if not isinstance(data, list):
            raise KemenagError(f"unexpected response shape: {str(payload)[:200]}")
        return data

    # -- endpoints ---------------------------------------------------------

    def get_provinsi(self) -> list[dict[str, str]]:
        """List provinces: ``[{"id": <token>, "name": "ACEH"}, ...]``."""
        return self._entries(self._get_json("provinsi", {}))

    def get_kabko(self, prov_id: str) -> list[dict[str, str]]:
        """List regencies/cities in a province, same entry shape as provinsi."""
        return self._entries(self._get_json("kabko", {"prov": prov_id}))

    def get_jadwal(
        self, prov_id: str, kabko_id: str, year: int, month: int
    ) -> dict[str, Any]:
        """One month of prayer times for one regency/city.

        Returns the full API payload (prov, kabko, lintang, bujur, data keyed
        by ``YYYY-MM-DD``). Raises KemenagError when ``status != 1``.
        """
        payload = self._get_json(
            "jadwal",
            {
                "prov": prov_id,
                "kabko": kabko_id,
                "thn": str(year),
                "bln": f"{month:02d}",
            },
        )
        if payload.get("status") != 1:
            raise KemenagError(
                f"API refused jadwal (status={payload.get('status')!r}, "
                f"message={payload.get('message')!r})"
            )
        if not isinstance(payload.get("data"), dict):
            raise KemenagError(f"jadwal response has no data object: {str(payload)[:200]}")
        return payload
