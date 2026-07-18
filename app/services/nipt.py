"""
NIPT Service Plugin (gx-nipt / Nextflow)

Invokes gx-nipt's ``bin/run_nipt.sh`` wrapper, which in turn runs
``nextflow run main.nf``. The wrapper owns the on-disk layout
contract so the daemon just needs to provide order metadata.

Host layout (same shape as ken-nipt and sgnipt so gx-daemon's
existing output-pickup logic keeps working):

    {nipt_root_dir}/
        fastq/<work_dir>/<order_id>/R1.fastq.gz, R2.fastq.gz
        analysis/<work_dir>/<order_id>/...
        log/<work_dir>/<order_id>/pipeline.log
        output/<work_dir>/<order_id>/<order_id>.json
        output/<work_dir>/<order_id>/<order_id>.output.tar
        config/<labcode>/pipeline_config.json

Completion is declared when ``<output_dir>/<order_id>.json`` exists.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shlex
import shutil
from typing import Any, Dict, List, Optional, Tuple

from .base import ServicePlugin
from app.config import settings
from app.models import Job, OutputFile

logger = logging.getLogger(__name__)


def apply_nipt_layout_directories(job: Job) -> bool:
    """Normalise job.fastq_dir / analysis_dir / output_dir / log_dir to the
    current gx-nipt root layout. Called from queue_manager on enqueue /
    restore so DB rows keep tracking the correct paths.
    """
    if job.service_code != "nipt":
        return False
    root = (settings.nipt_root_dir or "").strip() or "/home/ken/gx-nipt-data"
    wk = str(job.work_dir or "").strip() or "00"
    oid = str(job.order_id or "").strip()
    new_f = os.path.join(root, "fastq", wk, oid)
    new_a = os.path.join(root, "analysis", wk, oid)
    new_o = os.path.join(root, "output", wk, oid)
    new_l = os.path.join(root, "log", wk, oid)
    changed = (
        job.fastq_dir != new_f
        or job.analysis_dir != new_a
        or job.output_dir != new_o
        or job.log_dir != new_l
    )
    if changed:
        logger.info(
            "[nipt] Normalized paths for %s (work=%s root=%s)",
            job.order_id, wk, root,
        )
        job.fastq_dir = new_f
        job.analysis_dir = new_a
        job.output_dir = new_o
        job.log_dir = new_l
    return changed


class NIPTPlugin(ServicePlugin):
    """gx-nipt (Nextflow) pipeline plugin."""

    # ── identity ────────────────────────────────────────────────
    @property
    def service_code(self) -> str:
        return "nipt"

    @property
    def display_name(self) -> str:
        return "NIPT (gx-nipt)"

    # ── helpers ─────────────────────────────────────────────────
    def _pipeline_dir(self) -> str:
        """Location of the gx-nipt repo (contains main.nf + bin/run_nipt.sh)."""
        configured = (getattr(settings, "nipt_pipeline_dir", "") or "").strip()
        if configured:
            return os.path.abspath(configured)
        # Fallback to historical location
        return "/home/ken/gx-nipt"

    def _run_script(self) -> str:
        override = (getattr(settings, "nipt_run_script", "") or "").strip()
        if override:
            return os.path.abspath(override)
        return os.path.join(self._pipeline_dir(), "bin", "run_nipt.sh")

    def _order_result_json(self, job: Job) -> str:
        """<output_dir>/<order_id>.json — daemon's completion signal."""
        oid = (job.order_id or "").strip()
        return os.path.join(job.output_dir or "", f"{oid}.json")

    def _order_output_tar(self, job: Job) -> str:
        oid = (job.order_id or "").strip()
        return os.path.join(job.output_dir or "", f"{oid}.output.tar")

    @staticmethod
    def _file_under(root: str, path: str) -> bool:
        r = os.path.abspath(root)
        p = os.path.abspath(path)
        try:
            return os.path.commonpath([r, p]) == r
        except ValueError:
            return False

    # ── lifecycle ───────────────────────────────────────────────
    def sync_is_complete(self, job: Job) -> bool:
        """Daemon restart: consider the job done if the final JSON exists."""
        path = self._order_result_json(job)
        if os.path.isfile(path):
            logger.info("[nipt] sync_is_complete: %s present → COMPLETED", path)
            return True
        return False

    def validate_params(self, params: Dict[str, Any], strict: bool = True) -> Tuple[bool, str]:
        return True, ""

    def get_pipeline_cwd(self, job: Job) -> Optional[str]:
        repo = self._pipeline_dir()
        if os.path.isdir(repo):
            return repo
        return None

    async def prepare_inputs(self, job: Job) -> bool:
        """Create the on-disk layout and make FASTQs visible under fastq_dir."""
        # 1) Enforce layout directories
        apply_nipt_layout_directories(job)
        for d in (job.fastq_dir, job.analysis_dir, job.output_dir, job.log_dir):
            if d:
                os.makedirs(d, exist_ok=True)

        # 2) Symlink FASTQs into the per-order fastq_dir (main.nf resolves
        #    params.fastq_r1/r2 relative to root_dir/fastq/work_dir/sample/)
        r1 = (job.fastq_r1_path or "").strip()
        r2 = (job.fastq_r2_path or "").strip()
        fqdir = job.fastq_dir or ""
        for label, src in (("R1", r1), ("R2", r2)):
            if not src:
                continue
            src_abs = os.path.abspath(src)
            if not os.path.isfile(src_abs):
                raise RuntimeError(
                    f"nipt: {label} FASTQ not found at path visible to daemon: {src_abs}"
                )
            if fqdir and self._file_under(fqdir, src_abs):
                continue
            dst = os.path.join(fqdir, os.path.basename(src_abs))
            if os.path.lexists(dst):
                continue
            try:
                os.symlink(src_abs, dst)
                logger.info("[nipt] Linked %s FASTQ %s -> %s", label, src_abs, dst)
            except OSError as e:
                raise RuntimeError(
                    f"nipt: could not symlink {label} FASTQ into {dst}: {e}"
                ) from e

        # 3) Make sure the wrapper is actually there
        script = self._run_script()
        if not os.path.isfile(script):
            raise RuntimeError(
                f"nipt: run_nipt.sh not found at {script} "
                f"(set NIPT_PIPELINE_DIR or NIPT_RUN_SCRIPT)"
            )
        if not os.access(script, os.X_OK):
            os.chmod(script, 0o755)

        logger.info(
            "[nipt] Input prepared for order=%s work_dir=%s root=%s",
            job.order_id, job.work_dir, settings.nipt_root_dir,
        )
        return True

    # ── command construction ────────────────────────────────────
    def _cli_parts(self, job: Job) -> List[str]:
        script = self._run_script()
        oid = (job.order_id or "").strip()
        wk = (job.work_dir or "").strip() or "00"
        params = job.params or {}
        root = settings.nipt_root_dir

        parts: List[str] = [
            "bash", script,
            "--order-id", oid,
            "--work-dir", wk,
            "--root-dir", root,
            "--labcode", (params.get("labcode") or settings.nipt_default_labcode or "").strip(),
        ]

        # Reference data root: forward to the wrapper so it can bind-mount
        # it into Docker and preflight required files.
        ref_dir = (
            params.get("ref_dir")
            or getattr(settings, "nipt_ref_dir", None)
            or ""
        ).strip()
        if ref_dir:
            parts.extend(["--ref-dir", ref_dir])

        # FASTQ names (main.nf expects bare filenames relative to fastq_dir)
        r1 = (job.fastq_r1_path or "").strip()
        r2 = (job.fastq_r2_path or "").strip()
        if r1 and r2:
            parts.extend([
                "--fastq-r1", os.path.basename(r1),
                "--fastq-r2", os.path.basename(r2),
            ])

        # Patient age comes from the Platform submit DTO
        age = params.get("patient_age")
        if age is None:
            age = params.get("age")
        if age is not None:
            parts.extend(["--age", str(age)])

        # Optional knobs driven by params or settings
        if params.get("from_bam"):
            parts.extend(["--from-bam", str(params["from_bam"])])
        if params.get("_algorithm_only") or params.get("algorithm_only"):
            parts.append("--algorithm-only")
        if params.get("_pipeline_fresh"):
            parts.append("--fresh")
        if params.get("_force"):
            parts.append("--force")

        # SSD scratch
        use_ssd = bool(getattr(settings, "nipt_use_ssd", False))
        if params.get("use_ssd") is True:
            use_ssd = True
        if params.get("use_ssd") is False:
            use_ssd = False
        if use_ssd:
            scratch_dir = (
                params.get("scratch_dir")
                or getattr(settings, "nipt_scratch_dir", None)
                or "/tmp/nipt_scratch"
            )
            max_gb = (
                params.get("ssd_max_usage_gb")
                or getattr(settings, "nipt_ssd_max_usage_gb", None)
                or 200
            )
            parts.extend([
                "--use-ssd",
                "--scratch-dir", str(scratch_dir),
                "--ssd-max-usage-gb", str(max_gb),
            ])

        # gx-FF / gx-cnv (both deferred: if the model/reference files are
        # not yet provisioned, leave the flags off and let the pipeline
        # fall back to seqFF-only FF and WisecondorX-only CNV).
        gxff_model = params.get("gxff_model") or getattr(settings, "nipt_gxff_model", None)
        if gxff_model:
            parts.extend(["--gxff-model", str(gxff_model)])
        gxcnv_ref = (
            params.get("gxcnv_reference")
            or params.get("gxcnv_model")          # alias matching user CLI docs
            or getattr(settings, "nipt_gxcnv_reference", None)
            or getattr(settings, "nipt_gxcnv_model", None)
        )
        if gxcnv_ref:
            parts.extend(["--gxcnv-reference", str(gxcnv_ref)])
        if params.get("run_wcx") is False or getattr(settings, "nipt_run_wcx", True) is False:
            parts.append("--no-wcx")
        if params.get("run_wc") is False or getattr(settings, "nipt_run_wc", True) is False:
            parts.append("--no-wc")

        # Nextflow binary override
        nf_bin = getattr(settings, "nextflow_executable", None)
        if nf_bin and nf_bin != "nextflow":
            parts.extend(["--nextflow", str(nf_bin)])

        return parts

    async def get_pipeline_command(self, job: Job) -> str:
        parts = self._cli_parts(job)
        # labcode validation (command-built above relies on it)
        lab = ""
        for i, tok in enumerate(parts):
            if tok == "--labcode" and i + 1 < len(parts):
                lab = parts[i + 1]
        if not lab:
            raise RuntimeError(
                "nipt: labcode not set. Provide via Platform DTO or set NIPT_DEFAULT_LABCODE."
            )
        return shlex.join(parts)

    # ── completion + post-processing ────────────────────────────
    async def check_completion(self, job: Job) -> bool:
        path = self._order_result_json(job)
        if not os.path.isfile(path):
            logger.warning("[nipt] Completion file missing: %s", path)
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[nipt] Invalid or unreadable result JSON %s: %s", path, e)
            return False
        return True

    async def process_results(self, job: Job) -> bool:
        """Copy the NIPT result into result.json so Portal review APIs work,
        and ensure the output tar exists.
        """
        src = self._order_result_json(job)
        dst = os.path.join(job.output_dir or "", "result.json")
        try:
            if not os.path.isfile(src):
                logger.error("[nipt] Expected result not found: %s", src)
                return False
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)
                logger.info("[nipt] Published result.json -> %s", dst)

            # Ensure output tar exists — run_nipt.sh builds it, but if the
            # daemon is replaying `process_results` after a partial failure
            # we rebuild from what's on disk.
            tar_path = self._order_output_tar(job)
            if not os.path.isfile(tar_path):
                logger.info("[nipt] %s missing; will be built by wrapper on next run", tar_path)
            return True
        except OSError as e:
            logger.error("[nipt] process_results failed: %s", e)
            return False

    async def get_output_files(self, job: Job) -> List[OutputFile]:
        out = job.output_dir or ""
        oid = (job.order_id or "").strip()
        files: List[OutputFile] = []

        result_json = self._order_result_json(job)
        if os.path.isfile(result_json):
            files.append(OutputFile(
                file_path=result_json,
                file_type="order_result_json",
                file_name=f"{oid}.json",
                content_type="application/json",
            ))

        review_json = os.path.join(out, "result.json")
        if os.path.isfile(review_json) and os.path.abspath(review_json) != os.path.abspath(result_json):
            files.append(OutputFile(
                file_path=review_json,
                file_type="review_json",
                file_name="result.json",
                content_type="application/json",
            ))

        tar_path = self._order_output_tar(job)
        if os.path.isfile(tar_path):
            files.append(OutputFile(
                file_path=tar_path,
                file_type="output_tar",
                file_name=f"{oid}.output.tar",
                content_type="application/x-tar",
            ))

        # Optional: HTML review page if present
        review_html = glob.glob(
            os.path.join(out, "Output_Result", "*.review.html")
        )
        for rh in review_html:
            files.append(OutputFile(
                file_path=rh,
                file_type="review_html",
                file_name=os.path.basename(rh),
                content_type="text/html",
            ))

        logger.info("[nipt] Output files for upload: %s", len(files))
        return files

    def get_progress_stages(self) -> Dict[str, int]:
        # Stage keywords emitted by run_nipt.sh progress() lines.
        return {
            "PREPARED":      5,
            "RUN":          10,
            "BWA":          35,
            "HMMCOPY":      55,
            "WCX":          70,
            "REPORT":       85,
            "PIPELINE_DONE": 90,
            "COMPLETED":    100,
        }


def create_plugin() -> ServicePlugin:
    return NIPTPlugin()
