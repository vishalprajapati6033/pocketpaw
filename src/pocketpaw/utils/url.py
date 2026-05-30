from __future__ import annotations

from urllib.parse import urlsplit


def is_valid_url(url: str) -> bool:
    """Validate if a string is a valid HTTP or HTTPS URL.

    Handles leading/trailing whitespaces, checks for supported schemes,
    and ensures a valid hostname/netloc is present.
    """
    if not isinstance(url, str):
        return False

    url = url.strip()
    if not url:
        return False

    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        if not parts.netloc:
            return False
        return True
    except Exception:
        return False
