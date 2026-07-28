"""Admin /queue/summary schema (today + slot groups)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.datetime_kst import KST, now_kst_date_iso
from app.models import Job, OrderStatus
from app.queue_manager import QueueManager, _iso_to_kst_date


def test_iso_to_kst_date_parses_offsets():
    assert _iso_to_kst_date("2026-07-28T14:20:40.930Z") == "2026-07-28"
    assert _iso_to_kst_date("2026-07-28T23:30:00+09:00") == "2026-07-28"
    assert _iso_to_kst_date("") is None


def test_get_summary_includes_admin_fields_and_today_counts():
    qm = QueueManager(max_concurrent=4, store=None)
    today = now_kst_date_iso()
    yesterday = (
        datetime.now(KST).date() - timedelta(days=1)
    ).isoformat()

    running = Job(
        order_id="RUN1",
        service_code="nipt",
        sample_name="RUN1",
        work_dir="2607",
        status=OrderStatus.RUNNING,
        progress=40,
        started_at=f"{today}T10:00:00+09:00",
    )
    qm._running_jobs["RUN1"] = running

    done_today = Job(
        order_id="DONE1",
        service_code="nipt",
        sample_name="DONE1",
        work_dir="2607",
        status=OrderStatus.COMPLETED,
        progress=100,
        started_at=f"{today}T09:00:00+09:00",
        completed_at=f"{today}T11:00:00+09:00",
    )
    qm._completed_jobs["DONE1"] = done_today

    fail_today = Job(
        order_id="FAIL1",
        service_code="nipt",
        sample_name="FAIL1",
        work_dir="2607",
        status=OrderStatus.FAILED,
        progress=5,
        started_at=f"{today}T08:00:00+09:00",
        completed_at=f"{today}T08:05:00+09:00",
    )
    qm._completed_jobs["FAIL1"] = fail_today

    old = Job(
        order_id="OLD1",
        service_code="nipt",
        sample_name="OLD1",
        work_dir="2607",
        status=OrderStatus.COMPLETED,
        progress=100,
        started_at=f"{yesterday}T10:00:00+09:00",
        completed_at=f"{yesterday}T12:00:00+09:00",
    )
    qm._completed_jobs["OLD1"] = old

    queued = Job(
        order_id="Q1",
        service_code="carrier_screening",
        sample_name="Q1",
        work_dir="2607",
        status=OrderStatus.QUEUED,
    )
    qm._jobs["Q1"] = queued

    summary = qm.get_summary()
    assert summary.today == today
    assert summary.totals.running == 1
    assert summary.totals.queued == 1
    assert summary.totals.completed_today == 1
    assert summary.totals.failed_today == 1

    by_code = {s.service_code: s for s in summary.services}
    assert "nipt" in by_code
    assert by_code["nipt"].running == 1
    assert by_code["nipt"].completed_today == 1
    assert by_code["nipt"].failed_today == 1
    assert by_code["nipt"].max_parallel >= 1
    assert by_code["nipt"].slot_group == "nipt"

    assert "carrier_screening" in by_code
    assert by_code["carrier_screening"].queued == 1
    assert by_code["carrier_screening"].slot_group == "exome"

    groups = {g.group: g for g in summary.slot_groups}
    assert "nipt" in groups and "exome" in groups and "sgnipt" in groups
    assert "carrier_screening" in groups["exome"].services

    assert any(r["order_id"] == "RUN1" for r in summary.running_jobs)

    # Legacy fields still populated
    assert summary.total_running == 1
    assert summary.total_queued == 1
    assert summary.total_completed >= 2  # DONE1 + OLD1
