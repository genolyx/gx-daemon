"""
Platform API Client

Merges:
- nipt-daemon aws_client.py (fetch_full_order, download FASTQ, sequencing data)
- service-daemon platform_client.py (status update, output upload, notification)
"""

import os
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
    return OrderDetailResponse(**data)


async def get_order_sequencing_data(order_id: str) -> List[SequencingRecord]:
    url = f"{settings.platform_api_base}/analysis/sequencing-data/order/{order_id}"
    resp = await auth_request("GET", url)
    resp.raise_for_status()
    records = resp.json()["data"]
    return [SequencingRecord(**r) for r in records]


def extract_work_dir(order_id: str) -> str:
    """order_id에서 work_dir 추출 (GNCI25060001 → 2506)"""
    if len(order_id) >= 12:
        if order_id.startswith("GN"):
            return order_id[4:8]
        elif order_id.startswith("DGN"):
            return order_id[5:9]
    return order_id


async def download_fastqs(order: FullOrder) -> Tuple[str, str]:
    work_dir = extract_work_dir(order.orderId)
    local_dir = os.path.join(settings.fastq_base_dir, work_dir, order.orderId)
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


async def check_local_fastq_exist(order_id: str, dto: SubmitOrderDto) -> bool:
    try:
        work_dir = extract_work_dir(order_id)
        fastq_dir = os.path.join(settings.fastq_base_dir, work_dir, order_id)
        if not os.path.isdir(fastq_dir):
            return False
        files = sorted(f for f in os.listdir(fastq_dir) if f.endswith(".fastq.gz"))
        if len(files) < 2:
            return False
        sample_barcode = dto.sampleBarcode.strip() if dto.sampleBarcode else ""
        valid_files = [
            f for f in files
            if f.startswith(order_id) or (sample_barcode and f.startswith(sample_barcode))
        ]
        return len(valid_files) >= 2
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
    order_id: str, dto: SubmitOrderDto, patient_age: int, lab_identifiers: List[str]
) -> FullOrder:
    work_dir = extract_work_dir(order_id)
    fastq_dir = os.path.join(settings.fastq_base_dir, work_dir, order_id)
    if not os.path.isdir(fastq_dir):
        error_msg = f"Local FASTQ directory not found: {fastq_dir}"
        await notify_platform_failure(order_id, error_msg)
        raise FileNotFoundError(error_msg)

    files = sorted(f for f in os.listdir(fastq_dir) if f.endswith(".fastq.gz"))
    if len(files) < 2:
        error_msg = f"Not enough FASTQ files in {fastq_dir}"
        await notify_platform_failure(order_id, "Insufficient sequencing data")
        raise RuntimeError(error_msg)

    sample_barcode = dto.sampleBarcode.strip() if dto.sampleBarcode else None
    valid_files = [
        f for f in files
        if f.startswith(order_id) or (sample_barcode and f.startswith(sample_barcode))
    ]
    if len(valid_files) < 2:
        error_msg = f"Not enough valid FASTQ files ({len(valid_files)} of {len(files)})"
        await notify_platform_failure(order_id, "Insufficient sequencing data")
        raise RuntimeError(error_msg)

    r1_path = os.path.join(fastq_dir, valid_files[0])
    r2_path = os.path.join(fastq_dir, valid_files[1])
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


