from __future__ import annotations

import os
from urllib.request import urlopen


def get_public_ipv4() -> str:
    override = os.environ.get("SERVER_IPV4", "").strip()
    if override:
        return override
    try:
        with urlopen("https://api4.ipify.org", timeout=10) as resp:  # nosec B310
            return resp.read().decode().strip()  # type: ignore[no-any-return]
    except Exception as exc:
        raise RuntimeError(f"Could not determine public IPv4 address: {exc}") from exc


def get_public_ipv6() -> str:
    override = os.environ.get("SERVER_IPV6", "").strip()
    if override:
        return override
    try:
        with urlopen("https://api6.ipify.org", timeout=10) as resp:  # nosec B310
            return resp.read().decode().strip()  # type: ignore[no-any-return]
    except Exception as exc:
        raise RuntimeError(f"Could not determine public IPv6 address: {exc}") from exc
