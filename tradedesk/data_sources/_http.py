"""Minimal stdlib HTTP helper for data-source downloads.

The macro feeds (FRED, ECB) are small CSV downloads over HTTPS with no
authentication, so a thin :mod:`urllib.request` wrapper is sufficient — we
deliberately avoid pulling extra runtime dependencies.  A descriptive
``User-Agent`` is sent because some public statistics portals (notably the
ECB Data Portal) reject the default ``Python-urllib`` agent.
"""

from __future__ import annotations

import urllib.request

USER_AGENT = "radiusred-tradedesk/1.0 (+https://github.com/radiusred/tradedesk)"
"""User-Agent sent on all data-source downloads.

Public statistics portals fronted by a CDN (the ECB Data Portal and
cftc.gov on Cloudflare) reject the default ``Python-urllib`` agent with a
403, so every downloader must present a descriptive UA.
"""


def get_text(url: str, *, timeout: float = 60.0) -> str:
    """GET ``url`` and return the decoded response body as text.

    Raises :class:`urllib.error.HTTPError` / :class:`urllib.error.URLError`
    on transport failures so callers can decide whether to skip or abort.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        charset = resp.headers.get_content_charset() or "utf-8"
        raw: bytes = resp.read()
        return raw.decode(charset, errors="replace")