async def fetch_full_order(order_id: str, od: OrderDetailSubmit, dto: SubmitOrderDto) -> FullOrder:
    patient_age = od.age if od.age is not None else dto.calculate_age()
    lab_identifiers = od.labIdentifier if od.labIdentifier else dto.labIdentifier

    if dto.is_remotedata_client():
        if await check_local_fastq_exist(order_id, dto):
            return await handle_local_fastq_order(order_id, dto, patient_age, lab_identifiers)
        else:
            return await handle_download_fastq_order(order_id, dto, patient_age, lab_identifiers)
    elif dto.is_localdata_client():
        return await handle_local_fastq_order(order_id, dto, patient_age, lab_identifiers)
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
    """Unified platform client for status updates, uploads, notifications."""

    def __init__(self):
        self.base_url = settings.platform_api_base.rstrip("/")

    @staticmethod
    def _skipped(msg: str = "Platform API disabled") -> NotificationResult:
        return NotificationResult(status=NotificationStatus.SKIPPED, message=msg)

    async def update_order_status(
        self, order_id: str, service_code: str, status: str, progress: int = 0, message: str = ""
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            url = f"{self.base_url}/analysis/order/{order_id}/status"
            payload = {"status": status, "progress": progress, "message": message}
            response = await auth_request(method="PATCH", url=url, json=payload, timeout=10.0)
            return NotificationResult(
                status=NotificationStatus.SUCCESS, response_code=response.status_code
            )
        except Exception as e:
            logger.warning(f"Failed to update platform status for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def notify_analysis_result(
        self, order_id: str, service_code: str, success: bool = True, log: str = ""
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            url = f"{self.base_url}/analysis/result/{order_id}"
            payload = {"success": success, "log": log}
            await auth_request(method="DELETE", url=url, timeout=10.0)
            response = await auth_request(method="POST", url=url, json=payload, timeout=30.0)
            return NotificationResult(
                status=NotificationStatus.SUCCESS, response_code=response.status_code
            )
        except Exception as e:
            logger.error(f"notify_analysis_result failed for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def notify_analysis_failed(
        self, order_id: str, service_code: str, error: str
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            url = f"{self.base_url}/analysis/order/{order_id}/failed"
            payload = {"failedReason": error}
            response = await auth_request(method="PATCH", url=url, json=payload, timeout=10.0)
            return NotificationResult(
                status=NotificationStatus.SUCCESS, response_code=response.status_code
            )
        except Exception as e:
            logger.error(f"notify_analysis_failed for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def upload_analysis_file(self, order_id: str, tar_path: str) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            if not os.path.exists(tar_path):
                return NotificationResult(status=NotificationStatus.NOT_FOUND, message=f"File not found: {tar_path}")

            url = f"{self.base_url}/analysis/result/{order_id}/file"
            await auth_request(method="DELETE", url=url, timeout=30.0)

            for attempt in range(3):
                try:
                    with open(tar_path, 'rb') as f:
                        response = await auth_request(method="POST", url=url, files={'file': f}, timeout=300.0)
                    if response.status_code == 200:
                        logger.info(f"TAR file uploaded for {order_id}")
                        return NotificationResult(
                            status=NotificationStatus.SUCCESS, response_code=200
                        )
                except Exception as e:
                    logger.warning(f"Upload attempt {attempt+1}/3 failed for {order_id}: {e}")
                    if attempt == 2:
                        raise
                    import asyncio
                    await asyncio.sleep(2)

            return NotificationResult(status=NotificationStatus.FAILED, message="Upload failed after retries")
        except Exception as e:
            logger.error(f"upload_analysis_file failed for {order_id}: {e}")
            return NotificationResult(status=NotificationStatus.FAILED, message=str(e))

    async def upload_pdf_report(
        self, order_id: str, pdf_path: str, *, signed: bool = False
    ) -> NotificationResult:
        try:
            if not settings.platform_api_enabled:
                return self._skipped()
            if not os.path.exists(pdf_path):
                return NotificationResult(status=NotificationStatus.NOT_FOUND, message=f"PDF not found: {pdf_path}")

            if signed:
                url = f"{self.base_url}/analysis/order/{order_id}/signed-pdf-result"
            else:
                url = f"{self.base_url}/analysis/order/{order_id}/pdf-result"

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
        self, order_id: str, service_code: str, output_files: List[OutputFile]
    ) -> Dict[str, NotificationResult]:
        results = {}
        for of in output_files:
            ft = of.file_type.lower()
            if ft in ("tar", "output_tar"):
                results[of.file_type] = await self.upload_analysis_file(order_id, of.file_path)
            elif ft == "pdf":
                results[of.file_type] = await self.upload_pdf_report(order_id, of.file_path)
            elif ft == "signed_pdf":
                results[of.file_type] = await self.upload_pdf_report(order_id, of.file_path, signed=True)
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
