"""
Platform API Client

Merges:
- nipt-daemon aws_client.py (fetch_full_order, download FASTQ, sequencing data)
- service-daemon platform_client.py (status update, output upload, notification)
"""

import os
import re
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urljoin

import httpx

from .config import settings
from .models import (
    NotificationResult, NotificationStatus, OutputFile,
    OrderDetailResponse, OrderDetailSubmit, SequencingRecord, FullOrder, SubmitOrderDto,
    ClientDetailResponse, ClientApiResponse,
    PartnerClientResponse, PartnerClientApiResponse,
)
from .auth_client import auth_request, get_auth_headers

logger = logging.getLogger(__name__)


# ─── Order detail / sequencing data (from nipt-daemon aws_client) ──

async def get_order_detail(order_id: str) -> OrderDetailResponse:
    url = f"{settings.platform_api_base}/analysis/order/{order_id}"
    resp = await auth_request("GET", url)
    resp.raise_for_status()
    data = resp.json()["data"]

    # gx-daemon Portal API는 임상 정보를 serviceData 하위에 중첩
    # serviceData 필드를 최상위로 flat merge (top-level 값이 없을 때만 채움)
    service_data = data.get("serviceData") or {}
    if service_data:
        for k, v in service_data.items():
            if k not in data or data[k] is None:
                data[k] = v

    if settings.debug_http:
        import json as _json
        logger.debug(
            "get_order_detail merged for %s:\n%s",
            order_id,
            _json.dumps(data, ensure_ascii=False, indent=2),
        )
    return OrderDetailResponse(**data)


async def get_order_sequencing_data(order_id: str) -> List[SequencingRecord]:
    url = f"{settings.platform_api_base}/analysis/sequencing-data/order/{order_id}"
    resp = await auth_request("GET", url)
    resp.raise_for_status()
    records = resp.json()["data"]
    return [SequencingRecord(**r) for r in records]


def extract_work_dir(order_id: str) -> str:
    """order_id에서 YYMM work_dir 추출.

    order_id 형식: [Portal prefix][Client ID (2자)][YYMM (4자)][seq (4자)]
      예) DGGCI26070003 (Dev Portal DGG + CI + 2607 + 0003) → 2607
          GGCI26070003  (Prod Portal GG + CI + 2607 + 0003)  → 2607
          GNCI25060001  (GN + CI + 2506 + 0001)              → 2506

    prefix 길이가 가변적이므로 끝 8자리(YYMM+seq) 숫자를 regex로 찾아 앞 4자리 반환.
    """
    m = re.search(r'(\d{4})\d{4}$', order_id)
    if m:
        return m.group(1)
    return order_id


async def download_fastqs(order: FullOrder, fastq_base: Optional[str] = None) -> Tuple[str, str]:
    base = fastq_base or settings.fastq_base_dir
    work_dir = extract_work_dir(order.orderId)
    local_dir = os.path.join(base, work_dir, order.orderId)
    os.makedirs(local_dir, exist_ok=True)
    local_r1 = await _download_single_fastq(order.r1_id, order.r1_path, local_dir)
    local_r2 = await _download_single_fastq(order.r2_id, order.r2_path, local_dir)
    return local_r1, local_r2


async def _download_single_fastq(file_id: str, file_path: str, local_dir: str) -> str:
    url = f"{settings.platform_api_base}/analysis/sequencing-data/{file_id}"
    resp = await auth_request("GET", url)
    resp.raise_for_status()
    file_url = resp.json()["data"]["url"]
    logger.info(f"Downloading FASTQ from S3: {file_url[:80]}...")
    async with httpx.AsyncClient() as client:
        file_resp = await client.get(file_url, timeout=600.0)
        file_resp.raise_for_status()
    local_path = os.path.join(local_dir, os.path.basename(file_path))
    with open(local_path, "wb") as f:
        f.write(file_resp.content)
    logger.info(f"Downloaded: {local_path} ({os.path.getsize(local_path)} bytes)")
    return local_path


