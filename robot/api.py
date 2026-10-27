"""Minimal T-Invest REST client (stdlib only).

invest-public-api.tbank.ru is signed by the Russian Trusted Root CA, which is not
in the default trust store, so the bundled certs/russian_trusted_ca.pem is loaded
on top of the system CAs. HTTPS_PROXY is honored by urllib automatically.
"""
from __future__ import annotations

import http.client
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

BASE_URL = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
ROOT = Path(__file__).resolve().parent.parent
RU_CA = ROOT / "certs" / "russian_trusted_ca.pem"


class ApiError(RuntimeError):
    def __init__(self, method: str, status: int, body: str):
        super().__init__(f"{method}: HTTP {status}: {body[:500]}")
        self.method, self.status, self.body = method, status, body


def q2f(q: dict | None) -> float:
    """Quotation/MoneyValue {units, nano} -> float."""
    if not q:
        return 0.0
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9


def f2q(x: float) -> dict:
    """float -> Quotation, exact to 9 decimals."""
    d = Decimal(str(x)).quantize(Decimal("0.000000001"))
    units = int(d)
    nano = int((d - units) * 1_000_000_000)
    return {"units": str(units), "nano": nano}


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    extra = os.environ.get("SSL_CERT_FILE")
    if extra and Path(extra).is_file():
        ctx.load_verify_locations(cafile=extra)
    if RU_CA.is_file():
        ctx.load_verify_locations(cafile=str(RU_CA))
    return ctx


class Client:
    def __init__(self, token: str | None = None, timeout: float = 30.0, retries: int = 3):
        self.token = token or os.environ.get("TINVEST_TOKEN")
        if not self.token:
            raise RuntimeError("TINVEST_TOKEN is not set")
        self.timeout = timeout
        self.retries = retries
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )

    def call(self, method: str, body: dict | None = None) -> dict:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            BASE_URL + method,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:
                body_text = e.read().decode(errors="replace")
                # 429 = rate limit, 5xx = transient; everything else is a real error
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    reset = e.headers.get("x-ratelimit-reset")
                    time.sleep(float(reset) if reset and reset.isdigit() else delay)
                    delay *= 2
                    continue
                raise ApiError(method, e.code, body_text) from None
            # IncompleteRead / reset: the connection dropped mid-response (seen on the ~5 MB
            # futures list), a transient network error like a timeout
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError):
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise AssertionError("unreachable")
