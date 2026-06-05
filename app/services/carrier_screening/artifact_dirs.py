"""
Carrier screening artifact directory creation + permission repair.

Mirrors gx-exome ``run_analysis.sh`` ``ensure_sample_output_dirs`` /
``repair_order_tree_permissions`` so gx-daemon can open ``pipeline.log`` under
``CARRIER_SCREENING_ARTIFACT_BASE`` (e.g. /data/gx-exome-work) before invoking
run_analysis.sh (which runs the same repair later on --data-dir trees).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from ...models import Job
from .layout_norm import (
    _CARRIER_LIKE,
    carrier_run_analysis_work_arg,
    carrier_sequencing_folder,
    gx_exome_run_container_name,
)

logger = logging.getLogger(__name__)

_ARTIFACT_DIR_MODE = 0o2775


def carrier_chown_spec() -> str:
    return f"{os.getuid()}:{os.getgid()}"


def _docker_available() -> bool:
    return bool(shutil.which("docker")) and os.path.exists("/var/run/docker.sock")


def repair_carrier_order_tree_permissions(
    work_root: str, work_dir: str, *, quiet: bool = False
) -> bool:
    """
    chown uid:gid + g+rwX + setgid on ``work_root/{analysis,output,log}/{work_dir}``.
    """
    root = work_root.rstrip("/")
    if not any(
        os.path.isdir(os.path.join(root, base, work_dir))
        for base in ("analysis", "output", "log")
    ):
        return True

    spec = carrier_chown_spec()
    if _docker_available():
        script = (
            "for base in /fa /fo /fl; do "
            't="$base/$WORK_DIR"; [ -d "$t" ] || continue; '
            'chown -R "$CHOWN_SPEC" "$t"; '
            'chmod -R g+rwX "$t"; '
            'find "$t" -type d -exec chmod g+s {} + 2>/dev/null || true; '
            "done"
        )
        cmd = [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "-e",
            f"WORK_DIR={work_dir}",
            "-e",
            f"CHOWN_SPEC={spec}",
            "-v",
            f"{root}/analysis:/fa",
            "-v",
            f"{root}/output:/fo",
            "-v",
            f"{root}/log:/fl",
            "alpine:3.20",
            "sh",
            "-c",
            script,
        ]
        try:
            subprocess.run(
                cmd, check=True, capture_output=True, text=True, timeout=120
            )
            if not quiet:
                logger.info(
                    "[carrier_screening] Repaired order tree permissions for %s (%s)",
                    work_dir,
                    spec,
                )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(
                "[carrier_screening] docker order-tree permission repair failed: %s",
                e,
            )

    for base in ("analysis", "output", "log"):
        tree = os.path.join(root, base, work_dir)
        if not os.path.isdir(tree):
            continue
        try:
            os.chmod(tree, _ARTIFACT_DIR_MODE)
        except OSError:
            pass
    return False


def _sample_artifact_paths(work_root: str, work_dir: str, sample_name: str) -> tuple[str, str, str, str]:
    root = work_root.rstrip("/")
    analysis = os.path.join(root, "analysis", work_dir, sample_name)
    output = os.path.join(root, "output", work_dir, sample_name)
    log_dir = os.path.join(root, "log", work_dir, sample_name)
    return analysis, output, log_dir, os.path.join(analysis, ".nextflow")


def _mkdir_sample_tree(work_root: str, work_dir: str, sample_name: str) -> bool:
    analysis, output, log_dir, nf_dir = _sample_artifact_paths(
        work_root, work_dir, sample_name
    )
    try:
        os.makedirs(nf_dir, mode=_ARTIFACT_DIR_MODE, exist_ok=True)
        os.makedirs(output, mode=_ARTIFACT_DIR_MODE, exist_ok=True)
        os.makedirs(log_dir, mode=_ARTIFACT_DIR_MODE, exist_ok=True)
        repair_carrier_order_tree_permissions(work_root, work_dir, quiet=True)
        return os.access(log_dir, os.W_OK)
    except PermissionError:
        return False


def _docker_mkdir_sample_tree(work_root: str, work_dir: str, sample_name: str) -> None:
    root = work_root.rstrip("/")
    spec = carrier_chown_spec()
    script = (
        'mkdir -p "/fa/${WORK_DIR}/${SAMPLE_NAME}/.nextflow" '
        '"/fo/${WORK_DIR}/${SAMPLE_NAME}" "/fl/${WORK_DIR}/${SAMPLE_NAME}" && '
        "for base in /fa /fo /fl; do "
        't="$base/${WORK_DIR}"; [ -d "$t" ] || continue; '
        'chown -R "$CHOWN_SPEC" "$t"; '
        'chmod -R g+rwX "$t"; '
        'find "$t" -type d -exec chmod g+s {} + 2>/dev/null || true; '
        "done"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "-e",
            f"WORK_DIR={work_dir}",
            "-e",
            f"SAMPLE_NAME={sample_name}",
            "-e",
            f"CHOWN_SPEC={spec}",
            "-v",
            f"{root}/analysis:/fa",
            "-v",
            f"{root}/output:/fo",
            "-v",
            f"{root}/log:/fl",
            "alpine:3.20",
            "sh",
            "-c",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def ensure_carrier_sample_artifact_dirs(
    work_root: str, work_dir: str, sample_name: str
) -> None:
    """
    Create analysis/output/log sample dirs under ``work_root`` and ensure the daemon
    user can write ``pipeline.log`` there.
    """
    _, _, log_dir, _ = _sample_artifact_paths(work_root, work_dir, sample_name)
    if _mkdir_sample_tree(work_root, work_dir, sample_name):
        return

    if not _docker_available():
        raise PermissionError(
            f"Cannot create writable carrier artifact dirs (log={log_dir}). "
            f"On the host: chown -R {carrier_chown_spec()} "
            f"{work_root.rstrip('/')}/log/{work_dir} (and analysis/output siblings)."
        )

    logger.info(
        "[carrier_screening] Creating artifact dirs via docker (host mkdir failed): %s",
        log_dir,
    )
    _docker_mkdir_sample_tree(work_root, work_dir, sample_name)
    if not os.access(log_dir, os.W_OK):
        raise PermissionError(
            f"Carrier log dir still not writable after docker repair: {log_dir}"
        )


def clear_carrier_fresh_nextflow_cache(
    data_dir: str, work_dir: str, sample_name: str
) -> None:
    """
    Remove ``work/`` and ``.nextflow`` before ``run_analysis.sh --fresh``.

    Nested Nextflow Docker often leaves these as root on the host mount; plain ``rm``
    and ``sudo`` (missing in gx-daemon image) then fail inside run_analysis.sh.
    """
    root = data_dir.rstrip("/")
    sample_dir = os.path.join(root, "analysis", work_dir, sample_name)
    targets = [
        os.path.join(sample_dir, "work"),
        os.path.join(sample_dir, ".nextflow"),
    ]
    existing = [p for p in targets if os.path.exists(p)]
    if not existing:
        return

    for path in existing:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError:
            pass

    remaining = [p for p in targets if os.path.exists(p)]
    if not remaining:
        logger.info(
            "[carrier_screening] Cleared Nextflow cache for fresh run: %s",
            sample_dir,
        )
        return

    if not _docker_available():
        raise RuntimeError(
            "Cannot clear Nextflow work/.nextflow for --fresh run "
            f"(still present under {sample_dir}). On the host run: "
            f"sudo rm -rf {remaining[0]!r}"
            + (f" {remaining[1]!r}" if len(remaining) > 1 else "")
        )

    logger.info(
        "[carrier_screening] Clearing root-owned Nextflow cache via docker: %s",
        sample_dir,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "-v",
            f"{sample_dir}:{sample_dir}",
            "alpine:3.20",
            "sh",
            "-c",
            f'rm -rf "{sample_dir}/work" "{sample_dir}/.nextflow"',
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if any(os.path.exists(p) for p in targets):
        raise RuntimeError(
            f"Nextflow cache still present after docker cleanup: {sample_dir}"
        )


def remove_gx_exome_run_container(work_dir: str, sample_name: str) -> bool:
    """
    Remove a leftover ``run_analysis.sh`` driver container (``--name gx-exome-...``).

    After Stop the named container may still exist and block the next ``docker run``.
    """
    if not _docker_available():
        return False
    name = gx_exome_run_container_name(work_dir, sample_name)
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
        logger.info("[carrier_screening] Removed stale gx-exome run container: %s", name)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning(
            "[carrier_screening] Failed to remove gx-exome run container %s: %s",
            name,
            e,
        )
        return False


def remove_gx_exome_run_container_for_job(job: Job) -> bool:
    if job.service_code not in _CARRIER_LIKE:
        return False
    return remove_gx_exome_run_container(
        carrier_run_analysis_work_arg(job),
        carrier_sequencing_folder(job),
    )