def _pick_r1_r2(files: List[str], order_id: str, barcode: str = "") -> Tuple[Optional[str], Optional[str]]:
    """파일 목록에서 R1/R2 쌍을 찾아 반환.

    우선순위:
    1. order_id 또는 barcode prefix로 시작하는 파일에서 _R1_ / _R2_ 패턴
    2. 전체 파일 중 _R1_ / _R2_ 패턴
    3. sorted 결과의 첫 두 파일 (파일이 정확히 2개일 때)
    """
    def is_r1(f: str) -> bool:
        return bool(re.search(r'[_.]R1[_.]', f) or f.endswith('_R1.fastq.gz'))

    def is_r2(f: str) -> bool:
        return bool(re.search(r'[_.]R2[_.]', f) or f.endswith('_R2.fastq.gz'))

    candidates = [
        f for f in files
        if f.startswith(order_id) or (barcode and f.startswith(barcode))
    ] or files  # prefix 매칭 없으면 전체 대상

    r1 = next((f for f in candidates if is_r1(f)), None)
    r2 = next((f for f in candidates if is_r2(f)), None)

    if r1 and r2:
        return r1, r2
    # R1/R2 패턴 없이 파일이 2개뿐이면 정렬 순서대로 사용
    if len(files) == 2:
        return files[0], files[1]
    return None, None


async def check_local_fastq_exist(
    order_id: str,
    dto: SubmitOrderDto,
    fastq_base: Optional[str] = None,
    sample_barcode: Optional[str] = None,
) -> bool:
    try:
        base = fastq_base or settings.fastq_base_dir
        work_dir = extract_work_dir(order_id)
        fastq_dir = os.path.join(base, work_dir, order_id)
        if not os.path.isdir(fastq_dir):
            return False
        files = sorted(f for f in os.listdir(fastq_dir) if f.endswith(".fastq.gz"))
        if len(files) < 2:
            return False
        barcode = (sample_barcode or "").strip()
        r1, r2 = _pick_r1_r2(files, order_id, barcode)
        return r1 is not None and r2 is not None
    except Exception as e:
        logger.warning(f"check_local_fastq_exist error for {order_id}: {e}")
        return False


async def handle_download_fastq_order(
    order_id: str, dto: SubmitOrderDto, patient_age: int, lab_identifiers: List[str]
) -> FullOrder:
    seqs = await get_order_sequencing_data(order_id)
    if len(seqs) < 2:
        error_msg = f"Insufficient sequencing data for {order_id}"
        logger.error(error_msg)
        await notify_platform_failure(order_id, error_msg)
        raise RuntimeError(error_msg)

    lab_name = lab_identifiers[0] if lab_identifiers else None
    return FullOrder(
        orderId=order_id,
        clientId="",
        sequencingDataMethod=dto.sequencingDataMethod,
        lab=lab_name,
        age=patient_age,
        r1_id=seqs[0].id,
        r2_id=seqs[1].id,
        r1_path=seqs[0].asset.path,
        r2_path=seqs[1].asset.path,
    )


async def handle_local_fastq_order(
    order_id: str,
    dto: SubmitOrderDto,
    patient_age: int,
    lab_identifiers: List[str],
    fastq_base: Optional[str] = None,
    sample_barcode: Optional[str] = None,
) -> FullOrder:
    base = fastq_base or settings.fastq_base_dir
    work_dir = extract_work_dir(order_id)
    fastq_dir = os.path.join(base, work_dir, order_id)
    if not os.path.isdir(fastq_dir):
        error_msg = f"Local FASTQ directory not found: {fastq_dir}"
        await notify_platform_failure(order_id, error_msg)
        raise FileNotFoundError(error_msg)

    files = sorted(f for f in os.listdir(fastq_dir) if f.endswith(".fastq.gz"))
    if len(files) < 2:
        error_msg = f"Not enough FASTQ files in {fastq_dir}"
        await notify_platform_failure(order_id, "Insufficient sequencing data")
        raise RuntimeError(error_msg)

    barcode = (sample_barcode or "").strip()
    r1_name, r2_name = _pick_r1_r2(files, order_id, barcode)
    if not r1_name or not r2_name:
        error_msg = f"Could not identify R1/R2 FASTQ pair in {fastq_dir} (files: {files})"
        await notify_platform_failure(order_id, "Insufficient sequencing data")
        raise RuntimeError(error_msg)

    r1_path = os.path.join(fastq_dir, r1_name)
    r2_path = os.path.join(fastq_dir, r2_name)
    lab_name = lab_identifiers[0] if lab_identifiers else "default"

    return FullOrder(
        orderId=order_id,
        clientId="",
        sequencingDataMethod=dto.sequencingDataMethod,
        lab=lab_name,
        age=patient_age,
        r1_id="",
        r2_id="",
        r1_path=r1_path,
        r2_path=r2_path,
    )


