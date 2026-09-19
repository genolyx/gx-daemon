"""Call GX Portal callback URLs (report PDF + failed status)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


def _auth_headers() -> dict:
    key = (getattr(settings, "gx_callback_api_key", None) or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def _safe_callback(url: str) -> str:
    return (url or "").split("?")[0]


async def notify_failed(status_url: Optional[str], order_id: str, message: str) -> None:
    if not status_url:
        logger.warning("GX status_url missing; skip failed notify for %s", order_id)
        return
    payload = {"order_id": order_id, "status": "failed", "message": message}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(status_url, json=payload, headers=_auth_headers())
        logger.info(
            "GX status callback %s → HTTP %s",
            _safe_callback(status_url),
            res.status_code,
        )
    except Exception as exc:
        logger.warning("GX status callback failed for %s: %s", order_id, exc)


async def send_report_pdf(
    report_url: str,
    pdf_path: str,
    order_id: str,
) -> dict:
    if not report_url:
        raise RuntimeError("callback.report_url is not set")
    if not os.path.isfile(pdf_path):
        raise RuntimeError(f"PDF not found: {os.path.basename(pdf_path)}")
    completed_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    filename = os.path.basename(pdf_path)
    headers = _auth_headers()
    async with httpx.AsyncClient(timeout=120.0) as client:
        with open(pdf_path, "rb") as fh:
            res = await client.post(
                report_url,
                headers=headers,
                files={"file": (filename, fh, "application/pdf")},
                data={
                    "external_order_id": order_id,
                    "completed_at": completed_at,
                },
            )
    if res.status_code >= 400:
        text = (res.text or "")[:300]
        raise RuntimeError(f"GX report send HTTP {res.status_code}: {text}")
    try:
        body = res.json()
    except Exception:
        body = {"order_id": order_id, "status": "REPORT_READY"}
    logger.info("GX report sent for %s → %s", order_id, _safe_callback(report_url))
    return body if isinstance(body, dict) else {"order_id": order_id, "status": "REPORT_READY"}
