"""GX Portal service_code ↔ daemon service_code."""

from typing import Optional

# GX document uses uppercase codes. Daemon plugins use snake_case.
GX_TO_DAEMON = {
    "CARRIER": "carrier_screening",
    "CARRIER_SCREENING": "carrier_screening",
    "WHOLE_EXOME": "whole_exome",
    "WES": "whole_exome",
    "HEALTH": "health_screening",
    "HEALTH_SCREENING": "health_screening",
    "SGNIPT": "sgnipt",
    "SG_NIPT": "sgnipt",
}

DAEMON_TO_GX = {
    "carrier_screening": "CARRIER",
    "whole_exome": "WHOLE_EXOME",
    "health_screening": "HEALTH_SCREENING",
    "sgnipt": "SGNIPT",
}

SUPPORTED_GX_CODES = ("CARRIER", "WHOLE_EXOME", "HEALTH_SCREENING", "SGNIPT")


def resolve_daemon_service(service_code: str) -> Optional[str]:
    raw = (service_code or "").strip()
    if not raw:
        return None
    upper = raw.upper().replace("-", "_")
    if upper in GX_TO_DAEMON:
        return GX_TO_DAEMON[upper]
    lower = raw.lower().replace("-", "_")
    if lower in DAEMON_TO_GX:
        return lower
    return None


def to_gx_code(daemon_code: str) -> str:
    return DAEMON_TO_GX.get(daemon_code, (daemon_code or "").upper())
