# pdf_tools.py
"""
PDF signing utilities (image + tight caption overlay w/ auto-trim).

Deps:
    pip install pypdf reportlab pillow
"""

from __future__ import annotations
import io
import os
import time
import logging
from typing import Optional, Tuple, List

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import mm
from PIL import Image, ImageChops
import datetime

# ===== caption defaults (same behavior as your script) =====
CAPTION_POSITION_DEFAULT = "right"   # or "below"
CAPTION_GAP_PT_DEFAULT   = 2.0
CAPTION_OVERLAP_PT_DEF   = 0.0
CAPTION_DX_PT_DEFAULT    = 0.0
CAPTION_DY_PT_DEFAULT    = 0.0
CAPTION_LINE_SPACING_PT  = 1.5

TRIM_DEFAULT = True
WHITE_THRESH_DEFAULT = 250

ANCHOR_DEFAULT = "bottom_right"

logger = logging.getLogger(__name__)

def mm_to_pt(val_mm: Optional[float]) -> Optional[float]:
    return None if val_mm is None else val_mm * mm

# ---------- image trim helpers (ported from your script) ----------
def _alpha_trim_bbox(img_rgba: Image.Image):
    alpha = img_rgba.split()[-1]
    return alpha.getbbox()

def _white_trim_bbox(img_rgb: Image.Image, white_threshold: int):
    bg = Image.new("RGB", img_rgb.size, (255, 255, 255))
    diff = ImageChops.difference(img_rgb, bg)
    gray = diff.convert("L")
    mask = gray.point(lambda p: 0 if p < (255 - white_threshold) else p)
    return mask.getbbox()

def load_trimmed_image_reader(path: str, white_threshold: int):
    im = Image.open(path)
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    bbox = _alpha_trim_bbox(im)
    if not bbox:
        bbox = _white_trim_bbox(im.convert("RGB"), white_threshold) or (0, 0, im.width, im.height)
    cropped = im.crop(bbox)
    return ImageReader(cropped), (im.width, im.height), (cropped.width, cropped.height), bbox


# ---------- overlay (points API) Use Anchor ----------
def _string_width(text: str, font_name: str, font_size: float) -> float:
    if not text:
        return 0.0
    try:
        return pdfmetrics.stringWidth(text, font_name, font_size)
    except Exception:
        # 폰트 미등록 시 대략값
        return len(text) * font_size * 0.55

def _compose_caption_lines(signer: Optional[str], reason: Optional[str], show_date: bool) -> List[str]:
    lines: List[str] = []
    if signer:
        lines.append(f"Signed by: {signer}")
    if reason:
        lines.append(f"Reason: {reason}")
    if show_date:
        lines.append(f"Date: {datetime.date.today().isoformat()}")
    return lines