def _platform_fastq_base(service_code: str) -> str:
    """서비스별 Platform FASTQ 다운로드/탐색 기준 디렉토리."""
    if service_code in ("carrier_screening", "whole_exome", "health_screening"):
        return settings.carrier_screening_fastq_dir
    if service_code == "sgnipt":
        return settings.sgnipt_fastq_root
    if service_code == "nipt":
        return settings.nipt_fastq_dir
    return settings.fastq_base_dir


async def fetch_full_order(
    order_id: str,
    od: OrderDetailSubmit,
    dto: SubmitOrderDto,
    fastq_base: Optional[str] = None,
) -> FullOrder:
    """Platform API에서 FASTQ 정보를 가져와 FullOrder 반환.

    fastq_base를 명시하지 않으면 _platform_fastq_base()로 결정한 서비스별 기준 경로를 사용.
    호출자가 service_code를 알고 있으면 _platform_fastq_base(service_code)를 넘기는 것을 권장.
    """
    base = fastq_base or settings.fastq_base_dir
    # od.age / od.sampleBarcode는 _enqueue_platform_order에서 get_order_detail로 세팅됨
    patient_age = od.age
    lab_identifiers = od.labIdentifier if od.labIdentifier else dto.labIdentifier
    sample_barcode = od.sampleBarcode  # SubmitOrderDto에는 없음 → Platform API에서 조회한 값 사용

    if dto.is_remotedata_client():
        if await check_local_fastq_exist(order_id, dto, fastq_base=base, sample_barcode=sample_barcode):
            return await handle_local_fastq_order(
                order_id, dto, patient_age, lab_identifiers, fastq_base=base, sample_barcode=sample_barcode
            )
        else:
            full_order = await handle_download_fastq_order(
                order_id, dto, patient_age, lab_identifiers
            )
            # REMOTE 다운로드 시 서비스별 fastq_base로 저장
            r1, r2 = await download_fastqs(full_order, fastq_base=base)
            full_order.r1_path = r1
            full_order.r2_path = r2
            return full_order
    elif dto.is_localdata_client():
        return await handle_local_fastq_order(
            order_id, dto, patient_age, lab_identifiers, fastq_base=base, sample_barcode=sample_barcode
        )
    else:
        raise RuntimeError(f"Unknown sequencingDataMethod: {dto.sequencingDataMethod}")


# ─── Client info helpers ──────────────────────────────────────

async def get_client_detail(client_id: str) -> Optional[ClientDetailResponse]:
    try:
        url = f"{settings.platform_api_base}/organization/client/{client_id}"
        response = await auth_request(method="GET", url=url)
        if response.status_code != 200:
            return None
        api_response = ClientApiResponse(**response.json())
        return api_response.data if api_response.status == 200 else None
    except Exception as e:
        logger.error(f"Error getting client details for {client_id}: {e}")
        return None


async def get_client_by_partner(partner_id: str) -> Optional[PartnerClientResponse]:
    try:
        url = f"{settings.platform_api_base}/organization/partner/{partner_id}/client"
        response = await auth_request(method="GET", url=url)
        if response.status_code != 200:
            return None
        api_response = PartnerClientApiResponse(**response.json())
        return api_response.data if api_response.status == 200 else None
    except Exception as e:
        logger.error(f"Error getting client by partner {partner_id}: {e}")
        return None


async def get_client_info(order) -> Optional[ClientDetailResponse]:
    if hasattr(order, 'clientId') and order.clientId is not None:
        client = await get_client_detail(order.clientId)
        if client:
            return client

    if hasattr(order, 'partnerId') and order.partnerId is not None:
        partner_client = await get_client_by_partner(order.partnerId)
        if partner_client:
            return ClientDetailResponse(
                id=partner_client.id,
                name=partner_client.name,
                address=partner_client.address,
                phoneNumber=partner_client.phoneNumber,
                email=partner_client.email,
                identifier=partner_client.identifier,
                createdAt=partner_client.createdAt,
                updatedAt=partner_client.updatedAt,
            )

    if hasattr(order, 'package') and order.package and isinstance(order.package, dict):
        pkg_client_id = order.package.get('clientId')
        if pkg_client_id:
            client = await get_client_detail(pkg_client_id)
            if client:
                return client

    return None


