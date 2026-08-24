"""
GX-Daemon Data Models

Merges:
- Platform-facing models from nipt-daemon (SubmitOrderDto, OrderDetailResponse, FullOrder)
- Internal models from service-daemon (Job, OrderStatus, review/report models)
"""

from enum import Enum
from typing import Optional, Dict, Any, List, Literal
from pydantic import BaseModel, Field, Extra, model_validator

from .datetime_kst import now_kst_iso


# ─── Enums ─────────────────────────────────────────────────

class OrderStatus(str, Enum):
    RECEIVED = "RECEIVED"
    SAVED = "SAVED"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    RUNNING = "RUNNING"
    PROCESSING = "PROCESSING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    REPORT_READY = "REPORT_READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ServiceCode(str, Enum):
    CARRIER_SCREENING = "carrier_screening"
    WHOLE_EXOME = "whole_exome"
    HEALTH_SCREENING = "health_screening"


class NotificationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOT_FOUND = "NOT_FOUND"
    SKIPPED = "SKIPPED"


class SequencingMethodType(Enum):
    REMOTE = "remote"
    LOCAL = "local"


# ─── Platform-facing Models (from nipt-daemon) ─────────────

class SubmitOrderDto(BaseModel):
    """POST /analysis/{service_code}/order/{order_id}/submit — Platform submit payload.

    모든 서비스 공통 최소 body.
    - sequencingDataMethod: REMOTE(Platform S3 다운로드) / LOCAL(로컬 디렉토리 탐색)
    - labIdentifier: Lab별 reference data 선택에 사용 (NIPT 필수, carrier/sgNIPT 무시)
    - type: 레거시 Cloud/Gx-Portal body 필드 (선택). URL typed submit에서는 무시됨.

    임상 정보(patientBirth, sampleBarcode 등)는 Platform API GET으로 가져오므로 body 불필요.
    """
    sequencingDataMethod: str
    labIdentifier: List[str] = Field(default_factory=list)
    # Legacy Cloud Portal / Gx-Portal may send type="CLIENT" | "nipt" | ...
    type: Optional[str] = None

    def is_remotedata_client(self) -> bool:
        return self.sequencingDataMethod.lower() == SequencingMethodType.REMOTE.value

    def is_localdata_client(self) -> bool:
        return self.sequencingDataMethod.lower() == SequencingMethodType.LOCAL.value


class OrderDetailSubmit(BaseModel):
    """Internal simplified order detail for submit processing"""
    id: str
    clientId: str
    patientBirth: Optional[str] = None
    age: Optional[int] = None
    labIdentifier: Optional[List[str]] = None
    sampleBarcode: Optional[str] = None


class OrderDetailResponse(BaseModel):
    """GET /analysis/order/{order_id} response from Platform.

    gx-daemon Portal API는 임상 정보를 serviceData 하위에 중첩하므로,
    get_order_detail()에서 serviceData 필드를 최상위로 flat merge 후 파싱.
    """
    id: str
    clientId: Optional[str] = None
    partnerId: Optional[str] = None
    hospitalId: Optional[str] = None
    packageId: Optional[str] = None
    serviceType: Optional[str] = None
    patientName: Optional[str] = None
    patientBirth: Optional[str] = None
    patientGender: Optional[str] = None
    height: Optional[float] = None
    weight: Optional[float] = None
    gestationalAgeWeeks: Optional[int] = Field(None, alias="gaWeeks")
    gestationalAgeDays: Optional[int] = Field(None, alias="gaDays")
    pregnancyType: Optional[str] = None
    doctor: Optional[str] = None
    sampleSpecimenType: Optional[str] = None
    sampleCollectedAt: Optional[str] = None
    medicalRecordId: Optional[str] = None
    sequencingBatchId: Optional[str] = None
    receiptDate: Optional[str] = None
    reportLanguage: Optional[List[str]] = None
    reportDate: Optional[str] = None
    templateKey: Optional[str] = None
    indication: Optional[List[str]] = Field(None, alias="indicationForTesting")
    indicationForTestingSpecify: Optional[str] = None
    sampleBarcode: Optional[str] = None
    sampleId: Optional[str] = None
    showFetalGender: Optional[str] = None
    resample: Optional[str] = None
    status: Optional[str] = None
    hospital: Optional[Dict[str, Any]] = None
    package: Optional[Dict[str, Any]] = None
    pdf: Optional[Dict[str, Any]] = None

    model_config = {"populate_by_name": True}

    def get_template_key(self) -> str:
        return self.templateKey or "Default"


