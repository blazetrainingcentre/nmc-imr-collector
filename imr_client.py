"""Minimal client for the register page's own JSON endpoints (standard library only)."""
from __future__ import annotations

import json
import os
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import config

CERT_HELP = (
    "The TLS certificate check failed ({reason}). Certificate checking has NOT been switched off. "
    "On Windows, run `pip install truststore` and try again; if it still fails, "
    "see README > Troubleshooting."
)


class Blocked(RuntimeError):
    """HTTP 403/429, or an HTML/challenge page where JSON was expected. Stop; do not work around it."""


class ApiError(RuntimeError):
    """The request failed after retries, or the server rejected it."""


def _ssl_context() -> ssl.SSLContext:
    """A TLS context with certificate and hostname verification ALWAYS on.

    * IMR_CA_BUNDLE=<pem file>: trust that file in addition to the default certificates
      (for example a missing intermediate certificate exported from your browser).
    * Otherwise, if the optional `truststore` package is installed, use the operating
      system's trust store. That is what a browser does, including fetching missing
      intermediate certificates, so it fixes most "unable to get local issuer certificate" errors.
    * Otherwise, Python's default certificates.
    """
    bundle = os.environ.get("IMR_CA_BUNDLE", "").strip()
    if bundle:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=bundle)
        return context
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # not installed, or unsupported on this Python
        return ssl.create_default_context()


class Client:
    def __init__(self, base: str | None = None, delay: float | None = None,
                 timeout: float | None = None, retries: int | None = None):
        self.base = (base or config.BASE).rstrip("/")
        self.delay = config.DELAY_SECONDS if delay is None else delay
        self.timeout = config.TIMEOUT_SECONDS if timeout is None else timeout
        self.retries = config.MAX_RETRIES if retries is None else retries
        self.context = _ssl_context()
        self._first = True

    def _pause(self) -> None:
        if self._first:
            self._first = False
            return
        if self.delay > 0:
            time.sleep(random.uniform(self.delay, self.delay * 1.8))

    def get_json(self, path: str, params: dict | None = None):
        url = f"{self.base}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",  # what the page's own jQuery sends
            "Referer": config.PAGE_URL,
        })
        last_error = None
        for attempt in range(1, self.retries + 1):
            self._pause()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                    content_type = response.headers.get("Content-Type", "")
                    body = response.read()
            except urllib.error.HTTPError as exc:
                exc.close()  # release the response so Python does not warn about an unclosed file
                if exc.code in (403, 429):
                    raise Blocked(f"HTTP {exc.code} from /{path}") from exc
                if exc.code in (400, 404, 422):
                    raise ApiError(f"HTTP {exc.code} from /{path}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", None)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    # Deterministic: retrying cannot help, so fail at once with guidance.
                    raise ApiError(CERT_HELP.format(reason=reason)) from exc
                last_error = exc
            else:
                if "json" not in content_type.lower():
                    raise Blocked(f"/{path} returned {content_type or 'unknown content'} instead of JSON "
                                  "(a bot challenge or error page?)")
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise Blocked(f"/{path} returned unreadable JSON") from exc
            if self.delay > 0 and attempt < self.retries:
                time.sleep(min(30 * attempt, 60))
        raise ApiError(f"Giving up on /{path} after {self.retries} attempts: {last_error}")

    # --- the three endpoints the page uses ---
    def states(self):
        data = self.get_json("states")
        return [(s["code"], s["name"]) for s in data.get("states", [])]

    def years(self):
        data = self.get_json("years")
        return [str(y) for y in data.get("years", [])]

    def search(self, state: str, year: str, page: int = 1, per_page: int | None = None,
               name: str = "", reg_no: str = ""):
        return self.get_json("search", {
            "search_type": "advance", "name": name, "reg_no": reg_no,
            "year": year, "state": state, "page": page,
            "per_page": per_page or config.PER_PAGE,
        })
