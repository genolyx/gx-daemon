"""
Notifier — backward-compatible wrappers around PlatformClient.

Keeps nipt-daemon-style function signatures (notify_aws_result, attach_analysis_file, etc.)
so copied runner/queue_manager code can call them without changes.
"""

import logging
from typing import Optional

from .platform_client import get_platform_client
from .models import NotificationStatus

logger = logging.getLogger(__name__)


async def notify_aws_result(order_id: str, success: bool, log: str = ""):
    client = get_platform_client()
    result = await client.notify_analysis_result(order_id, "carrier_screening", success, log)
    if result.status == NotificationStatus.FAILED:
        raise Exception(result.message)


async def notify_aws_failed(order_id: str, failed_reason: str):
    client = get_platform_client()
    result = await client.notify_analysis_failed(order_id, "carrier_screening", failed_reason)
    if result.status == NotificationStatus.FAILED:
        raise Exception(result.message)


async def attach_analysis_file(order_id: str, tar_path: Optional[str] = None):
    client = get_platform_client()
    if not tar_path:
        from .platform_client import extract_work_dir
        from .config import settings
        import os
        work_dir = extract_work_dir(order_id)
        tar_path = os.path.join(
            settings.output_base_dir, work_dir, order_id, f"{order_id}.output.tar"
        )
    result = await client.upload_analysis_file(order_id, tar_path)
    if result.status == NotificationStatus.FAILED:
        raise Exception(result.message)


async def upload_pdf_report(order_id: str, pdf_path: str, *, signed: bool = False) -> bool:
    client = get_platform_client()
    result = await client.upload_pdf_report(order_id, pdf_path, signed=signed)
    return result.status == NotificationStatus.SUCCESS