class SequencingAsset(BaseModel):
    filename: str
    path: str


class SequencingRecord(BaseModel):
    id: str
    orderId: str
    clientId: Optional[str] = None
    asset: SequencingAsset

    class Config:
        extra = "ignore"


class FullOrder(BaseModel):
    """Complete order info for pipeline execution"""
    orderId: str
    clientId: str
    sequencingDataMethod: str
    lab: Optional[str] = None
    age: Optional[int] = None
    r1_id: str
    r2_id: str
    r1_path: str
    r2_path: str


class AnalysisStatus(BaseModel):
    orderId: str
    status: str
    detail: Optional[str] = None


class ClientDetailResponse(BaseModel):
    id: str
    name: str
    address: Optional[str] = None
    phoneNumber: Optional[str] = None
    email: Optional[str] = None
    identifier: Optional[str] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


class ClientApiResponse(BaseModel):
    status: int
    message: str
    data: ClientDetailResponse
    responseAt: str


class PartnerClientResponse(BaseModel):
    id: str
    name: str
    address: Optional[str] = None
    phoneNumber: Optional[str] = None
    email: Optional[str] = None
    identifier: Optional[str] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


class PartnerClientApiResponse(BaseModel):
    status: int
    message: str
    data: PartnerClientResponse
    responseAt: str


# ─── Internal Job/Request/Response Models (from service-daemon) ──

class OrderSubmitRequest(BaseModel):
    """Local API submit request (portal-style)"""
    order_id: str = Field(..., description="Platform에서 발급한 주문 ID")
    service_code: str = Field(..., description="서비스 코드")
    sample_name: Optional[str] = Field(default=None)
    work_dir: Optional[str] = Field(default=None)
    fastq_r1_url: Optional[str] = Field(default=None)
    fastq_r2_url: Optional[str] = Field(default=None)
    fastq_r1_path: Optional[str] = Field(default=None)
    fastq_r2_path: Optional[str] = Field(default=None)
    params: Optional[Dict[str, Any]] = Field(default_factory=dict)
    callback_url: Optional[str] = Field(
        default=None,
        description="결과 통보를 받을 Portal URL. 미설정 시 PLATFORM_API_BASE 사용 (Gx-Portal 기본값).",
    )


class OrderStatusResponse(BaseModel):
    order_id: str
    service_code: str
    status: OrderStatus
    progress: int = Field(default=0, ge=0, le=100)
    message: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrderSubmitResponse(BaseModel):
    status: str
    order_id: str
    service_code: str
    message: str
    queue_position: Optional[int] = None


class StartOrderRequest(BaseModel):
    fresh: bool = Field(default=False)
    use_ssd: bool = Field(
        default=False,
        description="Carrier / whole exome / health: run_analysis.sh --use-ssd (SSD scratch).",
    )
    scratch_dir: Optional[str] = Field(
        default=None,
        description="Host path for --scratch-dir when use_ssd is true.",
    )


class OrderSaveResponse(BaseModel):
    status: str
    order_id: str
    service_code: str
    message: str


class OrderUpdateRequest(BaseModel):
    order_id: Optional[str] = Field(default=None)
    sample_name: Optional[str] = None
    work_dir: Optional[str] = None
    fastq_r1_url: Optional[str] = None
    fastq_r2_url: Optional[str] = None
    fastq_r1_path: Optional[str] = None
    fastq_r2_path: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class OrderUpdateResponse(BaseModel):
    status: str
    order_id: str
    message: str


class UpdateFastqPathsRequest(BaseModel):
    fastq_r1_path: Optional[str] = Field(default=None)
    fastq_r2_path: Optional[str] = Field(default=None)


class DarkGenesSectionReviewItem(BaseModel):
    approved: bool = Field(default=False)
    notes: str = Field(default="")
    risk: Optional[Literal["low", "high"]] = Field(default=None)


