"""Download GX presigned FASTQ URLs without logging signatures."""

from __future__ import annotations

import logging
import os
from typing import Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=60.0, read=600.0, write=60.0, pool=60.0)


def safe_url_for_log(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return "(invalid-url)"


async def download_pair(
    r1_url: str,
    r2_url: str,
    dest_dir: str,
    order_id: str,
) -> Tuple[str, str]:
    os.makedirs(dest_dir, exist_ok=True)
    r1_dest = os.path.join(dest_dir, f"{order_id}_R1.fastq.gz")
    r2_dest = os.path.join(dest_dir, f"{order_id}_R2.fastq.gz")
    await _download_one(r1_url, r1_dest, "R1")
    await _download_one(r2_url, r2_dest, "R2")
    return r1_dest, r2_dest


async def _download_one(url: str, dest: str, role: str) -> None:
    host_path = safe_url_for_log(url)
    logger.info("GX FASTQ %s GET %s → %s", role, host_path, dest)
    tmp = dest + ".part"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code in (401, 403):
                    raise RuntimeError(f"FASTQ {role} URL expired or forbidden ({resp.status_code})")
                resp.raise_for_status()
                written = 0
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        f.write(chunk)
                        written += len(chunk)
        if written <= 0:
            raise RuntimeError(f"FASTQ {role} download is empty")
        os.replace(tmp, dest)
        logger.info("GX FASTQ %s saved (%s bytes)", role, written)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
