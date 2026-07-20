"""
GX-Daemon Configuration

Merges:
- nipt-daemon auth/platform fields (AWS_API_BASE → PLATFORM_API_BASE)
- service-daemon carrier screening / annotation / queue fields
"""

import os
from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, model_validator

_LEGACY_CARRIER_DATA_ROOT = "/data/carrier_screening"
_GX_EXOME_DATA_ROOT = "/data/gx-exome"
_LEGACY_CARRIER_WORK_ROOT = "/data/carrier_screening_work"
_GX_EXOME_WORK_ROOT = "/data/gx-exome-work"


def normalize_legacy_carrier_container_path(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return value
    t = value.strip()
    if not t:
        return value
    if t == _LEGACY_CARRIER_DATA_ROOT or t.startswith(_LEGACY_CARRIER_DATA_ROOT + "/"):
        return _GX_EXOME_DATA_ROOT + t[len(_LEGACY_CARRIER_DATA_ROOT):]
    if t == _LEGACY_CARRIER_WORK_ROOT or t.startswith(_LEGACY_CARRIER_WORK_ROOT + "/"):
        return _GX_EXOME_WORK_ROOT + t[len(_LEGACY_CARRIER_WORK_ROOT):]
    return value


class Settings(BaseSettings):
    """GX-Daemon settings (platform auth + carrier screening)"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── Application ───────────────────────────────────────
    app_name: str = Field(default="gx-daemon", description="Application name")
    app_env: str = Field(default="dev", description="Environment: dev, staging, prod")
    app_port: int = Field(default=8000, description="API server port")
    log_level: str = Field(default="INFO", description="Logging level")
    debug_http: bool = Field(default=False, env="DEBUG_HTTP", description="HTTP request/response 상세 로깅")

    # ─── Inbound API protection (optional) ─────────────────
    api_key: Optional[str] = Field(default=None)

    # ─── Platform API (from nipt-daemon's AWS_API_BASE) ────
    platform_api_base: str = Field(
        default="https://api.genolyx.com",
        description="Platform API base URL",
    )
    platform_api_enabled: bool = Field(default=True)
    auth_url: Optional[str] = Field(default=None, description="Authentication endpoint URL")
    api_username: Optional[str] = Field(default=None, description="API login username")
    api_password: Optional[str] = Field(default=None, description="API login password")
    # Fallback service code for Platform API submissions (/analysis/order/{id}/submit).
    # Gx-Portal sends type="CLIENT" (patient/order classification, not service type).
    # Set to the service this Portal instance manages:
    #   nipt             — Gx-Portal (NIPT 전용, 단계 2~3)
    #   carrier_screening — Carrier portal
    platform_submit_default_service: str = Field(
        default="nipt",
        description="Default service_code for Platform API submit when type field doesn't match any alias.",
    )
    # Custom service code alias overrides (comma-separated key:value).
    # Maps Portal service codes → internal daemon service_code.
    # Example: PLATFORM_SERVICE_ALIASES=nipt2:nipt,exome2:carrier_screening
    # Built-in aliases (nipt/nipt2/gx-nipt→nipt, carrier/exome→carrier_screening, etc.)
    # are always available; this env var extends/overrides them.
    platform_service_aliases: str = Field(
        default="",
        description="Extra portal code → internal service_code mappings (key:value,key:value).",
    )

    # ─── Telegram ──────────────────────────────────────────
    telegram_bot_token: Optional[str] = Field(default=None)
    telegram_chat_ids: Optional[str] = Field(default=None)
    telegram_notify_enabled: bool = Field(default=True)

    # ─── Queue / Worker ────────────────────────────────────
    # Legacy global limit — kept for backward compat but per-group limits below take precedence.
    max_concurrent_jobs: int = Field(default=8)
    queue_poll_interval: int = Field(default=30)

    # Per-service-group concurrency limits.
    # NIPT is lightweight (small FASTQ, ~30 min); exome-based services are CPU/RAM heavy.
    #   nipt              : gx-nipt
    #   exome             : carrier_screening, whole_exome, health_screening
    #   sgnipt            : sgNIPT (single-gene NIPT, exome-scale but lighter than carrier WES)
    # Total workers = sum of all groups; set each to match your server spec.
    max_concurrent_nipt: int = Field(default=4)
    max_concurrent_exome: int = Field(default=2)
    max_concurrent_sgnipt: int = Field(default=1)

    # NIPT Priority mode.
    # When true:
    #   - Uses PriorityQueue so NIPT jobs are always dequeued before exome/sgnipt.
    #   - NIPT concurrent limit switches to max_concurrent_nipt_priority (higher).
    #   - Exome/sgnipt jobs wait in queue until no NIPT jobs are pending.
    # When false: plain FIFO queue with equal per-group limits (balanced mode).
    nipt_priority: bool = Field(default=False)
    max_concurrent_nipt_priority: int = Field(default=8)

    # ─── Base Directories ──────────────────────────────────
    base_dir: str = Field(default="/data")
    orders_db_path: Optional[str] = Field(default=None)
    fastq_base_dir: str = Field(default="/data/fastq")

    # ─── Carrier Screening ─────────────────────────────────
    carrier_screening_host: Optional[str] = Field(
        default="/home/ken/gx-exome",
        description="Host path to gx-exome repo (compose bind-mount source; nested docker -v)",
    )
    carrier_screening_fastq_dir: str = Field(default="/data/gx-exome/fastq")
    analysis_base_dir: str = Field(default="/data/analysis")
    output_base_dir: str = Field(default="/data/output")
    log_base_dir: str = Field(default="/data/log")

    carrier_screening_pipeline_dir: str = Field(default="/opt/pipelines/carrier-screening")
    carrier_screening_main_nf: Optional[str] = Field(default=None)
    dark_gene_pipeline_dir: Optional[str] = Field(default=None)
    dark_gene_skip_cnv: bool = Field(default=True)
    carrier_screening_force_direct_nextflow: bool = Field(default=False)
    carrier_screening_nextflow_profile: Optional[str] = Field(default=None)
    carrier_screening_run_script: Optional[str] = Field(default=None)
    carrier_screening_script_data_dir: Optional[str] = Field(default=None)
    carrier_screening_script_ref_dir: Optional[str] = Field(default=None)
    carrier_screening_script_extra_args: str = Field(default="")
    carrier_screening_fresh_append_nf_live_log: bool = Field(
        default=True,
        description=(
            "When Force Run (Fresh) invokes run_analysis.sh, append --nf-live-log so stdout shows a live "
            "Nextflow process table. Full audit remains in gx-exome log/nextflow.log and log/trace.txt "
            "(per-task metrics; resume shows CACHED where applicable). Set false for quieter pipeline.log."
        ),
    )
    carrier_screening_artifact_base: Optional[str] = Field(default=None)
    carrier_screening_report_output_root: Optional[str] = Field(default=None)
    carrier_screening_report_template_dir: Optional[str] = Field(default=None)
    carrier_capture_panel_bed_dir: Optional[str] = Field(default=None)
    carrier_default_backbone_bed: Optional[str] = Field(default=None)
    carrier_default_disease_bed: Optional[str] = Field(default=None)

    # ─── WES Panels ────────────────────────────────────────
    wes_panels_json: Optional[str] = Field(default=None)
    wes_panels_custom_json: Optional[str] = Field(default=None)
    wes_panels_generated_dir: Optional[str] = Field(default=None)
    wes_panel_gene_source_bed: Optional[str] = Field(default=None)

    # ─── Nextflow ──────────────────────────────────────────
    nextflow_executable: str = Field(default="nextflow")
    nextflow_config: Optional[str] = Field(default=None)

    # ─── Reference Data ────────────────────────────────────
    ref_fasta: Optional[str] = Field(default=None)
    ref_fai: Optional[str] = Field(default=None)
    ref_dict: Optional[str] = Field(default=None)
    ref_bwa_indices: Optional[str] = Field(default=None)

    # ─── Annotation Resources ──────────────────────────────
    clinvar_vcf: Optional[str] = Field(default=None)
    gnomad_vcf: Optional[str] = Field(default=None)
    gnomad_dir: Optional[str] = Field(default=None)
    gnomad_genomes_glob: str = Field(default="gnomad.genomes.v*.sites*.bgz")
    gnomad_exomes_glob: str = Field(default="gnomad.exomes.v*.sites*.bgz")
    dbsnp_vcf: Optional[str] = Field(default=None)
    snpeff_jar: Optional[str] = Field(default=None)
    snpeff_db: str = Field(default="GRCh38.86")
    snpeff_data_dir: Optional[str] = Field(default=None)
    clingen_tsv: Optional[str] = Field(default=None)
    mane_gff: Optional[str] = Field(default=None)
    gene_bed: Optional[str] = Field(default=None)
    hpo_gene_file: Optional[str] = Field(default=None)
    curated_variants_db: Optional[str] = Field(default=None)
    disease_db_dir: Optional[str] = Field(default=None)
    disease_gene_json: Optional[str] = Field(default=None)
    proactive_gene_disease_json: Optional[str] = Field(default=None)
    gene_knowledge_db: Optional[str] = Field(default=None)
    gene_knowledge_db_fallback_path: Optional[str] = Field(default=None)
    gene_knowledge_enrich_on_report: bool = Field(default=True)
    gene_knowledge_gemini_on_report: bool = Field(default=True)
    gene_knowledge_gemini_model: str = Field(default="gemini-2.5-flash")
    hgmd_vcf: Optional[str] = Field(default=None)
    known_carrier_genes: Optional[str] = Field(default=None)
    acmg_sf_genes: Optional[str] = Field(default=None)

    # ─── Report Templates ──────────────────────────────────
    report_template_dir: Optional[str] = Field(default=None)
    report_languages: str = Field(default="EN,CN")
    report_logo_path: Optional[str] = Field(default=None)

    # ─── AI Classification ─────────────────────────────────
    acmg_ai_enabled: bool = Field(default=False)
    acmg_ai_provider: str = Field(default="gemini")
    acmg_ai_model: str = Field(default="gemini-2.5-flash")
    gemini_api_key: str = Field(default="")
    gemini_api_key_env_file: Optional[str] = Field(default=None)
    acmg_ai_api_key: str = Field(default="")
    # Ollama / OpenAI-compatible local LLM
    ollama_base_url: str = Field(
        default="http://host.docker.internal:11434/v1",
        description="OpenAI-compatible base URL for Ollama (container→host)",
    )
    ollama_model: str = Field(
        default="qwen2.5:32b",
        description="Default Ollama model name",
    )
    # Gene knowledge AI provider (overrides gemini when set to 'ollama')
    gene_knowledge_ai_provider: str = Field(
        default="gemini",
        description="AI provider for gene knowledge fetch: 'gemini' or 'ollama'",
    )
    gene_knowledge_ai_model: Optional[str] = Field(
        default=None,
        description="Model override for gene knowledge AI (falls back to gemini/ollama default)",
    )

    # ─── Literature Search ─────────────────────────────────
    literature_enabled: bool = Field(default=True)
    literature_db_path: Optional[str] = Field(default=None)
    literature_max_results: int = Field(default=10)
    ncbi_email: str = Field(default="gx-daemon@example.com")
    ncbi_api_key: str = Field(default="")
    ncbi_tool: str = Field(default="gx-daemon")

    # ─── Enabled Services ──────────────────────────────────
    enabled_services: str = Field(default="carrier_screening,whole_exome,health_screening,sgnipt,nipt")

    # ─── NIPT (gx-nipt / Nextflow) ─────────────────────────
    # nipt_root_dir is the base under which {fastq,analysis,output,log,config}
    # subdirs live. ``run_nipt.sh`` and the NIPTPlugin both expect this layout:
    #   {nipt_root_dir}/fastq/<work_dir>/<order_id>/R[12].fastq.gz
    #   {nipt_root_dir}/analysis/<work_dir>/<order_id>/
    #   {nipt_root_dir}/output/<work_dir>/<order_id>/<order_id>.json
    #   {nipt_root_dir}/log/<work_dir>/<order_id>/pipeline.log
    #   {nipt_root_dir}/config/<labcode>/pipeline_config.json
    # ``nipt_pipeline_dir`` is the gx-nipt source checkout (has main.nf + bin/).
    nipt_root_dir: str = Field(
        default="/home/ken/gx-nipt-data",
        description="Base directory for gx-nipt fastq/analysis/output/log/config",
    )
    nipt_fastq_dir: str = Field(default="/home/ken/gx-nipt-data/fastq")
    nipt_output_dir: str = Field(default="/home/ken/gx-nipt-data/output")
    nipt_analysis_dir: str = Field(default="/home/ken/gx-nipt-data/analysis")
    nipt_log_dir: str = Field(default="/home/ken/gx-nipt-data/log")
    nipt_config_dir: str = Field(default="/home/ken/gx-nipt-data/config")
    nipt_script_dir: str = Field(default="/home/ken/gx-nipt/bin")
    nipt_run_script: Optional[str] = Field(
        default=None,
        description="Override path to bin/run_nipt.sh (defaults to <nipt_pipeline_dir>/bin/run_nipt.sh)",
    )
    nipt_default_labcode: str = Field(
        default="",
        description="Fallback labcode used when the Platform DTO does not specify one",
    )
    # Reference data root. The directory is bind-mounted (read-only) into the
    # gx-nipt Docker container at the same path. Expected layout:
    #   ${NIPT_REF_DIR}/genomes/hg19/hg19.fa(+ BWA-MEM2 index)
    #   ${NIPT_REF_DIR}/hmmcopy/hg19.{50kb,10mb}.{gc,map}.wig
    #   ${NIPT_REF_DIR}/models/seqff_model.pkl
    #   ${NIPT_REF_DIR}/labs/<labcode>/{WC,WCX,EZD,PRIZM}/<group>/...
    #   ${NIPT_REF_DIR}/labs/<labcode>/bed/...
    nipt_ref_dir: str = Field(
        default="/data/reference",
        description="Host path to the gx-nipt reference data root",
    )
    # SSD scratch knobs (exposed to run_nipt.sh)
    nipt_use_ssd: bool = Field(default=False, description="Enable SSD scratch profile for gx-nipt")
    nipt_scratch_dir: Optional[str] = Field(
        default=None,
        description="Scratch directory on fast storage (e.g. /tmp/nipt_scratch)",
    )
    nipt_ssd_max_usage_gb: Optional[int] = Field(
        default=None,
        description="Soft cap on scratch usage in GB (passed to run_nipt.sh --ssd-max-usage-gb)",
    )
    # Model / reference overrides for gx-FF and gx-cnv.
    # Both are DEFERRED: the training/reference-building pipelines in
    # genolyx/gx-FF and genolyx/gx-cnv have not produced artefacts yet.
    # Leave these unset and the gx-nipt pipeline will fall back to:
    #   - seqFF only (no LightGBM+DNN ensemble) for FF estimation
    #   - WisecondorX only (no hybrid dual-track) for CNV calling
    nipt_gxff_model: Optional[str] = Field(default=None)
    nipt_gxcnv_reference: Optional[str] = Field(default=None)
    # Alias for nipt_gxcnv_reference — some specs refer to the file as
    # the "gx-cnv model". Either env var will be forwarded as
    # --gxcnv-reference to the wrapper.
    nipt_gxcnv_model: Optional[str] = Field(default=None)
    nipt_run_wcx: bool = Field(default=True, description="Run WisecondorX inside gx-nipt")
    nipt_run_wc: bool = Field(default=True, description="Run legacy Wisecondor (WC) inside gx-nipt")
    # Age fallback (Platform DOB 없을 때)
    nipt_default_age: Optional[int] = Field(
        default=None,
        description="Fallback age when Platform API does not return patientBirth (e.g. 30)",
    )
    # Pipeline resource knobs — passed through run_nipt.sh → Nextflow
    nipt_max_cpus: Optional[int] = Field(default=None, description="Nextflow --max_cpus")
    nipt_samtools_threads: Optional[int] = Field(default=None, description="samtools thread count")
    nipt_samtools_memory: Optional[str] = Field(default=None, description="e.g. 6G")
    nipt_picard_memory: Optional[str] = Field(default=None, description="e.g. 12G")
    # gxcnv knobs
    nipt_run_gxcnv: bool = Field(default=True, description="Legacy gx-cnv on/off (--no-gxcnv)")
    nipt_run_gxcnv1: Optional[bool] = Field(default=None, description="gxcnv1 on/off")
    nipt_run_gxcnv2: Optional[bool] = Field(default=None, description="gxcnv2 on/off")
    nipt_report_engine: str = Field(
        default="pptx",
        description="NIPT report engine: 'pptx' (legacy PPTX→PDF) or 'html' (Jinja2 HTML→WeasyPrint PDF)",
    )
    nipt_report_template_dir: str = Field(default="/home/ken/gx-daemon/data/templates", alias="REPORT_TEMPLATE_DIR")
    nipt_report_html_template_dir: str = Field(
        default="/app/data/GX_Report_html",
        description="Directory containing GX_Report_Template.html and assets for HTML engine",
    )
    nipt_report_sign_dir: str = Field(default="/home/ken/gx-daemon/data/signature", alias="REPORT_SIGNATURE_DIR")
    remove_bam: bool = Field(default=False, description="Remove BAM files after analysis")
    skip_platform_notifications: bool = Field(default=False, description="Skip platform notifications (test)")

    # ─── sgNIPT (kept for plugin compatibility) ────────────
    sgnipt_fastq_dir: str = Field(default="/home/ken/sgNIPT/fastq")
    sgnipt_data_dir: str = Field(default="/home/ken/sgNIPT/data")
    sgnipt_config_dir: str = Field(default="/home/ken/sgNIPT/config")
    sgnipt_layout_root: str = Field(default="/home/ken/sgNIPT")
    sgnipt_work_root: Optional[str] = Field(default=None)
    sgnipt_docker_image: str = Field(default="sgnipt")
    sgnipt_run_script_path: str = Field(default="/home/ken/sgNIPT/src/run_sgnipt.sh")
    sgnipt_src_root: Optional[str] = Field(default=None)
    sgnipt_container_mount_root: str = Field(default="/Work/SgNIPT")
    sgnipt_use_docker: bool = Field(default=True)
    sgnipt_docker_extra_args: str = Field(default="")
    # gx-nipt source checkout (must contain main.nf and bin/run_nipt.sh)
    nipt_pipeline_dir: str = Field(default="/home/ken/gx-nipt")
    sgnipt_pipeline_dir: str = Field(default="/opt/pipelines/sgnipt")

    @field_validator("gemini_api_key", "acmg_ai_api_key", mode="before")
    @classmethod
    def _strip_api_key_fields(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @property
    def resolved_orders_db_path(self) -> str:
        if self.orders_db_path and str(self.orders_db_path).strip():
            return os.path.abspath(str(self.orders_db_path).strip())
        return os.path.join(self.base_dir, "gx-daemon", "orders.db")

    @property
    def resolved_literature_db_path(self) -> str:
        if self.literature_db_path and str(self.literature_db_path).strip():
            return os.path.abspath(str(self.literature_db_path).strip())
        return os.path.join(self.base_dir, "gx-daemon", "literature.db")

    @property
    def sgnipt_job_root(self) -> str:
        wr = (self.sgnipt_work_root or "").strip()
        if wr:
            return os.path.abspath(wr)
        lr = (self.sgnipt_layout_root or "").strip() or "/home/ken/sgNIPT"
        return os.path.abspath(lr)

    @property
    def sgnipt_fastq_root(self) -> str:
        fq = (self.sgnipt_fastq_dir or "").strip()
        if fq:
            return os.path.abspath(fq)
        return os.path.abspath(os.path.join(self.sgnipt_job_root, "fastq"))

    @property
    def carrier_screening_layout_base(self) -> str:
        fq = (self.carrier_screening_fastq_dir or "").strip().rstrip("/")
        if fq.lower().endswith("/fastq"):
            return os.path.abspath(os.path.dirname(fq))
        return os.path.join(self.base_dir, "gx-exome")

    @property
    def carrier_screening_work_root(self) -> str:
        ab = (self.carrier_screening_artifact_base or "").strip()
        if ab:
            return os.path.abspath(ab)
        return self.carrier_screening_layout_base

    @property
    def report_temp_dir(self) -> str:
        """Backward-compat alias for nipt-daemon."""
        return self.nipt_report_template_dir

    @property
    def report_sign_dir(self) -> str:
        """Backward-compat alias for nipt-daemon."""
        return self.nipt_report_sign_dir

    @property
    def enabled_service_list(self) -> List[str]:
        return [s.strip() for s in self.enabled_services.split(",") if s.strip()]

    @property
    def report_language_list(self) -> List[str]:
        return [s.strip() for s in self.report_languages.split(",") if s.strip()]

    def get_carrier_screening_data_dir(self) -> str:
        return os.path.join(self.carrier_screening_pipeline_dir, "data")

    def get_carrier_screening_bed_dir(self) -> str:
        return os.path.join(self.carrier_screening_pipeline_dir, "data", "bed")

    @model_validator(mode="after")
    def _fill_gemini_api_key_from_genetic_reporter_lookup(self) -> "Settings":
        if (self.gemini_api_key or "").strip():
            return self
        path = (self.gemini_api_key_env_file or "").strip()
        if not path or not os.path.isfile(path):
            return self
        try:
            from dotenv import dotenv_values
            vals = dotenv_values(path)
            k = (vals.get("GEMINI_API_KEY") or "").strip()
            if k:
                return self.model_copy(update={"gemini_api_key": k})
        except Exception:
            pass
        return self

    @model_validator(mode="after")
    def _fill_gene_knowledge_db_from_genetic_reporter_lookup(self) -> "Settings":
        if (self.gene_knowledge_db or "").strip():
            return self
        path = (self.gene_knowledge_db_fallback_path or "").strip()
        if not path:
            return self
        abspath = os.path.abspath(path)
        parent = os.path.dirname(abspath)
        if not os.path.isdir(parent):
            return self
        return self.model_copy(update={"gene_knowledge_db": abspath})

    @field_validator(
        "carrier_screening_fastq_dir",
        "carrier_screening_run_script",
        "carrier_screening_script_data_dir",
        "carrier_screening_script_ref_dir",
        "carrier_screening_artifact_base",
        "carrier_screening_report_output_root",
        mode="before",
    )
    @classmethod
    def _normalize_legacy_carrier_paths(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, str):
            return v
        return normalize_legacy_carrier_container_path(v)


settings = Settings()