class DarkGenesReviewRequest(BaseModel):
    section_reviews: List[DarkGenesSectionReviewItem] = Field(default_factory=list)


class PgxGeneReviewRow(BaseModel):
    gene: str = Field(..., min_length=1, max_length=64)
    reviewer_confirmed: bool = False
    reviewer_comment: str = Field(default="", max_length=4000)


class PgxCustomGeneReviewRow(BaseModel):
    """Per-variant review row for extended panel ``pgx.custom_gene_results[]`` (e.g. APOE rs429358 / rs7412)."""

    gene: str = Field(..., min_length=1, max_length=64)
    rsid: str = Field(..., min_length=1, max_length=32)
    reviewer_confirmed: bool = False
    reviewer_comment: str = Field(default="", max_length=4000)


class PgxReviewRequest(BaseModel):
    reviewer_notes: str = Field(default="", max_length=16000)
    reviewed: bool = Field(default=False)
    gene_reviews: List[PgxGeneReviewRow] = Field(default_factory=list)
    custom_gene_reviews: List[PgxCustomGeneReviewRow] = Field(
        default_factory=list,
        description="Updates reviewer_confirmed / reviewer_comment for matching gene+rsid in pgx.custom_gene_results",
    )
    include_apoe_proactive_pdf: bool = Field(
        default=False,
        description="Include APOE ε2/ε3/ε4 proactive summary block in customer PDF (proactive health orders)",
    )


class WesPanelCustomSave(BaseModel):
    id: str = Field(..., min_length=2, max_length=64)
    label: str = Field(..., min_length=1, max_length=220)
    category: str = Field(default="other", max_length=64)
    description: Optional[str] = Field(default=None, max_length=4000)
    backbone_bed: Optional[str] = Field(default=None)
    disease_bed: Optional[str] = Field(default=None)
    genes: Optional[List[str]] = Field(default=None)
    genes_text: Optional[str] = Field(default=None)
    gene_source_bed: Optional[str] = Field(default=None)
    skip_generated_bed: bool = Field(default=False)