def make_overlay_pt(
    page_width_pt: float,
    page_height_pt: float,
    sig_img_path: str,
    # === 절대 좌표(역호환). 둘 다 주면 절대좌표 모드로 동작 ===
    x_pt: Optional[float] = None,
    y_pt: Optional[float] = None,
    # === 크기 지정 (둘 중 하나만 주면 비율 유지). 없으면 비율 기반으로 자동 ===
    w_pt: Optional[float] = None,
    h_pt: Optional[float] = None,
    *,
    # 캡션
    signer: Optional[str] = None,
    reason: Optional[str] = None,
    show_date: bool = True,
    font_size: float = 6.0,
    caption_pos: str = CAPTION_POSITION_DEFAULT,   # "right" or "below"
    caption_gap_pt: float = CAPTION_GAP_PT_DEFAULT,
    caption_overlap_pt: float = CAPTION_OVERLAP_PT_DEF,  # 겹치기(음수x), right/below에서 간격 대신 겹침
    caption_dx_pt: float = CAPTION_DX_PT_DEFAULT,  # 미세 보정 x
    caption_dy_pt: float = CAPTION_DY_PT_DEFAULT,  # 미세 보정 y
    # 트리밍
    trim: bool = TRIM_DEFAULT,
    white_threshold: int = WHITE_THRESH_DEFAULT,
    # === 새 옵션: 비율/앵커 ===
    width_ratio: Optional[float] = 0.10,   # 이미지 폭 = 페이지 폭의 10%
    anchor: str = ANCHOR_DEFAULT,          # "bottom_right" 등
    margin_w_ratio: float = 0.04,          # 좌/우 여백 비율 (페이지 폭 기준)
    margin_h_ratio: float = 0.02,          # 상/하 여백 비율 (페이지 높이 기준)
    anchor_includes_caption: bool = True,  # True면 (이미지+캡션) 블록 전체를 앵커 기준으로 정렬
    caption_font_name: str = "Helvetica",
) -> io.BytesIO:
    """
    Create a 1-page overlay PDF (pt 단위).

    동작 우선순위:
      1) x_pt와 y_pt가 주어지면 → 절대 좌표 모드 (기존 동작과 동일)
      2) 아니면 → width_ratio/anchor/margin_*_ratio 기반 자동 배치

    반환: overlay 1페이지 PDF를 담은 BytesIO
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width_pt, page_height_pt))

    # --- 이미지 로드/트림 ---
    if trim:
        # 프로젝트의 유틸. 없으면 trim=False로 사용하세요.
        img_reader, (ow, oh), (cw, ch), _bbox_px = load_trimmed_image_reader(sig_img_path, white_threshold)
    else:
        img_reader = ImageReader(sig_img_path)
        with Image.open(sig_img_path) as im:
            ow, oh = im.size
            cw, ch = im.size

    # --- 이미지 크기 결정 ---
    if w_pt and h_pt:
        draw_w, draw_h = w_pt, h_pt
    elif w_pt and not h_pt:
        draw_w = w_pt
        draw_h = (ch / cw) * draw_w
    elif h_pt and not w_pt:
        draw_h = h_pt
        draw_w = (cw / ch) * draw_h
    else:
        # 절대 크기를 안 줬다면, 페이지 폭 비율로
        wr = width_ratio if width_ratio else 0.10
        draw_w = page_width_pt * wr
        draw_h = (ch / cw) * draw_w

    # --- 캡션 구성 및 치수 ---
    lines = _compose_caption_lines(signer, reason, show_date)
    caption_w = max((_string_width(line, caption_font_name, font_size) for line in lines), default=0.0)
    line_height = font_size + CAPTION_LINE_SPACING_PT
    total_caption_h = line_height * max(len(lines), 1) if lines else 0.0

    # 블록(이미지+캡션)의 전체 크기 추정
    block_w = draw_w
    block_h = draw_h
    if lines:
        if caption_pos == "right":
            # [IMG][gap][CAPTION] (여기서 overlap은 gap을 줄이는 개념)
            effective_gap = max(0.0, caption_gap_pt - caption_overlap_pt)
            block_w = draw_w + effective_gap + caption_w
            block_h = max(draw_h, total_caption_h)
        else:  # "below"
            effective_gap = max(0.0, caption_gap_pt - caption_overlap_pt)
            block_w = max(draw_w, caption_w)
            block_h = draw_h + effective_gap + total_caption_h

    # --- 위치 계산 ---
    if x_pt is not None and y_pt is not None:
        # 절대 좌표 모드(역호환). 이미지 좌표 그대로 사용.
        img_x, img_y = x_pt, y_pt
        # 캡션은 아래에서 이미지 기준으로 계산
        block_x, block_y = img_x, img_y
    else:
        # 비율/앵커 모드
        mx = page_width_pt  * margin_w_ratio
        my = page_height_pt * margin_h_ratio

        # 앵커 기준 블록 좌하단 좌표
        if anchor == "bottom_right":
            block_x = page_width_pt  - mx - block_w
            block_y = my
        elif anchor == "bottom_left":
            block_x = mx
            block_y = my
        elif anchor == "top_right":
            block_x = page_width_pt  - mx - block_w
            block_y = page_height_pt - my - block_h
        elif anchor == "top_left":
            block_x = mx
            block_y = page_height_pt - my - block_h
        else:
            block_x = page_width_pt  - mx - block_w
            block_y = my

        if lines and anchor_includes_caption:
            if caption_pos == "right":
                # 블록 내에서 이미지는 좌측, 수직 중앙 정렬
                img_x = block_x
                img_y = block_y + max(0.0, (block_h - draw_h) / 2.0)
            else:  # below
                # 블록 내에서 이미지는 상단, 수평 중앙 정렬
                img_x = block_x + max(0.0, (block_w - draw_w) / 2.0)
                img_y = block_y + (total_caption_h + max(0.0, caption_gap_pt - caption_overlap_pt))
        else:
            # 캡션 무시하고 이미지만 앵커
            img_x = block_x
            img_y = block_y

    # 미세 보정
    img_x += caption_dx_pt
    img_y += caption_dy_pt

    # --- 이미지 그리기 ---
    c.drawImage(img_reader, img_x, img_y, width=draw_w, height=draw_h, mask='auto')

    # --- 캡션 그리기 ---
    if lines:
        c.setFont(caption_font_name, font_size)
        if caption_pos == "right":
            text_x = img_x + draw_w + max(0.0, caption_gap_pt - caption_overlap_pt)
            # 이미지와 캡션의 높이가 다를 수 있어 수직 가운데 정렬
            top_align_y = img_y + max(draw_h, total_caption_h)
            # 첫 줄 baseline
            text_y = top_align_y - font_size
        else:  # "below"
            # 중앙 정렬 느낌으로
            text_x = img_x + max(0.0, (draw_w - caption_w) / 2.0)
            # 아래쪽으로 내리기
            text_y = img_y - max(0.0, caption_gap_pt - caption_overlap_pt) - font_size

        # 미세 보정
        text_x += caption_dx_pt
        text_y += caption_dy_pt

        if caption_pos == "right":
            # 위에서 아래로
            for line in lines:
                c.drawString(text_x, text_y, line)
                text_y -= line_height
        else:
            # 아래에 여러 줄이면 아래로 더 내려가므로 마지막 줄부터 그리는 방식도 가능
            for line in lines:
                c.drawString(text_x, text_y, line)
                text_y -= line_height

    c.showPage()
    c.save()
    buf.seek(0)
    return buf

# ---------- overlay (points API) ----------
def make_overlay_pt_old(
    page_width_pt: float,
    page_height_pt: float,
    sig_img_path: str,
    x_pt: float,
    y_pt: float,
    w_pt: Optional[float] = None,
    h_pt: Optional[float] = None,
    *,
    signer: Optional[str] = None,
    reason: Optional[str] = None,
    show_date: bool = True,
    font_size: float = 6.0,
    caption_pos: str = CAPTION_POSITION_DEFAULT,
    caption_gap_pt: float = CAPTION_GAP_PT_DEFAULT,
    caption_overlap_pt: float = CAPTION_OVERLAP_PT_DEF,
    caption_dx_pt: float = CAPTION_DX_PT_DEFAULT,
    caption_dy_pt: float = CAPTION_DY_PT_DEFAULT,
    trim: bool = TRIM_DEFAULT,
    white_threshold: int = WHITE_THRESH_DEFAULT,
) -> io.BytesIO:
    """
    Create 1-page overlay in POINTS coords.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width_pt, page_height_pt))

    if trim:
        img_reader, (ow, oh), (cw, ch), bbox_px = load_trimmed_image_reader(sig_img_path, white_threshold)
    else:
        img_reader = ImageReader(sig_img_path)
        with Image.open(sig_img_path) as im:
            ow, oh = im.size
            cw, ch = im.size

    # keep aspect if only one dim is given
    if w_pt and h_pt:
        draw_w, draw_h = w_pt, h_pt
    elif w_pt and not h_pt:
        draw_w = w_pt
        draw_h = (ch / cw) * draw_w
    elif h_pt and not w_pt:
        draw_h = h_pt
        draw_w = (cw / ch) * draw_h
    else:
        draw_w = 150
        draw_h = (ch / cw) * draw_w

    c.drawImage(img_reader, x_pt, y_pt, width=draw_w, height=draw_h, mask='auto')

    # caption lines
    import datetime
    lines: List[str] = []
    if signer:
        lines.append(f"Signed by: {signer}")
    if reason:
        lines.append(f"Reason: {reason}")
    if show_date:
        lines.append(f"Date: {datetime.date.today().isoformat()}")

    if lines:
        c.setFont("Helvetica", font_size)
        per_line = font_size + CAPTION_LINE_SPACING_PT
        if caption_pos == "below":
            text_x = x_pt + caption_dx_pt
            text_y = y_pt - caption_gap_pt + caption_dy_pt - font_size
        else:  # right (tight to ink box)
            text_x = x_pt + draw_w + caption_dx_pt + (caption_gap_pt - caption_overlap_pt)
            text_y = y_pt + draw_h - font_size + caption_dy_pt

        for line in lines:
            c.drawString(text_x, text_y, line)
            text_y -= per_line

    c.showPage()
    c.save()
    buf.seek(0)
    return buf

