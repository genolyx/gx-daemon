"""
Queue Manager

작업 큐와 동시 실행 제어를 관리합니다.
기존 nipt-daemon의 queue_manager.py를 일반화하여 멀티서비스를 지원합니다.
"""

import asyncio
import itertools
import logging
from typing import Dict, List, Optional, Set
from collections import defaultdict

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings
from .datetime_kst import now_kst_iso, now_kst_date_iso, KST
from .models import (
    Job,
    OrderStatus,
    QueueSummary,
    QueueSummaryTotals,
    QueueSummaryServiceRow,
    QueueSummarySlotGroup,
)

# Service → resource group mapping.
# "exome" group is the heavy one (carrier_screening / whole_exome / health_screening).
_SERVICE_GROUP: dict[str, str] = {
    "nipt": "nipt",
    "carrier_screening": "exome",
    "whole_exome": "exome",
    "health_screening": "exome",
    "sgnipt": "sgnipt",
}

_SERVICE_DISPLAY: dict[str, str] = {
    "nipt": "NIPT",
    "sgnipt": "sgNIPT",
    "carrier_screening": "Carrier Screening",
    "whole_exome": "Whole Exome",
    "health_screening": "Health Screening",
}

_GROUP_SERVICES: dict[str, list[str]] = {
    "nipt": ["nipt"],
    "sgnipt": ["sgnipt"],
    "exome": ["carrier_screening", "whole_exome", "health_screening"],
}