class Job(BaseModel):
    """Internal job model (queue managed)"""
    order_id: str
    service_code: str
    sample_name: str
    work_dir: str
    fastq_dir: Optional[str] = None
    analysis_dir: Optional[str] = None
    output_dir: Optional[str] = None
    log_dir: Optional[str] = None
    fastq_r1_url: Optional[str] = None
    fastq_r2_url: Optional[str] = None
    fastq_r1_path: Optional[str] = None
    fastq_r2_path: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    # Per-order callback URL for multi-portal support.
    # When set, result notifications go to this URL instead of settings.platform_api_base.
    # Omit (or set None) to use the global PLATFORM_API_BASE (backward-compatible).
    callback_url: Optional[str] = None
    status: OrderStatus = OrderStatus.RECEIVED
    progress: int = 0
    message: str = ""
    created_at: str = Field(default_factory=now_kst_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    updated_at: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    duration: Optional[str] = None
    error_log: Optional[str] = None

    def update_status(self, status: OrderStatus, progress: int = None, message: str = None):
        self.status = status
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
        self.updated_at = now_kst_iso()

    @model_validator(mode="after")
    def _normalize_carrier_legacy_container_paths(self) -> "Job":
        if self.service_code not in ("carrier_screening", "whole_exome", "health_screening"):
            return self
        from .config import normalize_legacy_carrier_container_path

        def norm(s: Optional[str]) -> Optional[str]:
            if not s or not isinstance(s, str):
                return s
            return normalize_legacy_carrier_container_path(s) or s

        updates: Dict[str, Any] = {}
        for fname in ("fastq_dir", "analysis_dir", "output_dir", "log_dir", "fastq_r1_path", "fastq_r2_path"):
            v = getattr(self, fname)
            nv = norm(v)
            if nv != v:
                updates[fname] = nv

        p = dict(self.params or {})
        param_keys = ("main_vcf", "_prior_reuse_main_vcf_hint", "_prior_reuse_analysis_dir", "_prior_reuse_output_dir", "_prior_reuse_log_dir")
        p_changed = False
        for k in param_keys:
            if k in p and isinstance(p[k], str):
                nk = norm(p[k])
                if nk != p[k]:
                    p[k] = nk
                    p_changed = True
        if p_changed:
            updates["params"] = p

        if updates:
            return self.model_copy(update=updates)
        return self


class NotificationResult(BaseModel):
    status: NotificationStatus
    message: str = ""
    response_code: Optional[int] = None


class OutputFile(BaseModel):
    file_path: str = Field(..., description="로컬 파일 경로")
    file_type: str = Field(..., description="파일 유형")
    file_name: Optional[str] = Field(default=None)
    content_type: str = Field(default="application/octet-stream")


class ReportGenerateRequest(BaseModel):
    confirmed_variants: List[Dict[str, Any]] = Field(...)
    reviewer_info: Dict[str, Any] = Field(...)
    patient_info: Optional[Dict[str, Any]] = Field(default=None)
    partner_info: Optional[Dict[str, Any]] = Field(default=None)
    languages: Optional[List[str]] = Field(default=None)


class ReportGenerateResponse(BaseModel):
    status: str
    order_id: str
    service_code: str
    report_files: List[str] = Field(default_factory=list)
    uploaded_count: int = 0
    message: str = ""


class GeneKnowledgeSaveRequest(BaseModel):
    gene: str = Field(...)
    lang: str = Field(default="EN")
    function_summary: str = Field(default="")
    disease_association: str = Field(default="")
    disorder: str = Field(default="")
    omim_number: str = Field(default="")
    inheritance: str = Field(default="")


class VariantKnowledgeSaveRequest(BaseModel):
    variant_key: str = Field(...)
    lang: str = Field(default="EN")
    variant_notes: str = Field(default="")


class QueueSummaryTotals(BaseModel):
    """Admin Summary — live + today counters."""

    queued: int = 0
    running: int = 0
    completed_today: int = 0
    failed_today: int = 0


class QueueSummaryServiceRow(BaseModel):
    service_code: str
    display_name: str = ""
    slot_group: str
    max_parallel: int = 0
    running: int = 0
    queued: int = 0
    available: int = 0
    completed_today: int = 0
    failed_today: int = 0


class QueueSummarySlotGroup(BaseModel):
    group: str
    max_parallel: int = 0
    running: int = 0
    queued: int = 0
    available: int = 0
    services: List[str] = Field(default_factory=list)


class QueueSummary(BaseModel):
    """
    Admin queue summary for Portal Dashboard.

    Cloud Portal / nipt-daemon compatible fields:
      requested_samples_today, running ("n/max"), queue_waiting,
      completed_today, failed_today, requested_samples_total,
      completed_total, failed_total, details{*_list, legacy maps}

    gx-portal also uses: ``totals``, ``services``, ``slot_groups``, ``running_jobs``, ``today``.
    """

    # ── Cloud Portal / nipt-daemon compatible ─────────────
    # _today = calendar day (UTC); _total = in-memory cumulative while daemon is up
    requested_samples_today: int = 0
    running: Any = 0  # HTTP: "n/max" string (Cloud Portal); internals also keep totals.running
    max_parallel: int = 0
    queue_waiting: int = 0
    completed_today: int = 0
    failed_today: int = 0
    requested_samples_total: int = 0
    completed_total: int = 0
    failed_total: int = 0
    details: Dict[str, Any] = Field(default_factory=dict)

    # ── gx-portal Admin schema ────────────────────────────
    today: str = ""  # YYYY-MM-DD (KST)
    totals: QueueSummaryTotals = Field(default_factory=QueueSummaryTotals)
    services: List[QueueSummaryServiceRow] = Field(default_factory=list)
    slot_groups: List[QueueSummarySlotGroup] = Field(default_factory=list)
    running_jobs: List[Dict[str, Any]] = Field(default_factory=list)

    # ── Legacy (service-daemon / older Portal) ────────────
    total_queued: int = 0
    total_running: int = 0
    total_completed: int = 0
    total_failed: int = 0
    jobs_by_service: Dict[str, int] = Field(default_factory=dict)
    stats_by_service: Dict[str, Dict[str, int]] = Field(default_factory=dict)


# Aliases for backward compatibility
OrderDetail = OrderDetailSubmit
