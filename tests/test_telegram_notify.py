"""Telegram status lines for gx-daemon orders."""

from app.models import Job
from app.telegram_notify import format_order_message


def _job(service_code: str, order_id: str = "GNMF26100001", **kwargs) -> Job:
    return Job(
        order_id=order_id,
        service_code=service_code,
        sample_name=order_id,
        work_dir="2610",
        **kwargs,
    )


def test_service_status_lines():
    assert format_order_message("registered", _job("nipt")) == (
        "[NIPT]\nGNMF26100001 - Registered"
    )
    assert format_order_message("started", _job("whole_exome", "WEGX26100001")) == (
        "[WES]\nWEGX26100001 - In-Analysis"
    )
    assert format_order_message("completed", _job("carrier_screening", "CSGX26100001")) == (
        "[Carrier-Test]\nCSGX26100001 - Completed"
    )


def test_failed_includes_reason():
    text = format_order_message(
        "failed",
        _job("whole_exome", "WEGX26100012", error_log="Pipeline exited with code 1\n"),
    )
    assert text == "[WES]\nWEGX26100012 - Failed\nPipeline exited with code 1"