def _iso_to_kst_date(value: Optional[str]) -> Optional[str]:
    """Parse job timestamp → YYYY-MM-DD in KST (None if unparseable)."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(KST).date().isoformat()

# Dequeue priority when NIPT_PRIORITY=true (lower = dequeued first).
# NIPT runs first; sgnipt before heavy exome.
_SERVICE_DEQUEUE_PRIORITY: dict[str, int] = {
    "nipt": 0,
    "sgnipt": 1,
    "carrier_screening": 2,
    "whole_exome": 2,
    "health_screening": 2,
}
from .order_store import (
    OrderStore,
    ACTIVE_BEFORE_RESTART,
    ingest_result_json_from_disk,
)
from .order_cleanup import delete_run_artifacts
from .services.carrier_screening.layout_norm import (
    _CARRIER_LIKE,
    apply_carrier_layout_directories,
)
from .services.sgnipt import apply_sgnipt_layout_directories
from .services.nipt import apply_nipt_layout_directories
from .services import get_plugin

logger = logging.getLogger(__name__)


class QueueManager:
    """
    멀티서비스 작업 큐 관리자.

    단일 큐에서 모든 서비스의 작업을 관리하며,
    Semaphore를 통해 동시 실행 수를 제어합니다.
    """

    def __init__(self, max_concurrent: int = None, store: Optional[OrderStore] = None):
        # Per-group semaphores replace the legacy single semaphore.
        # max_concurrent is kept for backward-compat (test injection); ignored when using groups.
        self._nipt_priority: bool = settings.nipt_priority
        nipt_limit = (
            settings.max_concurrent_nipt_priority
            if self._nipt_priority
            else settings.max_concurrent_nipt
        )
        self._group_limits: Dict[str, int] = {
            "nipt":   nipt_limit,
            "exome":  settings.max_concurrent_exome,
            "sgnipt": settings.max_concurrent_sgnipt,
        }
        # Total worker count = sum of all group limits so every slot can be filled.
        self._max_concurrent = max_concurrent or sum(self._group_limits.values())

        # Priority mode: use PriorityQueue so NIPT jobs are dequeued before exome.
        # Normal mode: plain FIFO Queue.
        self._seq = itertools.count()  # tie-breaker to preserve arrival order within same priority
        # Always use PriorityQueue internally. Items are always (priority, seq, job) tuples.
        # When nipt_priority=false all services get priority=1 → pure FIFO by seq.
        # When nipt_priority=true use _SERVICE_DEQUEUE_PRIORITY (nipt=0, sgnipt=1, exome=2).
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

        self._group_semaphores: Dict[str, asyncio.Semaphore] = {
            group: asyncio.Semaphore(limit)
            for group, limit in self._group_limits.items()
        }
        # Fallback semaphore for unknown service codes (uses global limit).
        self._fallback_semaphore = asyncio.Semaphore(self._max_concurrent)
        self._store = store

        # 작업 상태 추적
        self._saved_jobs: Dict[str, Job] = {}  # order_id -> Job (SAVED, 파이프라인 미시작)
        self._jobs: Dict[str, Job] = {}  # order_id -> Job (QUEUED, 큐 대기)
        self._running_jobs: Dict[str, Job] = {}  # order_id -> Job (실행 중)
        self._completed_jobs: Dict[str, Job] = {}  # order_id -> Job (완료/실패/취소)
        self._cancel_requested: Set[str] = set()  # QUEUED 작업 dequeue 시 버림

        # 서비스별 통계
        self._stats: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"queued": 0, "running": 0, "completed": 0, "failed": 0}
        )

        self._lock = asyncio.Lock()
        if self._store:
            self._restore_from_store()
        logger.info(
            "QueueManager initialized (workers=%d, limits=%s, nipt_priority=%s, store=%s)",
            self._max_concurrent,
            self._group_limits,
            self._nipt_priority,
            "on" if self._store else "off",
        )

    @property
    def store(self) -> Optional[OrderStore]:
        return self._store

    def _restore_from_store(self) -> None:
        assert self._store is not None
        jobs = self._store.fetch_all_jobs()
        if not jobs:
            logger.info("No orders to restore from SQLite")
            return
        queued_list: List[Job] = []
        interrupted = 0
        for job in jobs:
            st = job.status
            if (
                job.service_code in _CARRIER_LIKE
                and st not in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY)
                and apply_carrier_layout_directories(job)
            ):
                self._store.upsert_job(job)
            if (
                job.service_code == "sgnipt"
                and st not in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY)
                and apply_sgnipt_layout_directories(job)
            ):
                self._store.upsert_job(job)
            if (
                job.service_code == "nipt"
                and st not in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY)
                and apply_nipt_layout_directories(job)
            ):
                self._store.upsert_job(job)
            if st in ACTIVE_BEFORE_RESTART:
                # daemon 재시작 전에 파이프라인이 실제로 완료됐는지 동기 확인
                plugin = get_plugin(job.service_code)
                recovered = plugin is not None and plugin.sync_is_complete(job)
                if recovered:
                    job.status = OrderStatus.REPORT_READY
                    job.progress = 100
                    job.message = "Recovered after daemon restart (pipeline completed on disk)"
                    job.completed_at = now_kst_iso()
                    self._completed_jobs[job.order_id] = job
                    self._stats[job.service_code]["completed"] += 1
                    try:
                        self._store.upsert_job(job)
                    except Exception as _e:
                        logger.warning("Could not persist recovered job %s: %s", job.order_id, _e)
                    logger.info(
                        "Recovered order %s as REPORT_READY after restart (was %s)",
                        job.order_id,
                        st.value,
                    )
                else:
                    old_log = (job.error_log or "").strip()
                    extra = "Daemon restarted while job was active."
                    job.error_log = f"{old_log}\n{extra}" if old_log else extra
                    job.message = "Interrupted: daemon restarted"
                    job.status = OrderStatus.FAILED
                    job.completed_at = now_kst_iso()
                    self._completed_jobs[job.order_id] = job
                    self._stats[job.service_code]["failed"] += 1
                    try:
                        self._store.upsert_job(job)
                    except Exception as _e:
                        logger.warning("Could not persist failed job %s: %s", job.order_id, _e)
                    interrupted += 1
                    logger.warning(
                        "Marked order %s FAILED after restart (was %s)",
                        job.order_id,
                        st.value,
                    )
            elif st == OrderStatus.SAVED:
                self._saved_jobs[job.order_id] = job
            elif st == OrderStatus.QUEUED:
                self._jobs[job.order_id] = job
                queued_list.append(job)
                self._stats[job.service_code]["queued"] += 1
            elif st in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY):
                self._completed_jobs[job.order_id] = job
                self._stats[job.service_code]["completed"] += 1
            elif st == OrderStatus.FAILED:
                # FAILED 잡도 실제로 완료됐으면 REPORT_READY 로 복구
                plugin_f = get_plugin(job.service_code)
                if plugin_f is not None and plugin_f.sync_is_complete(job):
                    job.status = OrderStatus.REPORT_READY
                    job.progress = 100
                    job.message = "Recovered: pipeline completed on disk"
                    job.error_log = ""
                    try:
                        self._store.upsert_job(job)
                    except Exception as _e:
                        logger.warning("Could not persist recovered FAILED job %s: %s", job.order_id, _e)
                    self._completed_jobs[job.order_id] = job
                    self._stats[job.service_code]["completed"] += 1
                    logger.info(
                        "Recovered FAILED order %s as REPORT_READY (pipeline completed on disk)",
                        job.order_id,
                    )
                else:
                    self._completed_jobs[job.order_id] = job
                    self._stats[job.service_code]["failed"] += 1
            elif st == OrderStatus.CANCELLED:
                self._completed_jobs[job.order_id] = job
            else:
                self._completed_jobs[job.order_id] = job
                logger.warning(
                    "Restored order %s with status %s into completed bucket",
                    job.order_id,
                    st.value,
                )

        queued_list.sort(key=lambda j: j.created_at or "")
        for j in queued_list:
            self._queue.put_nowait(j)

        logger.info(
            "Restored %d order(s) from SQLite (queued=%d, interrupted→failed=%d)",
            len(jobs),
            len(queued_list),
            interrupted,
        )

    async def persist_job(self, job: Job) -> None:
        if not self._store:
            return
        try:
            await asyncio.to_thread(self._store.upsert_job, job)
        except Exception as e:
            logger.error("Failed to persist order %s: %s", job.order_id, e)

    async def _ingest_result_snapshot(self, job: Job) -> None:
        if not self._store:
            return
        try:
            await asyncio.to_thread(ingest_result_json_from_disk, self._store, job)
        except Exception as e:
            logger.warning("Ingest result.json for %s: %s", job.order_id, e)

    async def enqueue(self, job: Job) -> int:
        """
        작업을 큐에 추가합니다.

        이미 QUEUED 또는 RUNNING 상태인 동일 order_id는 거부합니다.
        --fresh 실행 도중 같은 작업이 삭제 후 재투입되면 실행 중인 work 디렉토리가
        날아가는 것을 방지합니다.

        Args:
            job: 작업 정보

        Returns:
            현재 큐 위치 (1-based)

        Raises:
            ValueError: 동일 order_id가 이미 활성 상태일 때
        """
        if job.service_code in _CARRIER_LIKE:
            apply_carrier_layout_directories(job)
        elif job.service_code == "sgnipt":
            apply_sgnipt_layout_directories(job)
        elif job.service_code == "nipt":
            apply_nipt_layout_directories(job)
        async with self._lock:
            if job.order_id in self._running_jobs:
                raise ValueError(
                    f"Order {job.order_id!r} is already RUNNING — "
                    "stop it first before re-submitting"
                )
            if job.order_id in self._jobs:
                raise ValueError(
                    f"Order {job.order_id!r} is already QUEUED — "
                    "cancel it first before re-submitting"
                )
            job.status = OrderStatus.QUEUED
            job.updated_at = now_kst_iso()
            self._jobs[job.order_id] = job
            self._stats[job.service_code]["queued"] += 1

        # priority=0 is highest. When NIPT_PRIORITY=false all services get priority=1 (FIFO).
        priority = (
            _SERVICE_DEQUEUE_PRIORITY.get(job.service_code, 1)
            if self._nipt_priority
            else 1
        )
        await self._queue.put((priority, next(self._seq), job))
        queue_size = self._queue.qsize()

        logger.info(
            f"[{job.service_code}] Enqueued job {job.order_id} "
            f"(sample: {job.sample_name}, queue_size: {queue_size})"
        )
        await self.persist_job(job)
        return queue_size

    async def dequeue(self) -> Job:
        """큐에서 다음 작업을 가져옵니다 (blocking). 취소 요청된 작업은 건너뜁니다.

        Priority mode (NIPT_PRIORITY=true) 시 PriorityQueue에서 (priority, seq, job) 튜플로
        반환되므로 job을 언패킹합니다.
        """
        while True:
            raw = await self._queue.get()
            _priority, _seq, job = raw  # always (priority, seq, job) tuple

            async with self._lock:
                if job.order_id in self._cancel_requested:
                    self._cancel_requested.discard(job.order_id)
                    self._stats[job.service_code]["queued"] = max(
                        0, self._stats[job.service_code]["queued"] - 1
                    )
                    self._jobs.pop(job.order_id, None)
                    job.status = OrderStatus.CANCELLED
                    job.completed_at = now_kst_iso()
                    job.updated_at = now_kst_iso()
                    job.message = "Cancelled while queued"
                    self._completed_jobs[job.order_id] = job
                    cancelled = job
                else:
                    self._stats[job.service_code]["queued"] = max(
                        0, self._stats[job.service_code]["queued"] - 1
                    )
                    cancelled = None

            if cancelled is not None:
                await self.persist_job(cancelled)
                continue

            await self.persist_job(job)
            return job

    def _semaphore_for(self, service_code: str) -> asyncio.Semaphore:
        group = _SERVICE_GROUP.get(service_code, "")
        return self._group_semaphores.get(group, self._fallback_semaphore)

    async def acquire_slot(self, service_code: str = ""):
        """서비스 그룹별 실행 슬롯 획득 (동시 실행 수 제한)"""
        sem = self._semaphore_for(service_code)
        await sem.acquire()
        group = _SERVICE_GROUP.get(service_code, "unknown")
        limit = self._group_limits.get(group, self._max_concurrent)
        logger.debug(
            "Acquired slot [%s/%s] (available: %d/%d)",
            service_code, group, sem._value, limit,
        )

    def release_slot(self, service_code: str = ""):
        """서비스 그룹별 실행 슬롯 반환"""
        sem = self._semaphore_for(service_code)
        sem.release()
        group = _SERVICE_GROUP.get(service_code, "unknown")
        limit = self._group_limits.get(group, self._max_concurrent)
        logger.debug(
            "Released slot [%s/%s] (available: %d/%d)",
            service_code, group, sem._value, limit,
        )

    async def mark_running(self, job: Job):
        """작업을 실행 중으로 표시"""
        async with self._lock:
            job.status = OrderStatus.RUNNING
            job.started_at = now_kst_iso()
            job.updated_at = now_kst_iso()
            self._running_jobs[job.order_id] = job
            self._stats[job.service_code]["running"] += 1

        logger.info(f"[{job.service_code}] Job {job.order_id} is now RUNNING")
        await self.persist_job(job)

    async def finalize_reprocess_results(self, job: Job) -> None:
        """
        process_results 재실행 성공 후: result.json 스냅샷 저장, FAILED → COMPLETED 복구.
        COMPLETED / REPORT_READY 는 상태 유지, 메시지·updated_at 만 갱신.
        """
        async with self._lock:
            if job.status == OrderStatus.FAILED:
                self._stats[job.service_code]["failed"] = max(
                    0, self._stats[job.service_code]["failed"] - 1
                )
                self._stats[job.service_code]["completed"] += 1
                job.status = OrderStatus.COMPLETED
                job.completed_at = now_kst_iso()
                job.error_log = None
            job.progress = 100
            job.updated_at = now_kst_iso()
            job.message = "Reprocessed (annotation/QC/result.json)"

        await self._ingest_result_snapshot(job)
        await self.persist_job(job)

    async def mark_completed(self, job: Job):
        """작업을 완료로 표시"""
        async with self._lock:
            job.status = OrderStatus.COMPLETED
            job.completed_at = now_kst_iso()
            job.updated_at = now_kst_iso()
            self._running_jobs.pop(job.order_id, None)
            self._jobs.pop(job.order_id, None)
            self._completed_jobs[job.order_id] = job
            self._stats[job.service_code]["running"] = max(
                0, self._stats[job.service_code]["running"] - 1
            )
            self._stats[job.service_code]["completed"] += 1

        logger.info(f"[{job.service_code}] Job {job.order_id} COMPLETED")
        if job.params and "_pipeline_fresh" in job.params:
            job.params = dict(job.params)
            job.params.pop("_pipeline_fresh", None)
        await self._ingest_result_snapshot(job)
        await self.persist_job(job)

    async def mark_report_ready(self, order_id: str, message: str = "Report ready for download") -> None:
        """리뷰 후 PDF/HTML 생성 완료 — Portal에서 다운로드 가능."""
        async with self._lock:
            job = (
                self._running_jobs.get(order_id)
                or self._jobs.get(order_id)
                or self._saved_jobs.get(order_id)
                or self._completed_jobs.get(order_id)
            )
            if not job:
                logger.warning("mark_report_ready: order %s not found", order_id)
                return
            job.status = OrderStatus.REPORT_READY
            job.message = message
            job.progress = 100
            job.updated_at = now_kst_iso()
        await self.persist_job(job)
        logger.info("[%s] Order %s → REPORT_READY", job.service_code, order_id)

    async def mark_failed(self, job: Job, error: str = ""):
        """작업을 실패로 표시"""
        async with self._lock:
            job.status = OrderStatus.FAILED
            job.completed_at = now_kst_iso()
            job.updated_at = now_kst_iso()
            job.error_log = error
            if (error or "").strip():
                job.message = error.strip()
            self._running_jobs.pop(job.order_id, None)
            self._jobs.pop(job.order_id, None)
            self._completed_jobs[job.order_id] = job
            self._stats[job.service_code]["running"] = max(
                0, self._stats[job.service_code]["running"] - 1
            )
            self._stats[job.service_code]["failed"] += 1

        logger.error(f"[{job.service_code}] Job {job.order_id} FAILED: {error}")
        if job.params and "_pipeline_fresh" in job.params:
            job.params = dict(job.params)
            job.params.pop("_pipeline_fresh", None)
        await self.persist_job(job)

    def get_job(self, order_id: str) -> Optional[Job]:
        """order_id로 작업 조회"""
        return (
            self._running_jobs.get(order_id)
            or self._jobs.get(order_id)
            or self._saved_jobs.get(order_id)
            or self._completed_jobs.get(order_id)
        )

    async def save_job(self, job: Job) -> None:
        """Portal 저장만: SAVED 상태로 보관, 큐에 넣지 않음."""
        async with self._lock:
            job.status = OrderStatus.SAVED
            job.updated_at = now_kst_iso()
            self._saved_jobs[job.order_id] = job
        if job.service_code in _CARRIER_LIKE:
            apply_carrier_layout_directories(job)
        elif job.service_code == "sgnipt":
            apply_sgnipt_layout_directories(job)
        elif job.service_code == "nipt":
            apply_nipt_layout_directories(job)
        logger.info(f"[{job.service_code}] Saved order {job.order_id} (not queued)")
        await self.persist_job(job)

    async def replace_edited_job(
        self,
        new_job: Job,
        *,
        previous_order_id: Optional[str] = None,
    ) -> None:
        """
        Portal에서 저장·종료·완료 주문 내용을 교체할 때 사용.
        (SAVED 또는 _completed_jobs 의 FAILED / CANCELLED / COMPLETED / REPORT_READY)
        previous_order_id: PATCH URL 등에서 온 기존 키(order_id 변경 시 필수).
        """
        old_id = previous_order_id if previous_order_id is not None else new_job.order_id
        new_id = new_job.order_id

        async with self._lock:
            if old_id not in self._saved_jobs and old_id not in self._completed_jobs:
                raise KeyError(old_id)

            if new_id != old_id:
                for d in (
                    self._saved_jobs,
                    self._jobs,
                    self._running_jobs,
                    self._completed_jobs,
                ):
                    if new_id in d:
                        raise ValueError(f"Order ID already in use: {new_id}")

            if self._store and old_id != new_id:
                try:
                    await asyncio.to_thread(self._store.rename_order_id, old_id, new_id)
                except ValueError:
                    raise
                except Exception as e:
                    logger.error("rename_order_id %s -> %s: %s", old_id, new_id, e)
                    raise ValueError(f"Could not rename order in database: {e}") from e

            if old_id in self._saved_jobs:
                if self._saved_jobs[old_id].status != OrderStatus.SAVED:
                    raise ValueError(f"Order {old_id} is not SAVED")
                self._saved_jobs.pop(old_id)
                self._saved_jobs[new_id] = new_job
            else:
                st = self._completed_jobs[old_id].status
                if st not in (
                    OrderStatus.FAILED,
                    OrderStatus.CANCELLED,
                    OrderStatus.COMPLETED,
                    OrderStatus.REPORT_READY,
                ):
                    raise ValueError(
                        f"Order {old_id} cannot be edited in status {st.value}"
                    )
                self._completed_jobs.pop(old_id)
                self._completed_jobs[new_id] = new_job

        if new_job.service_code in _CARRIER_LIKE:
            apply_carrier_layout_directories(new_job)
        elif new_job.service_code == "sgnipt":
            apply_sgnipt_layout_directories(new_job)
        elif new_job.service_code == "nipt":
            apply_nipt_layout_directories(new_job)
        logger.info(
            "[%s] Updated order %s -> %s (status=%s)",
            new_job.service_code,
            old_id,
            new_id,
            new_job.status.value,
        )
        await self.persist_job(new_job)

    def _prepare_job_for_retry(self, job: Job) -> None:
        """FAILED/CANCELLED/COMPLETED 주문을 다시 큐에 넣기 전 필드·통계 정리."""
        if job.status == OrderStatus.FAILED:
            self._stats[job.service_code]["failed"] = max(
                0, self._stats[job.service_code]["failed"] - 1
            )
        elif job.status in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY):
            self._stats[job.service_code]["completed"] = max(
                0, self._stats[job.service_code]["completed"] - 1
            )
        job.progress = 0
        job.message = ""
        job.error_log = None
        job.started_at = None
        job.completed_at = None
        job.updated_at = now_kst_iso()
        job.pid = None
        job.exit_code = None
        job.duration = None

    async def start_saved_job(
        self,
        order_id: str,
        *,
        fresh: bool = False,
        use_ssd: bool = False,
        scratch_dir: str | None = None,
    ) -> tuple:
        """
        SAVED 주문, 또는 FAILED/CANCELLED/COMPLETED 주문(재분석)을 큐에 넣습니다.
        fresh=True 이면 job.params['_pipeline_fresh']=True 를 설정해 플러그인이 캐시 삭제를 수행하게 함.
        (Job, queue_position) 반환.
        """
        async with self._lock:
            job = self._saved_jobs.pop(order_id, None)
            if job:
                if job.status != OrderStatus.SAVED:
                    self._saved_jobs[order_id] = job
                    raise ValueError(f"Order {order_id} is not SAVED")
            else:
                cj = self._completed_jobs.get(order_id)
                if cj is None or cj.status not in (
                    OrderStatus.FAILED,
                    OrderStatus.CANCELLED,
                    OrderStatus.COMPLETED,
                    OrderStatus.REPORT_READY,
                ):
                    raise KeyError(order_id)
                job = self._completed_jobs.pop(order_id)
                self._prepare_job_for_retry(job)

        job.params = dict(job.params or {})
        if fresh:
            job.params["_pipeline_fresh"] = True
        else:
            job.params.pop("_pipeline_fresh", None)

        if use_ssd:
            job.params["_use_ssd"] = True
            if scratch_dir:
                job.params["_scratch_dir"] = scratch_dir
        else:
            job.params.pop("_use_ssd", None)
            job.params.pop("_scratch_dir", None)

        queue_position = await self.enqueue(job)
        return job, queue_position

    async def delete_saved(self, order_id: str) -> bool:
        """SAVED 주문만 삭제."""
        async with self._lock:
            if order_id not in self._saved_jobs:
                return False
            del self._saved_jobs[order_id]
        if self._store:
            try:
                await asyncio.to_thread(self._store.delete_order, order_id)
            except Exception as e:
                logger.error("Failed to delete order %s from store: %s", order_id, e)
        logger.info(f"Deleted saved order {order_id}")
        return True

    async def forget_order(self, order_id: str) -> Optional[Job]:
        """
        order_id를 저장/큐/실행/완료 버킷과 asyncio.Queue에서 제거합니다 (SQLite 제외).
        통계 카운터를 Job 상태에 맞게 감소시킵니다.
        """
        async with self._lock:
            job = (
                self._saved_jobs.pop(order_id, None)
                or self._jobs.pop(order_id, None)
                or self._running_jobs.pop(order_id, None)
                or self._completed_jobs.pop(order_id, None)
            )
            self._saved_jobs.pop(order_id, None)
            self._jobs.pop(order_id, None)
            self._running_jobs.pop(order_id, None)
            self._completed_jobs.pop(order_id, None)
            self._cancel_requested.discard(order_id)
            pending: List[Job] = []
            while True:
                try:
                    j = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if j.order_id != order_id:
                    pending.append(j)
            for j in pending:
                self._queue.put_nowait(j)
            if job:
                svc = job.service_code
                st = job.status
                if st == OrderStatus.QUEUED:
                    self._stats[svc]["queued"] = max(
                        0, self._stats[svc]["queued"] - 1
                    )
                elif st in (
                    OrderStatus.RUNNING,
                    OrderStatus.DOWNLOADING,
                    OrderStatus.PROCESSING,
                    OrderStatus.UPLOADING,
                    OrderStatus.RECEIVED,
                ):
                    self._stats[svc]["running"] = max(
                        0, self._stats[svc]["running"] - 1
                    )
                elif st in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY):
                    self._stats[svc]["completed"] = max(
                        0, self._stats[svc]["completed"] - 1
                    )
                elif st == OrderStatus.FAILED:
                    self._stats[svc]["failed"] = max(
                        0, self._stats[svc]["failed"] - 1
                    )
        return job

    async def purge_queued_order(self, order_id: str) -> Optional[Job]:
        """
        QUEUED 주문을 메모리 큐와 asyncio.Queue에서 제거합니다.
        반환: 제거된 Job (없으면 None).
        """
        async with self._lock:
            job = self._jobs.pop(order_id, None)
            if not job:
                return None
            self._stats[job.service_code]["queued"] = max(
                0, self._stats[job.service_code]["queued"] - 1
            )
            self._cancel_requested.discard(order_id)
            pending: List[Job] = []
            while True:
                try:
                    j = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if j.order_id != order_id:
                    pending.append(j)
            for j in pending:
                self._queue.put_nowait(j)
        logger.info("Purged queued order %s from asyncio queue", order_id)
        return job

    async def delete_order_with_artifacts(self, order_id: str) -> tuple:
        """
        주문 레코드를 제거하고 analysis/output/log 트리 삭제 (FASTQ 제외).
        Returns:
            (ok: bool, message: str, detail: dict)
        """
        job = self.get_job(order_id)
        if not job:
            return False, f"Order not found: {order_id}", {}

        blocked = (
            OrderStatus.RUNNING,
            OrderStatus.DOWNLOADING,
            OrderStatus.PROCESSING,
            OrderStatus.UPLOADING,
            OrderStatus.RECEIVED,
        )
        if job.status in blocked:
            return (
                False,
                "Cannot delete while the job is active; use Stop first, then Delete.",
                {},
            )

        if job.service_code in _CARRIER_LIKE:
            apply_carrier_layout_directories(job)
        elif job.service_code == "sgnipt":
            apply_sgnipt_layout_directories(job)
        elif job.service_code == "nipt":
            apply_nipt_layout_directories(job)

        if job.status == OrderStatus.QUEUED:
            pj = await self.purge_queued_order(order_id)
            if pj is None:
                logger.warning(
                    "delete_order_with_artifacts: QUEUED %s not in asyncio queue; forcing forget",
                    order_id,
                )
                await self.forget_order(order_id)
            else:
                job = pj

        deleted, errs = await asyncio.to_thread(delete_run_artifacts, job)

        forgotten = await self.forget_order(order_id)

        if self._store:
            try:
                await asyncio.to_thread(self._store.delete_order, order_id)
            except Exception as e:
                logger.error("delete_order from store for %s: %s", order_id, e)

        detail = {"deleted": deleted, "errors": errs, "memory_cleared": forgotten is not None}
        msg = f"Order {order_id} removed; deleted {len(deleted)} directory tree(s)"
        if errs:
            msg += f"; {len(errs)} path warning(s)"
        return True, msg, detail

    async def purge_order_db_only(self, order_id: str, force: bool = False) -> tuple:
        """
        SQLite 행과 메모리·큐에서만 제거합니다 (디스크 analysis/output/log 미삭제).
        job_json 손상으로 부팅 시 복원되지 않은 행도 SQLite에서 지울 수 있습니다.
        force=True 이면 RUNNING 등 활성 상태여도 DB/메모리에서 제거합니다(파이프라인 프로세스는 그대로).
        """
        job = self.get_job(order_id)
        blocked = (
            OrderStatus.RUNNING,
            OrderStatus.DOWNLOADING,
            OrderStatus.PROCESSING,
            OrderStatus.UPLOADING,
            OrderStatus.RECEIVED,
        )
        if job and job.status in blocked and not force:
            return (
                False,
                "Stop the job before removing this record from the database, or use Purge with Force.",
                {},
            )
        if job and job.status in blocked and force:
            logger.warning(
                "purge_order_db_only force=True for active job %s status=%s",
                order_id,
                job.status.value,
            )

        forgotten = await self.forget_order(order_id)
        sqlite_rows = 0
        if self._store:
            try:
                sqlite_rows = await asyncio.to_thread(
                    self._store.delete_order, order_id
                )
            except Exception as e:
                logger.error("purge_order_db_only SQLite %s: %s", order_id, e)
                return False, f"SQLite delete failed: {e}", {}

        msg = f"Order {order_id} removed from database and daemon memory"
        if forgotten is None and job is None:
            msg += " (no live job in memory; SQLite row deleted if it existed)"
        detail = {
            "had_memory_job": forgotten is not None,
            "sqlite_rows_deleted": sqlite_rows,
        }
        if sqlite_rows == 0:
            msg += " — SQLite: no row matched this order_id (already removed or typo)."
        return True, msg, detail

    async def request_cancel_queued(self, order_id: str) -> bool:
        """QUEUED 상태 주문에 대해 dequeue 시 버리도록 표시."""
        async with self._lock:
            job = self._jobs.get(order_id)
            if not job or job.status != OrderStatus.QUEUED:
                return False
            self._cancel_requested.add(order_id)
        logger.info(f"Cancel requested for queued order {order_id}")
        return True

    async def mark_cancelled(self, job: Job, message: str = "Cancelled by user"):
        """실행 중 사용자 취소 등 (실패 통계 없이 종료 처리)."""
        async with self._lock:
            job.status = OrderStatus.CANCELLED
            job.completed_at = now_kst_iso()
            job.updated_at = now_kst_iso()
            job.message = message
            self._running_jobs.pop(job.order_id, None)
            self._jobs.pop(job.order_id, None)
            self._saved_jobs.pop(job.order_id, None)
            self._completed_jobs[job.order_id] = job
            self._stats[job.service_code]["running"] = max(
                0, self._stats[job.service_code]["running"] - 1
            )
        logger.info(f"[{job.service_code}] Job {job.order_id} CANCELLED")
        await self.persist_job(job)

    async def update_queued_job_fastq_paths(
        self,
        order_id: str,
        fastq_r1_path: Optional[str] = None,
        fastq_r2_path: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        QUEUED 상태인 주문만 로컬 FASTQ 경로 변경.
        fastq_r*_path 가 None 이면 해당 필드는 변경하지 않음.
        """
        async with self._lock:
            job = self._jobs.get(order_id) or self._saved_jobs.get(order_id)
            if not job:
                return False, "Order not found"
            if job.status not in (OrderStatus.QUEUED, OrderStatus.SAVED):
                return False, "Only SAVED or QUEUED orders can update FASTQ paths"
            if fastq_r1_path is not None:
                job.fastq_r1_path = fastq_r1_path or None
            if fastq_r2_path is not None:
                job.fastq_r2_path = fastq_r2_path or None
            job.updated_at = now_kst_iso()
        if job.service_code in _CARRIER_LIKE:
            apply_carrier_layout_directories(job)
        elif job.service_code == "sgnipt":
            apply_sgnipt_layout_directories(job)
        elif job.service_code == "nipt":
            apply_nipt_layout_directories(job)
        await self.persist_job(job)
        return True, ""

    def get_running_jobs(self, service_code: Optional[str] = None) -> List[Job]:
        """실행 중인 작업 목록"""
        jobs = list(self._running_jobs.values())
        if service_code:
            jobs = [j for j in jobs if j.service_code == service_code]
        return jobs

    def iter_all_jobs_unique(self) -> List[Job]:
        """saved / queued / running / completed 버킷에서 order_id 기준 중복 제거 목록."""
        seen: Set[str] = set()
        out: List[Job] = []
        for bucket in (self._saved_jobs, self._jobs, self._running_jobs, self._completed_jobs):
            for oid, job in bucket.items():
                if oid not in seen:
                    seen.add(oid)
                    out.append(job)
        return out

    def snapshot_stats_by_service(self) -> Dict[str, Dict[str, int]]:
        """대시보드·버킷 목록과 동일한 기준의 현재 스냅샷 통계."""
        running_ids = set(self._running_jobs.keys())
        by_svc: Dict[str, Dict[str, int]] = {}
        for j in self.iter_all_jobs_unique():
            svc = j.service_code
            if svc not in by_svc:
                by_svc[svc] = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
            st = by_svc[svc]
            if j.status == OrderStatus.QUEUED:
                st["queued"] += 1
            elif j.order_id in running_ids:
                st["running"] += 1
            elif j.status in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY):
                st["completed"] += 1
            elif j.status == OrderStatus.FAILED:
                st["failed"] += 1
        return by_svc

    def get_dashboard_bucket_jobs(
        self, bucket: str, service_code: Optional[str] = None
    ) -> List[Job]:
        """queued / running / completed / failed — 현재 시점 주문 스냅샷."""
        b = (bucket or "").strip().lower()
        jobs = self.iter_all_jobs_unique()
        if service_code and str(service_code).strip():
            sc = str(service_code).strip()
            jobs = [j for j in jobs if j.service_code == sc]
        if b == "queued":
            return [j for j in jobs if j.status == OrderStatus.QUEUED]
        if b == "running":
            return [j for j in jobs if j.order_id in self._running_jobs]
        if b == "completed":
            return [
                j
                for j in jobs
                if j.status in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY)
            ]
        if b == "failed":
            return [j for j in jobs if j.status == OrderStatus.FAILED]
        return []

    def get_summary(self) -> QueueSummary:
        """
        Admin queue summary.

        - Live: queued / running (+ per-service max_parallel from slot groups)
        - Today (KST): completed_today / failed_today by ``completed_at`` (fallback ``started_at``)
        - Legacy fields kept for older Portal / service-daemon clients
        """
        today = now_kst_date_iso()
        stats_by_service = self.snapshot_stats_by_service()
        slots = self.group_slot_status

        enabled = [
            s.strip()
            for s in (settings.enabled_service_list or [])
            if isinstance(s, str) and s.strip()
        ]
        service_codes = list(dict.fromkeys(
            enabled
            + list(stats_by_service.keys())
            + [c for codes in _GROUP_SERVICES.values() for c in codes]
        ))

        today_completed: Dict[str, int] = defaultdict(int)
        today_failed: Dict[str, int] = defaultdict(int)
        for j in self.iter_all_jobs_unique():
            svc = j.service_code or ""
            day = _iso_to_kst_date(j.completed_at) or _iso_to_kst_date(j.started_at)
            if day != today:
                continue
            if j.status in (OrderStatus.COMPLETED, OrderStatus.REPORT_READY):
                today_completed[svc] += 1
            elif j.status == OrderStatus.FAILED:
                today_failed[svc] += 1

        services: List[QueueSummaryServiceRow] = []
        for svc in service_codes:
            group = _SERVICE_GROUP.get(svc, "unknown")
            slot = slots.get(group, {})
            st = stats_by_service.get(svc, {})
            services.append(
                QueueSummaryServiceRow(
                    service_code=svc,
                    display_name=_SERVICE_DISPLAY.get(svc, svc),
                    slot_group=group,
                    max_parallel=int(slot.get("limit", 0)),
                    running=int(st.get("running", 0)),
                    queued=int(st.get("queued", 0)),
                    available=int(slot.get("available", 0)),
                    completed_today=int(today_completed.get(svc, 0)),
                    failed_today=int(today_failed.get(svc, 0)),
                )
            )

        slot_groups: List[QueueSummarySlotGroup] = []
        for group, limit in self._group_limits.items():
            slot = slots.get(group, {})
            slot_groups.append(
                QueueSummarySlotGroup(
                    group=group,
                    max_parallel=int(slot.get("limit", limit)),
                    running=int(slot.get("running", 0)),
                    queued=int(slot.get("queued", 0)),
                    available=int(slot.get("available", 0)),
                    services=list(_GROUP_SERVICES.get(group, [])),
                )
            )

        total_queued = sum(s.queued for s in services)
        total_running = sum(s.running for s in services)
        completed_today = sum(s.completed_today for s in services)
        failed_today = sum(s.failed_today for s in services)

        # Legacy snapshot totals (all jobs still known to daemon — not "today")
        total_completed = sum(s["completed"] for s in stats_by_service.values())
        total_failed = sum(s["failed"] for s in stats_by_service.values())

        running_jobs = [
            {
                "order_id": j.order_id,
                "service_code": j.service_code,
                "sample_name": j.sample_name,
                "status": j.status.value,
                "progress": j.progress,
                "message": j.message or "",
                "started_at": j.started_at,
            }
            for j in self._running_jobs.values()
        ]

        jobs_by_service = {
            svc: int(st.get("queued", 0)) + int(st.get("running", 0))
            for svc, st in stats_by_service.items()
        }
        stats_out = {
            svc: {
                "queued": int(st["queued"]),
                "running": int(st["running"]),
                "completed": int(st["completed"]),
                "failed": int(st["failed"]),
            }
            for svc, st in stats_by_service.items()
        }

        return QueueSummary(
            today=today,
            totals=QueueSummaryTotals(
                queued=total_queued,
                running=total_running,
                completed_today=completed_today,
                failed_today=failed_today,
            ),
            services=services,
            slot_groups=slot_groups,
            running_jobs=running_jobs,
            total_queued=total_queued,
            total_running=total_running,
            total_completed=total_completed,
            total_failed=total_failed,
            jobs_by_service=jobs_by_service,
            stats_by_service=stats_out,
        )

    @property
    def available_slots(self) -> int:
        """사용 가능한 실행 슬롯 수 (모든 서비스 그룹의 잔여 슬롯 합계)"""
        return sum(s._value for s in self._group_semaphores.values())

    @property
    def group_slot_status(self) -> Dict[str, Dict]:
        """서비스 그룹별 슬롯 현황 (limit / running / available / queued).

        /health 및 /queue/status 엔드포인트에서 사용.
        """
        queued_by_group: Dict[str, int] = defaultdict(int)
        for job in self._jobs.values():
            if job.status == OrderStatus.QUEUED:
                g = _SERVICE_GROUP.get(job.service_code, "unknown")
                queued_by_group[g] += 1

        result: Dict[str, Dict] = {}
        for group, sem in self._group_semaphores.items():
            limit = self._group_limits[group]
            running = limit - sem._value
            result[group] = {
                "limit": limit,
                "running": max(0, running),
                "available": max(0, sem._value),
                "queued": queued_by_group.get(group, 0),
            }
        return result

    def toggle_priority(self, enabled: bool) -> None:
        """NIPT 우선순위 모드 런타임 전환 (재시작 불필요).

        이미 큐에 있는 작업은 enqueue 당시의 우선순위로 유지되므로
        전환 직후 몇 개의 잡은 이전 모드의 순서로 처리될 수 있습니다.
        새로 들어오는 잡부터 새 우선순위가 적용됩니다.

        NIPT 우선순위 슬롯 한도도 함께 전환됩니다:
          enabled=True  → max_concurrent_nipt_priority
          enabled=False → max_concurrent_nipt
        """
        from .config import settings as s
        new_nipt_limit = (
            s.max_concurrent_nipt_priority if enabled else s.max_concurrent_nipt
        )
        self._nipt_priority = enabled
        self._group_limits["nipt"] = new_nipt_limit
        self._group_semaphores["nipt"] = asyncio.Semaphore(new_nipt_limit)
        logger.info(
            "NIPT priority toggled → %s (nipt_limit=%d)",
            "ON" if enabled else "OFF",
            new_nipt_limit,
        )

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent


# 전역 인스턴스
_queue_manager: Optional[QueueManager] = None


def get_queue_manager() -> QueueManager:
    """전역 QueueManager 인스턴스 반환"""
    global _queue_manager
    if _queue_manager is None:
        store = OrderStore(settings.resolved_orders_db_path)
        _queue_manager = QueueManager(store=store)
    return _queue_manager
