"""
sgNIPT Service Plugin

Docker 이미지 `sgnipt`에서 run_sgnipt.sh 를 실행합니다.
호스트 레이아웃: {job_root}/fastq|analysis|log|output/<work_dir>/<order_id>/
(job_root = SGNIPT_WORK_ROOT; FASTQ 는 레이아웃 sgNIPT/fastq 와 동일 트리 — compose 가
sgnipt_work/fastq 에 레이아웃 fastq 를 마운트. run_sgnipt.sh 는 기본 포그라운드 docker run
— carrier run_analysis.sh 와 동일하게 쉘이 파이프라인 종료까지 블록함)
완료 판정: output/.../<order_id>/<order_id>.json (generate_result_json.py)
Portal 연동: 동일 JSON을 output/.../result.json 으로 복사합니다.
"""

import csv
import os
import glob
import json
import logging
import re
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import ServicePlugin
from app.config import settings
from app.models import Job, OutputFile

logger = logging.getLogger(__name__)


def sgnipt_run_container_name(order_id: str) -> str:
    """``run_sgnipt.sh`` CONTAINER_NAME 와 동일 (order_id 경로는 원본 유지)."""
    raw = (order_id or "").strip()
    name = re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)
    name = re.sub(r"^[-_.]*", "", name)
    if not name:
        name = "sgnipt-unknown"
    return name[:200]


