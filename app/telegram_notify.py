"""
Optional Telegram notifications for order lifecycle (runner hooks).

Uses Bot API sendMessage (outbound HTTPS). No inbound webhook required.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

import httpx

from .config import settings
from .models import Job

logger = logging.getLogger(__name__)


_SERVICE_LABELS = {
    "nipt": "NIPT",
    "whole_exome": "WES",
    "wes_panel": "WES",
    "carrier_screening": "Carrier-Test",
    "carrier": "Carrier-Test",
    "carrier_couples": "Carrier-Test",
    "health_screening": "Health",
    "health_snp": "Health",
    "sgnipt": "sgNIPT",
}

_STATUS_LABELS = {
    "registered": "Registered",
    "started": "In-Analysis",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def _parse_chat_ids(raw: str) -> List[str]:
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


def _one_line(text: str, limit: int = 400) -> str:
    return " ".join((text or "").split())[:limit]


def service_label(service_code: str) -> str:
    code = (service_code or "").strip()
    return _SERVICE_LABELS.get(code, code or "Unknown")


def format_order_message(event: str, job: Job) -> str:
    """Two-line status, plus the failure reason when the run failed."""
    status = _STATUS_LABELS.get(event)
    if not status:
        raise ValueError(event)
    lines = [
        f"[{service_label(job.service_code)}]",
        f"{job.order_id} - {status}",
    ]
    if event == "failed":
        reason = _one_line(job.error_log or job.message or "")
        if reason:
            lines.append(reason)
    return "\n".join(lines)


async def send_order_telegram(event: str, job: Job) -> None:
    """
    event: registered | started | completed | failed | cancelled
    """
    if not settings.telegram_notify_enabled:
        return
    token = (settings.telegram_bot_token or "").strip()
    raw_ids = (settings.telegram_chat_ids or "").strip()
    if not token or not raw_ids:
        return

    try:
        text = format_order_message(event, job)
    except ValueError:
        logger.warning("Unknown telegram event: %s", event)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chat_ids = _parse_chat_ids(raw_ids)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for cid in chat_ids:
                r = await client.post(
                    url,
                    json={
                        "chat_id": cid,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                if r.status_code != 200:
                    logger.warning(
                        "Telegram sendMessage non-200 for chat_id=%s: %s %s",
                        cid,
                        r.status_code,
                        (r.text or "")[:500],
                    )
                else:
                    body = r.json()
                    if not body.get("ok"):
                        logger.warning(
                            "Telegram API ok=false for chat_id=%s: %s",
                            cid,
                            body,
                        )
    except Exception as e:
        logger.warning("Telegram notification failed: %s", e)


def schedule_order_telegram(event: str, job: Job) -> None:
    """Fire-and-forget so Telegram latency/outages never block the runner."""

    async def _run() -> None:
        try:
            await send_order_telegram(event, job)
        except Exception as e:
            logger.warning("Telegram task error: %s", e)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.debug("No running event loop; skipping Telegram schedule")
