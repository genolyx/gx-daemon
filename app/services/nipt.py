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
        """Daemon restart: done only when success marker (or json+tar) exists and not failed."""
        oid = (job.order_id or "").strip()
        out = job.output_dir or ""
        failed = os.path.join(out, f"{oid}.failed")
        if os.path.isfile(failed):
            return False
        completed = os.path.join(out, f"{oid}.completed")
        path = self._order_result_json(job)
        tar = self._order_output_tar(job)
        if os.path.isfile(completed) and os.path.isfile(path):
            logger.info("[nipt] sync_is_complete: %s present → COMPLETED", completed)
            return True
        if os.path.isfile(path) and os.path.isfile(tar):
            logger.info(
                "[nipt] sync_is_complete: %s + tar present → COMPLETED", path
            )
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
            if os.path.islink(dst):
                try:
                    existing = os.path.realpath(dst)
                except OSError:
                    existing = ""
                if existing == src_abs or os.readlink(dst) == src_abs:
                    continue
                logger.info(
                    "[nipt] Replacing stale FASTQ symlink: %s (was %s → now %s)",
                    dst, existing or os.readlink(dst), src_abs,
                )
                os.unlink(dst)
            elif os.path.lexists(dst):
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

        # Patient age — required by run_nipt.sh; fallback to env default
        age = params.get("patient_age") or params.get("age")
        if age is None:
            age = getattr(settings, "nipt_default_age", None)
        if age is not None:
            parts.extend(["--age", str(age)])

        # Optional knobs driven by params or settings
        if params.get("from_bam"):
            parts.extend(["--from-bam", str(params["from_bam"])])
        if params.get("_algorithm_only") or params.get("algorithm_only"):
            parts.append("--algorithm-only")
        if params.get("_pipeline_fresh"):
            parts.append("--fresh")
        if params.get("_pipeline_no_resume") or settings.nipt_no_resume:
            parts.append("--no-resume")
        if params.get("_force"):
            parts.append("--force")

        # ── Resource knobs (params override settings) ──────────────
        max_cpus = params.get("max_cpus") or getattr(settings, "nipt_max_cpus", None)
        if max_cpus:
            parts.extend(["--max-cpus", str(max_cpus)])

        samtools_threads = params.get("samtools_threads") or getattr(settings, "nipt_samtools_threads", None)
        if samtools_threads:
            parts.extend(["--samtools-threads", str(samtools_threads)])

        samtools_memory = params.get("samtools_memory") or getattr(settings, "nipt_samtools_memory", None)
        if samtools_memory:
            parts.extend(["--samtools-memory", str(samtools_memory)])

        picard_memory = params.get("picard_memory") or getattr(settings, "nipt_picard_memory", None)
        if picard_memory:
            parts.extend(["--picard-memory", str(picard_memory)])

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

        # legacy gx-cnv on/off
        run_gxcnv = params.get("run_gxcnv")
        if run_gxcnv is None:
            run_gxcnv = getattr(settings, "nipt_run_gxcnv", True)
        if not run_gxcnv:
            parts.append("--no-gxcnv")

        # gxcnv1 / gxcnv2 (params override settings; None = let pipeline decide)
        run_gxcnv1 = params.get("run_gxcnv1")
        if run_gxcnv1 is None:
            run_gxcnv1 = getattr(settings, "nipt_run_gxcnv1", None)
        if run_gxcnv1 is True:
            parts.append("--run-gxcnv1")
        elif run_gxcnv1 is False:
            parts.append("--no-gxcnv1")

        run_gxcnv2 = params.get("run_gxcnv2")
        if run_gxcnv2 is None:
            run_gxcnv2 = getattr(settings, "nipt_run_gxcnv2", None)
        if run_gxcnv2 is True:
            parts.append("--run-gxcnv2")
        elif run_gxcnv2 is False:
            parts.append("--no-gxcnv2")

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

        # labcode 검증
        lab = ""
        for i, tok in enumerate(parts):
            if tok == "--labcode" and i + 1 < len(parts):
                lab = parts[i + 1]
        if not lab:
            raise RuntimeError(
                "nipt: labcode not set. Provide via Platform DTO or set NIPT_DEFAULT_LABCODE."
            )

        # age 검증 — run_nipt.sh에 --age 없으면 exit 1
        has_age = "--age" in parts
        if not has_age:
            raise RuntimeError(
                "nipt: --age not set. Platform API did not return patientBirth "
                "and NIPT_DEFAULT_AGE is not configured in .env."
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

    # ── QC post-processing helpers ───────────────────────────────────
    def _parse_qc_filter_txt(
        self, qc_filter_path: str
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Parse Output_QC/<order_id>.qc.filter.txt into sequencing_metrics.

        Returns ``(metrics, overall_status)`` where overall_status is the
        ``overall`` row's PASS/FAIL when present (else None).

        Field set matches gx-nipt ``generate_json_output`` sequencing_metrics
        (including mean_coverage / mean_mapping_quality).
        """
        metrics: Dict[str, Any] = {}
        overall: Optional[str] = None
        if not os.path.isfile(qc_filter_path):
            return metrics, overall

        # qc.filter key → (json_key, default_unit, default_threshold_label)
        field_map = {
            "number_of_reads": ("total_reads", "reads", ">10M"),
            "number_of_mapped_reads": ("mapped_reads", "reads", ""),
            "mapping_rate": ("mapping_rate", "%", ">85%"),
            "number_of_duplicated_reads": ("duplicated_reads", "reads", ""),
            "duplication_rate": ("duplication_rate", "%", "<40%"),
            "gc_content": ("gc_content", "%", "33~55%"),
            "GC_content": ("gc_content", "%", "33~55%"),
            "mean_mapping_quality": ("mean_mapping_quality", "score", ">20"),
            "mean_coverage": ("mean_coverage", "X", ">0.1X"),
            "mean_coverageData": ("mean_coverage", "X", ">0.1X"),
        }
        try:
            with open(qc_filter_path, encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("\t")
                    if len(parts) < 2:
                        continue
                    key = parts[0].strip()
                    if key == "sample_id":
                        continue
                    if key == "overall":
                        overall = parts[-1].strip().upper() or None
                        continue
                    if key not in field_map:
                        continue
                    out_key, unit, default_threshold = field_map[key]
                    try:
                        raw_val = parts[1].strip().replace("%", "").replace("X", "").replace("x", "")
                        value = float(raw_val)
                    except (ValueError, IndexError):
                        continue
                    # Format: metric value [threshold comparator] status
                    if len(parts) >= 5:
                        threshold = parts[2].strip() or default_threshold
                        status = parts[4].strip() or "UNKNOWN"
                    elif len(parts) >= 4:
                        threshold = parts[2].strip() or default_threshold
                        status = parts[3].strip() or "UNKNOWN"
                    else:
                        threshold = default_threshold
                        status = parts[-1].strip() if len(parts) >= 3 else "UNKNOWN"
                    metrics[out_key] = {
                        "value": value,
                        "status": status,
                        "unit": unit,
                        "threshold": threshold,
                    }
        except OSError as e:
            logger.warning("[nipt] Could not parse qc.filter.txt: %s", e)
        return metrics, overall

    def _build_analysis_qc(self, final_results: Dict[str, Any]) -> Dict[str, Any]:
        """Build analysis_qc section from final_results (pipeline-parity keys)."""
        aqc: Dict[str, Any] = {}

        ff_yff = final_results.get("fetal_fraction_yff")
        aqc["fetal_fraction_yff"] = {
            "value": ff_yff,
            "unit": "%",
            "status": "PASS" if ff_yff not in (None, "N/A", "NA") else "N/A",
            "threshold": ">4.0%",
        }

        ff_seqff = final_results.get("fetal_fraction_seqff")
        try:
            ff_seqff_f = float(ff_seqff) if ff_seqff not in (None, "NA", "N/A") else None
        except (TypeError, ValueError):
            ff_seqff_f = None
        aqc["fetal_fraction_seqff"] = {
            "value": ff_seqff_f if ff_seqff_f is not None else ff_seqff,
            "unit": "%",
            "status": "PASS" if ff_seqff_f is not None and ff_seqff_f >= 4.0 else "FAIL" if ff_seqff_f is not None else "N/A",
            "threshold": ">4.0%",
        }

        ff_ratio = final_results.get("ff_ratio")
        try:
            ff_ratio_f = float(ff_ratio) if ff_ratio not in (None, "NA", "N/A") else None
        except (TypeError, ValueError):
            ff_ratio_f = None
        if ff_ratio_f is not None:
            aqc["ff_ratio"] = {
                "value": ff_ratio_f,
                "unit": "",
                "status": "PASS" if ff_ratio_f < 2.5 else "FAIL",
                "threshold": "<2.5",
            }

        qc_result = final_results.get("QC_result", "")
        aqc["overall_qc"] = {
            "value": qc_result,
            "status": qc_result if qc_result else "UNKNOWN",
        }

        return aqc

    @staticmethod
    def _apply_qc_fail(
        data: Dict[str, Any],
        nipt: Dict[str, Any],
        final_results: Dict[str, Any],
        reason: str,
    ) -> None:
        """Set QC_result=FAIL and No-call review comments (pipeline parity)."""
        final_results["QC_result"] = "FAIL"
        nipt["final_results"] = final_results
        review = nipt.setdefault("review", {})
        for who in ("reviewer1", "reviewer2"):
            block = review.setdefault(who, {})
            block["Trisomy_result"] = "No call"
            block["MD_result"] = "No call"
            prev_t = (block.get("Trisomy_comment") or "").strip()
            prev_m = (block.get("MD_comment") or "").strip()
            if reason and reason not in prev_t:
                block["Trisomy_comment"] = f"{prev_t}, {reason}".strip(", ") if prev_t else reason
            elif not prev_t:
                block["Trisomy_comment"] = reason
            if reason and reason not in prev_m:
                block["MD_comment"] = f"{prev_m}, {reason}".strip(", ") if prev_m else reason
            elif not prev_m:
                block["MD_comment"] = reason
        data["NIPT"] = nipt

    def _enrich_result_json(self, json_path: str, order_id: str) -> None:
        """Post-process the pipeline JSON to fill empty QC fields.

        Safety net when sequencing_metrics / analysis_qc were left empty:
        refill from qc.filter.txt (full field set) and sync QC_result from
        the filter ``overall`` row so Portal does not keep a false PASS.
        """
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[nipt] Could not read result JSON for enrichment: %s", e)
            return

        nipt = data.get("NIPT", {})
        qc = nipt.get("quality_control", {})
        final_results = nipt.get("final_results", {})

        changed = False
        overall_status: Optional[str] = None

        # 1. Fill sequencing_metrics from qc.filter.txt if empty
        if not qc.get("sequencing_metrics"):
            output_dir = os.path.dirname(json_path)
            qc_filter = os.path.join(output_dir, "Output_QC", f"{order_id}.qc.filter.txt")
            metrics, overall_status = self._parse_qc_filter_txt(qc_filter)
            if metrics:
                qc["sequencing_metrics"] = metrics
                logger.info(
                    "[nipt] Filled sequencing_metrics (%d fields) from qc.filter.txt",
                    len(metrics),
                )
                changed = True

        # 2. Fill analysis_qc from final_results if empty
        if not qc.get("analysis_qc") and final_results:
            aqc = self._build_analysis_qc(final_results)
            if aqc:
                qc["analysis_qc"] = aqc
                logger.info("[nipt] Filled analysis_qc (%d fields) from final_results", len(aqc))
                changed = True

        # 3. Sync QC_result when filter overall is FAIL but JSON still PASS/empty
        current_qc = str(final_results.get("QC_result") or "").strip().upper()
        if overall_status is None and qc.get("sequencing_metrics"):
            # Re-read overall if metrics were already present but we still need sync
            output_dir = os.path.dirname(json_path)
            qc_filter = os.path.join(output_dir, "Output_QC", f"{order_id}.qc.filter.txt")
            _, overall_status = self._parse_qc_filter_txt(qc_filter)

        if overall_status == "FAIL" and current_qc in ("", "PASS", "UNKNOWN"):
            self._apply_qc_fail(
                data, nipt, final_results,
                "sequencing QC overall FAIL (from qc.filter.txt)",
            )
            # refresh overall_qc display if we just built analysis_qc
            aqc = qc.get("analysis_qc") or {}
            if aqc:
                aqc["overall_qc"] = {"value": "FAIL", "status": "FAIL"}
                qc["analysis_qc"] = aqc
            changed = True
            logger.info("[nipt] Synced QC_result=FAIL from qc.filter overall")

        if changed:
            nipt["quality_control"] = qc
            nipt["final_results"] = final_results
            data["NIPT"] = nipt
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("[nipt] Result JSON enriched: %s", json_path)

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

            # Post-process: fill empty QC fields from pipeline artifacts
            order_id = (job.order_id or "").strip()
            self._enrich_result_json(src, order_id)

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