def remove_sgnipt_run_container_for_job(job: Job) -> bool:
    if job.service_code != "sgnipt":
        return False
    if not shutil.which("docker") or not os.path.exists("/var/run/docker.sock"):
        return False
    name = sgnipt_run_container_name(job.order_id or "")
    try:
        listed = subprocess.run(
            ["docker", "ps", "-aq", "-f", f"name=^{name}$"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if not (listed.stdout or "").strip():
            return False
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        logger.info("[sgnipt] Removed stale run container: %s", name)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[sgnipt] Failed to remove run container %s: %s", name, e)
        return False


def apply_sgnipt_layout_directories(job: Job) -> bool:
    """
    Job의 fastq_dir / analysis_dir / output_dir / log_dir 를 현재 설정 기준으로 갱신합니다.
    DB에 저장된 구 경로를 현재 SGNIPT_WORK_ROOT 로 교체할 때 사용.
    변경이 있었으면 True 반환.
    """
    if job.service_code != "sgnipt":
        return False
    root = settings.sgnipt_job_root
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
            "[sgnipt] Normalized paths for %s (work=%s root=%s)",
            job.order_id, wk, root,
        )
        job.fastq_dir = new_f
        job.analysis_dir = new_a
        job.output_dir = new_o
        job.log_dir = new_l
    return changed


def sgnipt_host_path_candidates(path: str) -> List[str]:
    """
    Map paths between pipeline container mount (/Work/SgNIPT), layout host tree,
    and SGNIPT_WORK_ROOT so the daemon can see BAMs and samplesheets.
    """
    raw = (path or "").strip()
    if not raw:
        return []
    out: List[str] = []
    seen: Set[str] = set()

    def add(p: str) -> None:
        if not p:
            return
        try:
            ap = os.path.abspath(p)
        except OSError:
            return
        if ap in seen:
            return
        seen.add(ap)
        out.append(ap)

    add(raw)
    layout = (settings.sgnipt_layout_root or "").strip()
    work = (settings.sgnipt_work_root or "").strip()
    mount = (settings.sgnipt_container_mount_root or "").strip()
    remap: List[Tuple[str, str]] = []
    if mount and layout:
        remap.append((os.path.abspath(mount), os.path.abspath(layout)))
    if layout and work:
        la, wa = os.path.abspath(layout), os.path.abspath(work)
        remap.append((la, wa))
        remap.append((wa, la))
    if mount and work:
        remap.append((os.path.abspath(mount), os.path.abspath(work)))
    for src_base, dst_base in remap:
        try:
            if os.path.commonpath([src_base, os.path.abspath(raw)]) == src_base:
                add(dst_base + raw[len(src_base) :])
        except ValueError:
            continue
    return out


def sgnipt_resolve_visible_path(path: str) -> Optional[str]:
    for candidate in sgnipt_host_path_candidates(path):
        if os.path.isfile(candidate) or os.path.isdir(candidate):
            return candidate
    return None


def sgnipt_artifact_roots(job: Job) -> List[str]:
    """Directories to search for sgNIPT BAMs (FASTQ runs, BAM-input runs, layout↔work)."""
    if (job.service_code or "").strip() != "sgnipt":
        return []
    roots: List[str] = []
    seen: Set[str] = set()

    def add_dir(path: Optional[str]) -> None:
        if not path or not str(path).strip():
            return
        for candidate in sgnipt_host_path_candidates(str(path).strip()):
            if not os.path.isdir(candidate):
                continue
            try:
                key = os.path.realpath(candidate)
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            roots.append(candidate)

    for attr in ("analysis_dir", "output_dir", "fastq_dir"):
        add_dir(getattr(job, attr, None))

    wk = str(job.work_dir or "").strip() or "00"
    oid = str(job.order_id or "").strip()
    for base in (
        settings.sgnipt_job_root,
        (settings.sgnipt_layout_root or "").strip() or None,
    ):
        if not base:
            continue
        for sub in ("analysis", "output"):
            add_dir(os.path.join(base, sub, wk, oid))

    data_dir = (settings.sgnipt_data_dir or "").strip()
    if data_dir:
        add_dir(data_dir)

    csv_path = (job.params or {}).get("input_bam_csv", "")
    if isinstance(csv_path, str) and csv_path.strip():
        csv_vis = sgnipt_resolve_visible_path(csv_path.strip())
        if csv_vis and os.path.isfile(csv_vis):
            try:
                with open(csv_vis, newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        for col in ("bam", "bai"):
                            cell = (row.get(col) or "").strip()
                            for host_path in sgnipt_host_path_candidates(cell):
                                parent = os.path.dirname(host_path)
                                if parent:
                                    add_dir(parent)
            except OSError as e:
                logger.debug("[sgnipt] samplesheet scan for artifact roots: %s", e)
    return roots


def sgnipt_collect_bam_abs_paths(job: Job) -> List[str]:
    """Absolute paths to BAMs listed in input_bam_csv (BAM simulation / external BAM mode)."""
    if (job.service_code or "").strip() != "sgnipt":
        return []
    csv_path = (job.params or {}).get("input_bam_csv", "")
    if not isinstance(csv_path, str) or not csv_path.strip():
        return []
    csv_vis = sgnipt_resolve_visible_path(csv_path.strip())
    if not csv_vis or not os.path.isfile(csv_vis):
        return []
    sample = (job.sample_name or job.order_id or "").strip()
    found: List[str] = []
    seen_bam: Set[str] = set()
    try:
        with open(csv_vis, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sid = (row.get("sample_id") or "").strip()
                if sample and sid and sid != sample:
                    continue
                bam_cell = (row.get("bam") or "").strip()
                for host_path in sgnipt_host_path_candidates(bam_cell):
                    if not host_path.lower().endswith(".bam"):
                        continue
                    if not os.path.isfile(host_path):
                        continue
                    try:
                        key = os.path.realpath(host_path)
                    except OSError:
                        continue
                    if key in seen_bam:
                        continue
                    seen_bam.add(key)
                    found.append(host_path)
    except OSError as e:
        logger.warning("[sgnipt] Could not read BAM samplesheet %s: %s", csv_vis, e)
    return found


def _sgnipt_qc_blob_has_values(blob: Optional[dict], keys: Tuple[str, ...]) -> bool:
    if not isinstance(blob, dict):
        return False
    return any(blob.get(k) is not None for k in keys)


def _parse_sgnipt_bam_qc_blob(data: dict) -> dict:
    legacy = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    flagstat = data.get("flagstat_metrics") if isinstance(data.get("flagstat_metrics"), dict) else {}
    stats = data.get("stats_metrics") if isinstance(data.get("stats_metrics"), dict) else {}
    target = data.get("target_metrics") if isinstance(data.get("target_metrics"), dict) else {}

    def pick(*keys: str):
        for key in keys:
            for src in (legacy, flagstat, stats, target):
                if key in src and src[key] is not None:
                    return src[key]
        return None

    dup = pick("duplicate_rate", "dup_rate")
    return {
        "total_reads": pick("total_reads", "raw_total_sequences"),
        "mapped_reads": pick("mapped_reads", "reads_mapped"),
        "mapping_rate": pick("mapping_rate"),
        "mean_coverage": pick("mean_coverage", "mean_target_coverage", "mean_genome_coverage"),
        "target_coverage": pick("target_mean_coverage", "mean_target_coverage"),
        "on_target_rate": pick("on_target_rate"),
        "dup_rate": dup,
        "duplicate_rate": dup,
        "mean_mapping_quality": pick("mean_mapping_quality", "average_quality"),
        "overall_pass": (data.get("qc_evaluation") or {}).get("overall_qc_pass", True),
    }


def _find_sgnipt_analysis_qc_json(analysis_dir: Optional[str], sample_id: str, stem: str) -> Optional[str]:
    if not analysis_dir or not sample_id:
        return None
    candidates = [
        os.path.join(analysis_dir, "qc", f"{sample_id}.{stem}.json"),
        os.path.join(analysis_dir, sample_id, "qc", f"{sample_id}.{stem}.json"),
    ]
    for path in candidates:
        vis = sgnipt_resolve_visible_path(path)
        if vis and os.path.isfile(vis):
            return vis
    return None


def _enrich_sgnipt_sample_qc(sample: dict, analysis_dir: Optional[str], sample_id: str) -> dict:
    """Backfill null QC fields from analysis/qc/*.json (BAM-input runs)."""
    out = dict(sample or {})
    bam_keys = (
        "total_reads", "mapped_reads", "mapping_rate", "mean_coverage",
        "target_coverage", "on_target_rate", "dup_rate", "mean_mapping_quality",
    )
    bam_qc = out.get("bam_qc") if isinstance(out.get("bam_qc"), dict) else {}
    if not _sgnipt_qc_blob_has_values(bam_qc, bam_keys):
        path = _find_sgnipt_analysis_qc_json(analysis_dir, sample_id, "bam_qc")
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or {}
                parsed = _parse_sgnipt_bam_qc_blob(raw)
                merged = dict(bam_qc)
                for k, v in parsed.items():
                    if merged.get(k) is None and v is not None:
                        merged[k] = v
                out["bam_qc"] = merged
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("[sgnipt] Could not enrich bam_qc from %s: %s", path, e)
    return out


def _normalize_ff(ff: dict | None) -> dict | None:
    """Normalize fetal_fraction dict: map pipeline key primary_ff → primary_fetal_fraction."""
    if not isinstance(ff, dict):
        return ff
    if "primary_fetal_fraction" not in ff and "primary_ff" in ff:
        ff = dict(ff)
        ff["primary_fetal_fraction"] = ff["primary_ff"]
    return ff


class SgNIPTPlugin(ServicePlugin):
    """sgNIPT 파이프라인 (Docker 또는 호스트 bash)."""

    @property
    def service_code(self) -> str:
        return "sgnipt"

    @property
    def display_name(self) -> str:
        return "Single-gene NIPT"

    def validate_params(self, params: Dict[str, Any], strict: bool = True) -> Tuple[bool, str]:
        return True, ""

    def sync_is_complete(self, job: Job) -> bool:
        """
        daemon 재시작 복구 시 파이프라인이 실제로 완료됐는지 동기적으로 확인.
        output_dir/<order_id>.json 파이프라인 결과 파일이 존재하면 완료로 판정.
        """
        path = self._order_result_json(job)
        if os.path.isfile(path):
            logger.info(
                "[sgnipt] sync_is_complete: found %s → marking COMPLETED", path
            )
            return True
        return False

    def _run_script_candidates(self) -> List[str]:
        """
        존재 여부와 무관한 후보 목록 (오류 메시지·탐색용).
        Docker 에서 소스(/home/ken/sgnipt) 와 데이터 트리(/home/ken/sgNIPT) 가 갈라질 수 있음.
        """
        candidates: List[str] = []
        configured = (settings.sgnipt_run_script_path or "").strip()
        if configured:
            candidates.append(os.path.abspath(configured))
        src_root = (settings.sgnipt_src_root or "").strip()
        if src_root:
            candidates.append(
                os.path.abspath(os.path.join(src_root, "src", "run_sgnipt.sh"))
            )
        root = (settings.sgnipt_job_root or "").strip()
        if root:
            candidates.append(os.path.abspath(os.path.join(root, "src", "run_sgnipt.sh")))
        layout = (settings.sgnipt_layout_root or "").strip()
        if layout:
            candidates.append(os.path.abspath(os.path.join(layout, "src", "run_sgnipt.sh")))
        seen: Set[str] = set()
        ordered: List[str] = []
        for c in candidates:
            if not c or c in seen:
                continue
            seen.add(c)
            ordered.append(c)
        return ordered

    def _resolve_run_script(self) -> str:
        """
        SGNIPT_RUN_SCRIPT_PATH 가 틀리거나(대소문자 경로 등) 없을 때 job / layout / SGNIPT_SRC_ROOT 로 보완.
        """
        configured = (settings.sgnipt_run_script_path or "").strip()
        for c in self._run_script_candidates():
            if os.path.isfile(c):
                if configured and os.path.abspath(configured) != c:
                    logger.warning(
                        "[sgnipt] Using run script %s (configured path missing: %s)",
                        c,
                        configured,
                    )
                return c
        cand = self._run_script_candidates()
        return cand[0] if cand else ""

    def get_pipeline_cwd(self, job: Job) -> Optional[str]:
        """
        수동 실행과 동일하게 **소스 저장소 루트**에서 ``bash src/run_sgnipt.sh ...`` 하도록 cwd 고정.
        SGNIPT_WORK_ROOT 만 쓰면 data 트리에 src/ 가 없어 상대 경로가 깨질 수 있음.
        """
        script = self._resolve_run_script()
        if script and os.path.isfile(script):
            repo_root = os.path.abspath(os.path.join(os.path.dirname(script), ".."))
            if os.path.isdir(os.path.join(repo_root, "src")):
                return repo_root
        root = (settings.sgnipt_job_root or "").strip()
        if root and os.path.isdir(root):
            return os.path.abspath(root)
        return None

    def _order_result_json(self, job: Job) -> str:
        oid = (job.order_id or "").strip()
        return os.path.join(job.output_dir, f"{oid}.json")

    def _fastq_host_root(self) -> str:
        """run_sgnipt.sh 의 HOST_FASTQ_DIR (= SGNIPT_ROOT_DIR/fastq) 과 동일해야 함."""
        root = (settings.sgnipt_job_root or "").strip()
        return os.path.abspath(os.path.join(root, "fastq"))

    @staticmethod
    def _file_is_under_dir(dir_abs: str, path_abs: str) -> bool:
        dir_abs = os.path.abspath(dir_abs)
        path_abs = os.path.abspath(path_abs)
        try:
            return os.path.commonpath([dir_abs, path_abs]) == dir_abs
        except ValueError:
            return False

    def _resolve_input_bam_csv_path(self, path: str) -> str:
        """
        Portal often stores paths under sgnipt_layout_root (/home/ken/sgNIPT) while the
        daemon process (or bind mounts) only see sgnipt_work_root (/data/sgnipt_work).
        Map layout → work when the same relative path exists there.
        """
        p = os.path.abspath(path.strip())
        if os.path.isfile(p):
            return p
        layout = (settings.sgnipt_layout_root or "").strip()
        work = (settings.sgnipt_work_root or "").strip()
        mapped = ""
        if layout and work:
            la = os.path.abspath(layout)
            wa = os.path.abspath(work)
            if p.startswith(la + os.sep):
                mapped = wa + p[len(la) :]
                if os.path.isfile(mapped):
                    logger.info(
                        "[sgnipt] Resolved BAM CSV (layout→work root): %s → %s",
                        p,
                        mapped,
                    )
                    return mapped
        extra = f"\n  Tried layout→work: {mapped!r}" if mapped else ""
        raise RuntimeError(
            "sgnipt: BAM samplesheet CSV not found at path visible to daemon: "
            f"{path}\n  Resolved as: {p}{extra}"
        )

    def _run_sgnipt_fastq_rel_paths(self, job: Job) -> Tuple[Optional[str], Optional[str]]:
        """
        run_sgnipt / inner Docker 가 기대하는 FASTQ 경로: HOST_FASTQ_DIR 기준 상대 경로.
        """
        r1 = (job.fastq_r1_path or "").strip()
        r2 = (job.fastq_r2_path or "").strip()
        if not r1 or not r2:
            return None, None
        fastq_host = self._fastq_host_root()
        dest1 = os.path.abspath(os.path.join(job.fastq_dir or "", os.path.basename(r1)))
        dest2 = os.path.abspath(os.path.join(job.fastq_dir or "", os.path.basename(r2)))
        if not os.path.isfile(dest1) or not os.path.isfile(dest2):
            return None, None
        try:
            rel1 = os.path.relpath(dest1, fastq_host)
            rel2 = os.path.relpath(dest2, fastq_host)
        except ValueError:
            return None, None
        if rel1.startswith("..") or rel2.startswith(".."):
            return None, None
        return rel1.replace("\\", "/"), rel2.replace("\\", "/")

    async def prepare_inputs(self, job: Job) -> bool:
        for d in (job.fastq_dir, job.analysis_dir, job.output_dir, job.log_dir):
            if d:
                try:
                    os.makedirs(d, exist_ok=True)
                except OSError as e:
                    raise RuntimeError(
                        f"sgnipt: cannot create job directory {d}: {e}"
                    ) from e

        input_bam_csv = (job.params or {}).get("input_bam_csv", "").strip()
        if input_bam_csv:
            resolved = self._resolve_input_bam_csv_path(input_bam_csv)
            p = dict(job.params or {})
            if resolved != input_bam_csv:
                p["input_bam_csv"] = resolved
                job.params = p
            logger.info("[sgnipt] BAM simulation mode — using CSV: %s", resolved)
        else:
            r1 = (job.fastq_r1_path or "").strip()
            r2 = (job.fastq_r2_path or "").strip()
            fqdir = os.path.abspath(job.fastq_dir or "")
            if r1 and r2:
                for label, src in (("R1", r1), ("R2", r2)):
                    src_abs = os.path.abspath(src)
                    if not os.path.isfile(src_abs):
                        raise RuntimeError(
                            f"sgnipt: {label} FASTQ not found at path visible to daemon: {src_abs}"
                        )
                    if fqdir and self._file_is_under_dir(fqdir, src_abs):
                        continue
                    dst = os.path.join(job.fastq_dir or "", os.path.basename(src_abs))
                    if os.path.lexists(dst):
                        continue
                    try:
                        os.symlink(src_abs, dst)
                    except OSError as e:
                        raise RuntimeError(
                            f"sgnipt: could not symlink FASTQ into {dst}: {e}"
                        ) from e
            elif r1 or r2:
                raise RuntimeError(
                    "sgnipt: both fastq_r1_path and fastq_r2_path are required"
                )

        script = self._resolve_run_script()
        if not script or not os.path.isfile(script):
            tried = ", ".join(repr(p) for p in self._run_script_candidates())
            raise RuntimeError(
                "sgnipt: run_sgnipt.sh not found. For Docker, set SGNIPT_SRC_ROOT to the "
                f"source clone (same path as SGNIPT_SRC_HOST), or set SGNIPT_RUN_SCRIPT_PATH. Tried: {tried}"
            )

        logger.info("[sgnipt] Input prepared for %s (work_dir=%s)", job.order_id, job.work_dir)
        return True

    def _pipeline_command_parts(self, job: Job) -> List[str]:
        raw = self._resolve_run_script()
        script = os.path.abspath(raw) if raw else ""
        oid = (job.order_id or "").strip()
        wd = (job.work_dir or "").strip()
        params = job.params or {}

        input_bam_csv = params.get("input_bam_csv", "").strip()
        if input_bam_csv:
            # BAM simulation mode: pass CSV path directly to run_sgnipt.sh --input-bam
            parts: List[str] = [
                "bash", script,
                "--order-id", oid,
                "--work-id", wd,
                "--input-bam", input_bam_csv,
            ]
            logger.info("[sgnipt] BAM simulation command — input-bam=%s", input_bam_csv)
        else:
            # run_sgnipt.sh itself calls docker run (like carrier's run_analysis.sh).
            # cwd 는 get_pipeline_cwd → 저장소 루트(수동 실행과 동일).
            parts = ["bash", script, "--order_id", oid, "--work_dir", wd]
            fr1, fr2 = self._run_sgnipt_fastq_rel_paths(job)
            if fr1 and fr2:
                parts.extend(["--fastq_r1", fr1, "--fastq_r2", fr2])

        if params.get("_pipeline_fresh"):
            parts.append("--fresh")
        return parts

    @staticmethod
    def _run_sgnipt_shell_env(job: Job) -> str:
        """
        Host paths for run_sgnipt.sh nested ``docker run -v``.

        SGNIPT_ROOT_DIR may be /data/sgnipt_work inside gx-daemon; data/config/fastq
        must use layout host paths so the host Docker daemon bind-mounts the real trees.

        When job FASTQ lives outside SGNIPT_FASTQ_DIR (e.g. symlinks pointing to
        gx-exome/fastq), pass SGNIPT_FASTQ_EXTRA_VOLUME so the nested docker run can
        follow those symlinks — mirrors run_analysis.sh INPUT_BAM_MOUNT_ARGS pattern.
        """
        parts = [
            f"SGNIPT_ROOT_DIR={shlex.quote(settings.sgnipt_job_root)}",
            f"SGNIPT_DATA_DIR={shlex.quote(settings.sgnipt_data_dir)}",
            f"SGNIPT_CONFIG_DIR={shlex.quote(settings.sgnipt_config_dir)}",
            f"SGNIPT_FASTQ_DIR={shlex.quote(settings.sgnipt_fastq_dir)}",
        ]

        r1 = (job.fastq_r1_path or "").strip()
        if r1:
            r1_real = os.path.realpath(r1) if os.path.exists(r1) else r1
            r1_dir = os.path.dirname(os.path.abspath(r1_real))
            fq_host = os.path.abspath((settings.sgnipt_fastq_dir or "").strip().rstrip("/"))
            if fq_host and not r1_dir.startswith(fq_host):
                extra = f"{r1_dir}:{r1_dir}:ro"
                parts.append(f"SGNIPT_FASTQ_EXTRA_VOLUME={shlex.quote(extra)}")
                logger.info(
                    "[sgnipt] FASTQ outside SGNIPT_FASTQ_DIR — adding extra volume: %s", r1_dir
                )

        return " ".join(parts)

    async def get_pipeline_command(self, job: Job) -> str:
        remove_sgnipt_run_container_for_job(job)
        parts = self._pipeline_command_parts(job)
        cmd = shlex.join(parts)
        return f"{self._run_sgnipt_shell_env(job)} {cmd}"

    async def check_completion(self, job: Job) -> bool:
        path = self._order_result_json(job)
        if not os.path.isfile(path):
            logger.warning("[sgnipt] Completion file missing: %s", path)
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[sgnipt] Invalid or unreadable result JSON %s: %s", path, e)
            return False
        return True

    @staticmethod
    def _find_variant_report_json(analysis_dir: Optional[str], output_dir: Optional[str], sample_id: str) -> Optional[str]:
        """
        variant_report.json 위치를 탐색합니다.
        파이프라인 버전/레이아웃에 따라 경로가 다를 수 있으므로
        고정 경로를 먼저 시도하고, 없으면 glob으로 탐색 (work/ 제외).
        """
        fixed = [
            os.path.join(analysis_dir or "", sample_id, "variants", f"{sample_id}.variant_report.json"),
            os.path.join(analysis_dir or "", "variant", f"{sample_id}.variant_report.json"),
            os.path.join(analysis_dir or "", "variants", f"{sample_id}.variant_report.json"),
            os.path.join(output_dir or "", sample_id, "variants", f"{sample_id}.variant_report.json"),
        ]
        found = next((p for p in fixed if p and os.path.isfile(p)), None)
        if found:
            return found
        fname = f"{sample_id}.variant_report.json"
        for root in filter(None, [analysis_dir, output_dir]):
            hits = []
            for depth_pat in (fname, os.path.join("*", fname), os.path.join("*", "*", fname), os.path.join("*", "*", "*", fname)):
                hits.extend(
                    p for p in glob.glob(os.path.join(root, depth_pat))
                    if os.sep + "work" + os.sep not in p
                )
            if hits:
                return sorted(hits)[0]
        return None

    @staticmethod
    def _build_merged_result(summary: dict, vr: dict, analysis_dir: Optional[str] = None) -> dict:
        """
        파이프라인 요약 JSON(summary)과 variant_report JSON(vr)을 병합하여
        포털 Review에서 바로 사용할 수 있는 완성된 result.json 구조를 반환합니다.
        carrier_screening의 result.json과 동일한 단일-파일 구조가 목표입니다.
        """
        samples = summary.get("samples") or []
        sample0 = samples[0] if samples else {}
        sample_id = vr.get("sample_id") or sample0.get("sample_id") or ""
        sample0 = _enrich_sgnipt_sample_qc(sample0, analysis_dir, sample_id)
        return {
            **summary,
            # variant_report 상세 필드 (Review 핵심 데이터)
            "sample_id": sample_id,
            "panel": vr.get("panel"),
            "fetal_fraction_used": vr.get("fetal_fraction_used"),
            "summary": vr.get("summary"),
            "clinical_findings": vr.get("clinical_findings") or [],
            "all_target_variants": vr.get("all_target_variants") or [],
            "gene_coverage_validation": vr.get("gene_coverage_validation"),
            # QC: samples[0] 하위 필드를 최상위로 올림
            "sgnipt_status": summary.get("status"),
            "sgnipt_status_flags": sample0.get("status_flags") or [],
            "fastq_qc": sample0.get("fastq_qc"),
            "bam_qc": sample0.get("bam_qc"),
            "fetal_fraction_detail": _normalize_ff(sample0.get("fetal_fraction")),
            "variant_analysis_summary": sample0.get("variant_analysis"),
        }

    async def process_results(self, job: Job) -> bool:
        src = self._order_result_json(job)
        dst = os.path.join(job.output_dir, "result.json")
        try:
            if not os.path.isfile(src):
                logger.error("[sgnipt] Expected result not found: %s", src)
                return False

            with open(src, "r", encoding="utf-8") as f:
                summary: dict = json.load(f)

            samples = summary.get("samples") or []
            sample0 = samples[0] if samples else {}
            sample_id = sample0.get("sample_id") or (job.order_id or "").strip()

            vr_path = self._find_variant_report_json(job.analysis_dir, job.output_dir, sample_id)
            vr: dict = {}
            if vr_path:
                logger.info("[sgnipt] variant_report.json found at %s", vr_path)
                try:
                    with open(vr_path, "r", encoding="utf-8") as f:
                        vr = json.load(f) or {}
                except Exception as e:
                    logger.warning("[sgnipt] Could not read variant_report.json %s: %s", vr_path, e)
            else:
                logger.warning(
                    "[sgnipt] variant_report.json not found (sample_id=%s, analysis_dir=%s)",
                    sample_id, job.analysis_dir,
                )

            merged = self._build_merged_result(summary, vr, job.analysis_dir)
            text = json.dumps(merged, ensure_ascii=False, indent=2, default=str)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
            logger.info("[sgnipt] Merged result.json written to %s", dst)
            return True
        except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
            logger.error("[sgnipt] process_results failed: %s", e)
            return False

    async def get_output_files(self, job: Job) -> List[OutputFile]:
        out = job.output_dir
        files: List[OutputFile] = []
        oid = (job.order_id or "").strip()

        result_json = os.path.join(out, "result.json")
        if os.path.isfile(result_json):
            files.append(
                OutputFile(
                    file_path=result_json,
                    file_type="review_json",
                    file_name="result.json",
                    content_type="application/json",
                )
            )

        order_json = os.path.join(out, f"{oid}.json")
        if os.path.isfile(order_json) and os.path.abspath(order_json) != os.path.abspath(result_json):
            files.append(
                OutputFile(
                    file_path=order_json,
                    file_type="order_result_json",
                    file_name=f"{oid}.json",
                    content_type="application/json",
                )
            )

        tar_path = os.path.join(out, f"{oid}.output.tar")
        if os.path.isfile(tar_path):
            files.append(
                OutputFile(
                    file_path=tar_path,
                    file_type="output_tar",
                    file_name=f"{oid}.output.tar",
                    content_type="application/x-tar",
                )
            )

        mq = os.path.join(out, "multiqc", "multiqc_report.html")
        if os.path.isfile(mq):
            files.append(
                OutputFile(
                    file_path=mq,
                    file_type="multiqc_html",
                    file_name="multiqc_report.html",
                    content_type="text/html",
                )
            )

        for f in glob.glob(os.path.join(out, "**", "*_final_report.html"), recursive=True):
            if os.path.isfile(f):
                files.append(
                    OutputFile(
                        file_path=f,
                        file_type="final_report_html",
                        file_name=os.path.basename(f),
                        content_type="text/html",
                    )
                )

        logger.info("[sgnipt] Output files for upload: %s", len(files))
        return files

    async def generate_report(
        self,
        job: Job,
        confirmed_variants: list,
        reviewer_info: dict,
        patient_info: Optional[dict] = None,
        partner_info: Optional[dict] = None,
        languages: Optional[List[str]] = None,
        **kwargs,
    ) -> List[str]:
        """
        Generate sgNIPT report PDF(s) from reviewer-confirmed variants.

        1. Locate result.json for this order.
        2. Build report.json (generate_sgnipt_report_json).
        3. Render WeasyPrint PDF(s) per language (generate_sgnipt_report_pdf).
        4. Return list of generated file paths (report.json + PDFs).
        """
        import asyncio

        from .sgnipt_report import (
            generate_sgnipt_report_json,
            generate_sgnipt_report_pdf,
        )

        output_dir = job.output_dir or ""
        result_json_path = os.path.join(output_dir, "result.json")
        if not os.path.isfile(result_json_path):
            # Try order-level JSON
            oid = (job.order_id or "").strip()
            candidate = os.path.join(output_dir, f"{oid}.json")
            if os.path.isfile(candidate):
                result_json_path = candidate
            else:
                raise FileNotFoundError(
                    f"[sgnipt] result.json not found for order {job.order_id} "
                    f"(checked {output_dir})"
                )

        langs = languages or ["EN"]
        db_path = (getattr(settings, "gene_knowledge_db", None) or "").strip() or None
        gemini_key = (getattr(settings, "gemini_api_key", None) or "").strip() or None
        gemini_model = getattr(settings, "gene_knowledge_gemini_model", "gemini-2.5-flash")
        _clinvar_vcf = (getattr(settings, "clinvar_vcf", None) or "").strip() or None
        _gnomad_dir = (getattr(settings, "gnomad_dir", None) or "").strip() or None
        _gnomad_genomes_glob = getattr(settings, "gnomad_genomes_glob", "gnomad.genomes.v*.sites*.bgz")
        _gnomad_exomes_glob = getattr(settings, "gnomad_exomes_glob", "gnomad.exomes.v*.sites*.bgz")

        primary_lang = langs[0].upper() if langs else "EN"

        def _sync_generate() -> List[str]:
            report_json = generate_sgnipt_report_json(
                order_id=job.order_id,
                sample_name=job.sample_name or job.order_id,
                result_json_path=result_json_path,
                confirmed_variants=confirmed_variants or [],
                output_dir=output_dir,
                reviewer_info=reviewer_info or {},
                patient_info=patient_info or {},
                report_language=primary_lang,
                gene_knowledge_db=db_path,
                gemini_api_key=gemini_key,
                gemini_model=gemini_model,
                clinvar_vcf=_clinvar_vcf,
                gnomad_dir=_gnomad_dir,
                gnomad_genomes_glob=_gnomad_genomes_glob,
                gnomad_exomes_glob=_gnomad_exomes_glob,
            )
            pdf_files = generate_sgnipt_report_pdf(
                report_json_path=report_json,
                output_dir=output_dir,
                languages=langs,
            )
            result = [report_json] + pdf_files
            logger.info(
                "[sgnipt] generate_report complete for %s: %d file(s)", job.order_id, len(result)
            )
            return result

        return await asyncio.to_thread(_sync_generate)

    def get_progress_stages(self) -> Dict[str, int]:
        return {
            "FASTQ": 15,
            "ALIGN": 35,
            "VARIANT": 60,
            "REPORT": 85,
            "SUMMARY": 92,
        }


def create_plugin() -> ServicePlugin:
    return SgNIPTPlugin()
