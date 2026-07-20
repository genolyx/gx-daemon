"""
Logging Configuration

기존 nipt-daemon의 logging_config.py를 일반화합니다.
"""

import collections
import logging
import threading
import time
from datetime import datetime
from colorlog import ColoredFormatter
from fastapi import Request

from .config import settings
from .datetime_kst import KST

log = logging.getLogger(__name__)

# ─── In-memory ring buffer ────────────────────────────────────────────────────
_LOG_BUFFER_MAX = 500   # keep last N lines
_log_buffer: collections.deque = collections.deque(maxlen=_LOG_BUFFER_MAX)
_log_buffer_lock = threading.Lock()


class _MemoryLogHandler(logging.Handler):
    """Appends plain-text log lines to the in-memory ring buffer."""

    PLAIN_FMT = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s ─ %(message)s",
        datefmt="%H:%M:%S",
    )
    PLAIN_FMT.converter = lambda s: datetime.fromtimestamp(s, tz=KST).timetuple()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.PLAIN_FMT.format(record)
            with _log_buffer_lock:
                _log_buffer.append(line)
        except Exception:
            pass


def get_log_lines(last_n: int | None = None, since_seq: int | None = None):
    """Return (lines, next_seq).

    *since_seq*: if given, only return entries appended after that sequence
    number (monotonic counter of total lines ever appended — approximated by
    buffer length + offset stored alongside).
    """
    with _log_buffer_lock:
        lines = list(_log_buffer)
    if last_n and last_n > 0:
        lines = lines[-last_n:]
    return lines



def _kst_log_converter(seconds: float):
    return datetime.fromtimestamp(seconds, tz=KST).timetuple()


def setup_logging():
    """로깅 설정 초기화"""
    logging_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    formatter = ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)s %(filename)s:%(lineno)d ─ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        reset=True,
        log_colors={
            "DEBUG": "cyan",
            "INFO": "white",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red",
        },
        secondary_log_colors={},
        style="%",
    )
    formatter.converter = _kst_log_converter

    logging.basicConfig(
        level=logging_level,
        format="%(asctime)s %(levelname)s %(name)s [%(filename)s:%(lineno)d] %(message)s"
    )

    handler = logging.StreamHandler()
    handler.setLevel(logging_level)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging_level)
    root_logger.handlers = [handler]

    # In-memory buffer handler (for /daemon-log API)
    mem_handler = _MemoryLogHandler()
    mem_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(mem_handler)

    # httpx 로그 레벨 설정
    logging.getLogger("httpx").setLevel(logging.WARNING)



# ─── HTTP access log ANSI colors ─────────────────────────────────────────────
# 터미널 및 log 파일(cat/less -R)에서 색상 구분
_R  = "\033[0m"          # reset
_RQ = "\033[1;96m"       # bold bright-cyan  : Portal → daemon  (request)
_RS = "\033[1;92m"       # bold bright-green : daemon → Portal  (response)
_RE = "\033[1;91m"       # bold bright-red   : 4xx/5xx error response

_SKIP_PATHS = {"/health", "/queue/summary", "/queue/status", "/favicon.ico"}
_SKIP_UA_FRAGMENTS = {"hello world", "curl", "wget", "bot", "crawler", "spider", "scanner"}
_LOG_BODY_METHODS = {"POST", "PATCH", "PUT"}
_BODY_PREVIEW_LEN = 800  # DEBUG_HTTP 시 body 최대 출력 길이


def setup_middleware(app):
    """HTTP 요청/응답 로깅 미들웨어.

    DEBUG_HTTP=true 일 때:
      - Portal → daemon 요청: cyan (method, path, client, body 미리보기)
      - daemon → Portal 응답: green (status, 처리시간)
      - 4xx/5xx: red

    DEBUG_HTTP=false (기본) 일 때:
      - 기존 한 줄 요약 (INFO)
    """

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        path = request.url.path
        user_agent = request.headers.get("user-agent", "").lower()

        should_skip = (
            path in _SKIP_PATHS
            or any(f in user_agent for f in _SKIP_UA_FRAGMENTS)
            or (path == "/" and request.client and
                request.client.host not in ("127.0.0.1", "localhost", "::1"))
        )

        path_qs = f"{path}?{request.url.query}" if request.url.query else path
        peer = (
            f"{request.client.host}:{request.client.port}"
            if request.client else "-"
        )

        start_time = time.time()

        if should_skip:
            response = await call_next(request)
            elapsed = time.time() - start_time
            log.debug("skip %s %s from %s", request.method, path, peer)
            return response

        # ── DEBUG_HTTP: 상세 모드 ────────────────────────────────
        if settings.debug_http:
            body_preview = ""
            if request.method in _LOG_BODY_METHODS:
                try:
                    raw = await request.body()   # Starlette가 내부적으로 캐시
                    if raw:
                        text = raw.decode("utf-8", errors="replace")
                        body_preview = text[:_BODY_PREVIEW_LEN]
                        if len(text) > _BODY_PREVIEW_LEN:
                            body_preview += f" …(+{len(text)-_BODY_PREVIEW_LEN})"
                except Exception:
                    pass

            req_line = f"{_R}{_RQ}→ {request.method} {path_qs}  [{peer}]{_R}"
            if body_preview:
                req_line += f"\n{_RQ}  body: {body_preview}{_R}"
            log.info(req_line)

            response = await call_next(request)
            elapsed = time.time() - start_time

            sc = response.status_code
            color = _RE if sc >= 400 else _RS
            log.info(
                "%s← %s %s %s  (%.3fs)%s",
                _R + color, sc, request.method, path_qs, elapsed, _R,
            )
        else:
            # ── 기본 모드: 기존 한 줄 요약 ──────────────────────
            response = await call_next(request)
            elapsed = time.time() - start_time
            log.info(
                "%s %s -> %s %.3fs %s",
                request.method, path_qs, response.status_code, elapsed, peer,
            )

        return response