# ---------- overlay (millimeters API) ----------
def make_overlay_mm(
    page_width_pt: float,
    page_height_pt: float,
    sig_img_path: str,
    x_mm: float,
    y_mm: float,
    w_mm: Optional[float],
    h_mm: Optional[float],
    **kwargs
) -> io.BytesIO:
    return make_overlay_pt(
        page_width_pt, page_height_pt, sig_img_path,
        mm_to_pt(x_mm), mm_to_pt(y_mm),
        w_pt=mm_to_pt(w_mm), h_pt=mm_to_pt(h_mm),
        **kwargs
    )

# ---------- sign function (points API) ----------
def sign_pdf_with_caption_pt(
    input_pdf: str,
    sig_img_path: str,
    *,
    page_index: int = -1,           # -1 = last page
    x_pt: float = 420.0,
    y_pt: float = 80.0,
    w_pt: Optional[float] = 180.0,
    h_pt: Optional[float] = None,
    signer: Optional[str] = None,
    reason: Optional[str] = None,
    show_date: bool = True,
    font_size: float = 6.0,
    caption_pos: str = CAPTION_POSITION_DEFAULT,
    caption_gap_pt: float = CAPTION_GAP_PT_DEFAULT,
    caption_overlap_pt: float = CAPTION_OVERLAP_PT_DEF,
    caption_dx_pt: float = CAPTION_DX_PT_DEFAULT,
    caption_dy_pt: float = CAPTION_DY_PT_DEFAULT,
    trim: bool = TRIM_DEFAULT,
    white_threshold: int = WHITE_THRESH_DEFAULT,
    out_path: Optional[str] = None,
    overwrite: bool = False,

    width_ratio: Optional[float] = 0.10,   # 이미지 폭 = 페이지 폭의 10%
    anchor: str = ANCHOR_DEFAULT,          # "bottom_right" 등
    margin_w_ratio: float = 0.04,          # 좌/우 여백 비율 (페이지 폭 기준)
    margin_h_ratio: float = 0.02,          # 상/하 여백 비율 (페이지 높이 기준)
    anchor_includes_caption: bool = True,  # True면 (이미지+캡션) 블록 전체를 앵커 기준으로 정렬
    caption_font_name: str = "Helvetica",
) -> str:
    """
    Stamp signature (image + tight caption) onto a PDF, save as *_signed.pdf (or out_path).
    """
    if not os.path.exists(input_pdf):
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")
    if not os.path.exists(sig_img_path):
        raise FileNotFoundError(f"Signature image not found: {sig_img_path}")

    reader = PdfReader(input_pdf)
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)

    n = len(reader.pages)
    if page_index < 0:
        page_index = n + page_index
    if page_index < 0 or page_index >= n:
        raise IndexError(f"Page {page_index} out of range for {n}-page PDF.")

    page = writer.pages[page_index]
    pw = float(page.mediabox.width)
    ph = float(page.mediabox.height)

    overlay_buf = make_overlay_pt(
        pw, ph, sig_img_path,
        x_pt, y_pt, w_pt=w_pt, h_pt=h_pt,
        signer=signer, reason=reason, show_date=show_date, font_size=font_size,
        caption_pos=caption_pos, caption_gap_pt=caption_gap_pt,
        caption_overlap_pt=caption_overlap_pt,
        caption_dx_pt=caption_dx_pt, caption_dy_pt=caption_dy_pt,
        trim=trim, white_threshold=white_threshold,
        width_ratio=width_ratio, anchor=anchor, margin_w_ratio=margin_w_ratio,
        margin_h_ratio=margin_h_ratio, anchor_includes_caption=anchor_includes_caption,
        caption_font_name=caption_font_name,
    )
    overlay_reader = PdfReader(overlay_buf)
    page.merge_page(overlay_reader.pages[0])

    if out_path is None:
        root, ext = os.path.splitext(input_pdf)
        out_path = f"{root}_signed{ext or '.pdf'}"
    if os.path.exists(out_path) and not overwrite:
        ts = time.strftime("%Y%m%d-%H%M%S")
        root, ext = os.path.splitext(out_path)
        out_path = f"{root}_{ts}{ext or '.pdf'}"

    logger.info(f"out_path = {out_path}")
    with open(out_path, "wb") as f:
        writer.write(f)
    return out_path