# ─── Platform notification (from service-daemon platform_client) ──

class PlatformClient:
    """Unified platform client for status updates, uploads, notifications.

    Per-order callback_url support:
      - Pass callback_url to any method to override the global PLATFORM_API_BASE.
      - Omit (or pass None) to use the global default (Gx-Portal backward-compat).
    """

    def __init__(self):
        self.base_url = settings.platform_api_base.rstrip("/")

    def _resolve_base(self, callback_url: Optional[str] = None) -> str:
        """Return per-job callback base URL if provided, else fall back to global config."""
        if callback_url:
            return callback_url.rstrip("/")
        return self.base_url

    @staticmethod
    def _skipped(msg: str = "Platform API disabled") -> NotificationResult:
        return NotificationResult(status=NotificationStatus.SKIPPED, message=msg)

    async def update_order_status(
        self, order_id: str, service_code: str, status: str, progress: int = 0, message: str = "",
        callback_url: Optional[str] = None,
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            url = f"{self._resolve_base(callback_url)}/analysis/order/{order_id}/status"
            payload = {"status": status, "progress": progress, "message": message}
            response = await auth_request(method="PATCH", url=url, json=payload, timeout=10.0)
            return NotificationResult(
                status=NotificationStatus.SUCCESS, response_code=response.status_code
            )
        except Exception as e:
            # 404 = Portal이 중간 status 업데이트 엔드포인트를 지원하지 않음 → DEBUG로 낮춤
            err_str = str(e)
            level = "debug" if "404" in err_str or "Not Found" in err_str else "warning"
            getattr(logger, level)(f"update_order_status skipped for {order_id} ({status}): {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=err_str)

    async def notify_analysis_result(
        self, order_id: str, service_code: str, success: bool = True, log: str = "",
        callback_url: Optional[str] = None,
        result_data: Optional[Dict[str, Any]] = None,
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            url = f"{self._resolve_base(callback_url)}/analysis/result/{order_id}"
            # 결과 JSON 전체를 payload에 포함 (Portal이 review 등 필드를 기대)
            # nipt-daemon과 동일: json_data + success/log 필드 추가
            if result_data:
                payload = dict(result_data)
                payload["success"] = success
                payload["log"] = log
            else:
                payload = {"success": success, "log": log}
            # 기존 결과 삭제 시도 — 없으면(404) 정상이므로 무시
            try:
                await auth_request(method="DELETE", url=url, timeout=10.0)
            except Exception as del_err:
                if "404" in str(del_err) or "Not Found" in str(del_err):
                    logger.debug(f"No existing result to delete for {order_id} (404 is ok)")
                else:
                    logger.warning(f"DELETE result for {order_id} failed: {del_err}")
            response = await auth_request(method="POST", url=url, json=payload, timeout=30.0)
            logger.info(f"notify_analysis_result succeeded for {order_id}: {response.status_code}")
            return NotificationResult(
                status=NotificationStatus.SUCCESS, response_code=response.status_code
            )
        except Exception as e:
            logger.error(f"notify_analysis_result failed for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def notify_analysis_failed(
        self, order_id: str, service_code: str, error: str,
        callback_url: Optional[str] = None,
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            url = f"{self._resolve_base(callback_url)}/analysis/order/{order_id}/failed"
            payload = {"failedReason": error}
            response = await auth_request(method="PATCH", url=url, json=payload, timeout=10.0)
            return NotificationResult(
                status=NotificationStatus.SUCCESS, response_code=response.status_code
            )
        except Exception as e:
            logger.error(f"notify_analysis_failed for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def upload_analysis_file(
        self, order_id: str, tar_path: str, callback_url: Optional[str] = None
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            if not os.path.exists(tar_path):
                return NotificationResult(status=NotificationStatus.NOT_FOUND, message=f"File not found: {tar_path}")

            size_mb = os.path.getsize(tar_path) / (1024 * 1024)
            logger.info(
                "Uploading analysis archive for %s: %.1f MB (%s)",
                order_id, size_mb, tar_path,
            )

            url = f"{self._resolve_base(callback_url)}/analysis/result/{order_id}/file"
            try:
                await auth_request(method="DELETE", url=url, timeout=30.0)
            except Exception as del_err:
                logger.warning(
                    "DELETE existing analysis file for %s failed (continuing): %s",
                    order_id, del_err,
                )

            # Cloudflare proxy timeout is ~100s (HTTP 524). Retrying the same
            # large body three times often burns ~6 minutes without helping.
            max_attempts = 2 if size_mb >= 20 else 3
            last_err = ""
            for attempt in range(max_attempts):
                try:
                    # Disable Expect: 100-continue — some CF/origin paths mishandle it on large POSTs
                    with open(tar_path, "rb") as f:
                        response = await auth_request(
                            method="POST",
                            url=url,
                            files={"file": (os.path.basename(tar_path), f, "application/x-tar")},
                            headers={"Expect": ""},
                            timeout=300.0,
                        )
                    if response.status_code == 200:
                        logger.info("TAR file uploaded for %s (%.1f MB)", order_id, size_mb)
                        return NotificationResult(
                            status=NotificationStatus.SUCCESS, response_code=200
                        )
                    last_err = f"HTTP {response.status_code}"
                    logger.warning(
                        "Upload attempt %s/%s failed for %s: %s",
                        attempt + 1, max_attempts, order_id, last_err,
                    )
                except Exception as e:
                    last_err = str(e)
                    # Truncate Cloudflare HTML error pages in logs
                    if "524" in last_err or "<!DOCTYPE html>" in last_err.lower():
                        last_err = "Cloudflare/origin timeout (HTTP 524) uploading large archive"
                    logger.warning(
                        "Upload attempt %s/%s failed for %s: %s",
                        attempt + 1, max_attempts, order_id, last_err,
                    )
                    if attempt == max_attempts - 1:
                        break
                    import asyncio
                    await asyncio.sleep(2)

            return NotificationResult(
                status=NotificationStatus.FAILED,
                message=last_err or "Upload failed after retries",
            )
        except Exception as e:
            logger.error(f"upload_analysis_file failed for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def upload_pdf_report(
        self, order_id: str, pdf_path: str, *, signed: bool = False,
        callback_url: Optional[str] = None,
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            if not os.path.exists(pdf_path):
                return NotificationResult(status=NotificationStatus.NOT_FOUND, message=f"PDF not found: {pdf_path}")

            base = self._resolve_base(callback_url)
            if signed:
                url = f"{base}/analysis/order/{order_id}/signed-pdf-result"
            else:
                url = f"{base}/analysis/order/{order_id}/pdf-result"

            with open(pdf_path, 'rb') as f:
                response = await auth_request(method="POST", url=url, files={'file': f}, timeout=120.0)

            if response.status_code == 200:
                kind = "signed" if signed else "normal"
                logger.info(f"Uploaded {kind} PDF for {order_id}")
                return NotificationResult(status=NotificationStatus.SUCCESS, response_code=200)
            else:
                return NotificationResult(
                    status=NotificationStatus.FAILED,
                    message=f"PDF upload HTTP {response.status_code}",
                    response_code=response.status_code,
                )
        except Exception as e:
            logger.error(f"upload_pdf_report failed for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def upload_all_outputs(
        self, order_id: str, service_code: str, output_files: List[OutputFile],
        callback_url: Optional[str] = None,
    ) -> Dict[str, NotificationResult]:
        results = {}
        for of in output_files:
            ft = of.file_type.lower()
            if ft in ("tar", "output_tar"):
                results[of.file_type] = await self.upload_analysis_file(
                    order_id, of.file_path, callback_url=callback_url
                )
            elif ft == "pdf":
                results[of.file_type] = await self.upload_pdf_report(
                    order_id, of.file_path, callback_url=callback_url
                )
            elif ft == "signed_pdf":
                results[of.file_type] = await self.upload_pdf_report(
                    order_id, of.file_path, signed=True, callback_url=callback_url
                )
            else:
                logger.debug(f"Skipping upload for file_type: {of.file_type}")
        return results


# Global instance
_platform_client: Optional[PlatformClient] = None


def get_platform_client() -> PlatformClient:
    global _platform_client
    if _platform_client is None:
        _platform_client = PlatformClient()
    return _platform_client


async def notify_platform_failure(order_id: str, error_msg: str):
    try:
        client = get_platform_client()
        await client.notify_analysis_failed(order_id, "carrier_screening", error_msg)
    except Exception as e:
        logger.error(f"Failed to notify platform: {e}")
