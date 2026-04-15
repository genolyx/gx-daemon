import time
import httpx
import asyncio
import logging
from typing import Dict, Optional
from dataclasses import dataclass

from .config import settings

logger = logging.getLogger(__name__)

@dataclass
class TokenInfo:
    """토큰 정보를 담는 데이터 클래스"""
    token: str
    expiry: float

    @property
    def is_valid(self) -> bool:
        """토큰이 유효한지 확인 (30초 여유 두기)"""
        return time.time() < self.expiry - 30

    @property
    def is_expired(self) -> bool:
        """토큰이 만료되었는지 확인"""
        return not self.is_valid

@dataclass
class AuthTokens:
    """AWS 인증 토큰들을 담는 데이터 클래스"""
    access_token: TokenInfo
    refresh_token: Optional[str] = None
    
    @property
    def is_access_token_valid(self) -> bool:
        """accessToken이 유효한지 확인"""
        return self.access_token.is_valid
    
    @property
    def has_refresh_token(self) -> bool:
        """refreshToken이 있는지 확인"""
        return self.refresh_token is not None

class AuthClient:
    """AWS 인증 클라이언트 (accessToken/refreshToken 지원)"""

    def __init__(self):
        self._auth_tokens: Optional[AuthTokens] = None
        self._refresh_lock = asyncio.Lock()

    async def get_token(self, force_refresh: bool = False) -> str:
        """
        유효한 accessToken 획득
        
        Args:
            force_refresh: 강제로 토큰을 새로 발급받을지 여부
        """
        # 토큰이 없거나 강제 갱신 요청 시
        if not self._auth_tokens or force_refresh:
            return await self._perform_login()
        
        # accessToken이 유효하면 반환
        if self._auth_tokens.is_access_token_valid:
            return self._auth_tokens.access_token.token
        
        # accessToken이 만료되었으면 refreshToken으로 갱신 시도
        if self._auth_tokens.has_refresh_token:
            async with self._refresh_lock:
                # 다른 요청에서 이미 갱신했는지 재확인
                if self._auth_tokens and self._auth_tokens.is_access_token_valid:
                    return self._auth_tokens.access_token.token
                
                # refreshToken으로 갱신 시도
                success = await self._refresh_access_token()
                if success and self._auth_tokens:
                    return self._auth_tokens.access_token.token
        
        # refresh 실패 시 재로그인
        return await self._perform_login()

    async def _perform_login(self) -> str:
        """username/password로 최초 로그인 (네트워크 재시도 포함)"""
        logger.info("Performing login with username/password")
        
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        (settings.auth_url or "").strip(),
                        headers={"Content-Type": "application/json"},
                        json={
                            "username": (settings.api_username or "").strip(),
                            "password": (settings.api_password or "").strip()
                        },
                        timeout=10.0
                    )
                    response.raise_for_status()

                token_data = response.json()
                
                # accessToken 추출
                access_token = self._extract_access_token(token_data)
                expires_in = self._extract_expires_in(token_data)
                
                # refreshToken 추출 (선택사항)
                refresh_token = self._extract_refresh_token(token_data)
                
                self._auth_tokens = AuthTokens(
                    access_token=TokenInfo(
                        token=access_token,
                        expiry=time.time() + expires_in
                    ),
                    refresh_token=refresh_token
                )

                # 재시도 성공 로그 (첫 시도가 아닌 경우)
                if attempt > 0:
                    logger.info(f"NETWORK_RETRY_SUCCESS: Login succeeded after {attempt + 1} attempts")
                
                logger.info(f"Login successful (access expires in {expires_in}s, refresh: {'Yes' if refresh_token else 'No'})")
                return self._auth_tokens.access_token.token

            except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
                # 네트워크 오류 (DNS 실패, 연결 실패 등)
                last_error = e
                error_type = type(e).__name__
                
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 1초, 2초, 4초
                    logger.warning(
                        f"NETWORK_RETRY: {error_type} during login "
                        f"(attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"NETWORK_RETRY_FAILED: Login failed after {max_retries} attempts: {e}"
                    )
                    self._auth_tokens = None
                    raise
                    
            except Exception as e:
                # 인증 오류 등 다른 오류는 재시도하지 않음
                logger.error(f"Login failed: {e}")
                self._auth_tokens = None
                raise
        
        # 모든 재시도 실패
        if last_error:
            self._auth_tokens = None
            raise last_error
        
        raise RuntimeError("Unexpected error in login")

    async def _refresh_access_token(self) -> bool:
        """refreshToken으로 accessToken 갱신 (네트워크 재시도 포함)"""
        if not self._auth_tokens or not self._auth_tokens.has_refresh_token:
            logger.warning("No refresh token available for token refresh")
            return False
        
        logger.info("Refreshing access token using refresh token")
        
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # AWS refresh endpoint 호출
                refresh_url = f"{settings.platform_api_base}/auth/refresh"
                
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        refresh_url,
                        headers={"Content-Type": "application/json"},
                        json={
                            "refreshToken": self._auth_tokens.refresh_token
                        },
                        timeout=10.0
                    )
                    response.raise_for_status()

                token_data = response.json()
                
                # 새로운 accessToken 추출
                access_token = self._extract_access_token(token_data)
                expires_in = self._extract_expires_in(token_data)
                
                # 새로운 refreshToken이 있다면 업데이트
                new_refresh_token = self._extract_refresh_token(token_data)
                if new_refresh_token:
                    self._auth_tokens.refresh_token = new_refresh_token
                
                # accessToken 업데이트
                self._auth_tokens.access_token = TokenInfo(
                    token=access_token,
                    expiry=time.time() + expires_in
                )

                # 재시도 성공 로그 (첫 시도가 아닌 경우)
                if attempt > 0:
                    logger.info(f"NETWORK_RETRY_SUCCESS: Token refresh succeeded after {attempt + 1} attempts")
                
                logger.info(f"Token refresh successful (expires in {expires_in}s)")
                return True

            except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
                # 네트워크 오류 (DNS 실패, 연결 실패 등)
                last_error = e
                error_type = type(e).__name__
                
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 1초, 2초, 4초
                    logger.warning(
                        f"NETWORK_RETRY: {error_type} during token refresh "
                        f"(attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        f"NETWORK_RETRY_FAILED: Token refresh failed after {max_retries} attempts: {e}"
                    )
                    self._auth_tokens = None
                    return False
                    
            except Exception as e:
                # 인증 오류 등 다른 오류는 재시도하지 않음
                logger.error(f"Token refresh failed: {e}")
                # refresh 실패 시 토큰 정보 초기화
                self._auth_tokens = None
                return False
        
        # 모든 네트워크 재시도 실패
        if last_error:
            logger.error(f"Token refresh failed after all retries: {last_error}")
            self._auth_tokens = None
            return False
        
        return False

    def _extract_access_token(self, data: dict) -> str:
        """응답 데이터에서 accessToken 추출"""
        # data.data.accessToken 패턴 확인
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            token = (data["data"].get("accessToken") or
                    data["data"].get("access_token") or
                    data["data"].get("token"))
            if token:
                return token

        # 최상위 필드 확인
        token = (data.get("accessToken") or
                data.get("access_token") or
                data.get("token"))

        if not token:
            raise ValueError(f"Authentication response missing access token: {data}")

        return token

    def _extract_refresh_token(self, data: dict) -> Optional[str]:
        """응답 데이터에서 refreshToken 추출 (선택사항)"""
        # data.data.refreshToken 패턴 확인
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            refresh_token = (data["data"].get("refreshToken") or
                           data["data"].get("refresh_token"))
            if refresh_token:
                return refresh_token

        # 최상위 필드 확인
        refresh_token = (data.get("refreshToken") or
                        data.get("refresh_token"))

        return refresh_token  # None이어도 괜찮음

    def _extract_expires_in(self, data: dict) -> int:
        """응답 데이터에서 만료 시간 추출"""
        # data.data.expires_in 패턴 확인
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            expires_in = (data["data"].get("expires_in") or
                         data["data"].get("expiresIn"))
            if expires_in:
                return expires_in

        # 최상위 필드 확인
        expires_in = (data.get("expires_in") or
                     data.get("expiresIn"))

        return expires_in or 3600  # 기본값 1시간

    async def get_auth_headers(self) -> Dict[str, str]:
        """인증 헤더 생성"""
        token = await self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "GX-Daemon/1.0"
        }

    async def auth_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        인증이 포함된 HTTP 요청
        - 401 에러 시 토큰 갱신 후 재시도
        - 네트워크 에러 시 자동 재시도 (DNS 실패, 연결 실패 등)
        """
        max_network_retries = 3
        last_network_error = None
        
        # 네트워크 오류 재시도 루프
        for attempt in range(max_network_retries):
            try:
                # HTTP 요청 시도
                response = await self._make_request(method, url, **kwargs)
                
                # 401 에러 시 토큰 갱신 후 재시도
                if response.status_code == 401:
                    logger.info("Received 401, attempting token refresh and retry")
                    
                    # 토큰 갱신 (refreshToken 또는 재로그인)
                    async with self._refresh_lock:
                        try:
                            # 새 토큰 획득
                            await self.get_token(force_refresh=True)
                            
                            # 새 토큰으로 재시도
                            response = await self._make_request(method, url, **kwargs)
                            
                        except Exception as e:
                            logger.error(f"Token refresh and retry failed: {e}")
                            # 원래 401 응답 반환
                            response.raise_for_status()
                            return response
                
                # 네트워크 재시도 성공 로그 (첫 시도가 아닌 경우)
                if attempt > 0:
                    logger.info(f"NETWORK_RETRY_SUCCESS: Request to {url} recovered after {attempt + 1} attempts")
                
                response.raise_for_status()
                return response
                
            except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
                # OSError: DNS 실패 ([Errno -2] Name or service not known 등)
                # ConnectError: 연결 실패
                # TimeoutException: 타임아웃
                
                last_network_error = e
                error_type = type(e).__name__
                
                if attempt < max_network_retries - 1:
                    # 아직 재시도 가능
                    wait_time = 2 ** attempt  # Exponential backoff: 1초, 2초, 4초
                    logger.warning(
                        f"NETWORK_RETRY: {error_type} for {method} {url} "
                        f"(attempt {attempt + 1}/{max_network_retries}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # 모든 재시도 실패
                    logger.error(
                        f"NETWORK_RETRY_FAILED: {error_type} for {method} {url} "
                        f"after {max_network_retries} attempts: {e}"
                    )
                    raise
        
        # 여기 도달하면 모든 시도 실패 (이론적으로는 위의 raise에서 처리됨)
        if last_network_error:
            raise last_network_error
        
        # Fallback (도달하지 않아야 함)
        raise RuntimeError(f"Unexpected error in auth_request for {url}")

    async def _make_request(self, method: str, url: str, force_refresh: bool = False, **kwargs) -> httpx.Response:
        """실제 HTTP 요청 수행"""
        token = await self.get_token(force_refresh=force_refresh)

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        timeout = kwargs.pop("timeout", 10.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, headers=headers, **kwargs)

        if response.status_code >= 400:
            logger.error(f"[{response.status_code}] Error Response from server: {response.text}")

        # 여기서는 raise_for_status()를 호출하지 않음 (상위에서 처리)
        return response

    async def logout(self):
        """세션 종료 및 토큰 무효화"""
        if not self._auth_tokens:
            logger.debug("No active session to logout")
            return

        try:
            logout_url = f"{settings.platform_api_base}/auth/logout"

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    logout_url,
                    headers={"Authorization": f"Bearer {self._auth_tokens.access_token.token}"}
                )

            if response.status_code == 200:
                logger.info("Successfully logged out from server")
            else:
                logger.warning(f"Server logout failed: {response.status_code}")

        except Exception as e:
            logger.warning(f"Logout error: {e}")

        finally:
            # 로컬 토큰 정리 (서버 에러가 발생해도 실행)
            self._auth_tokens = None
            logger.debug("Local session cleared")

    def clear_token(self):
        """토큰 강제 클리어 (에러 발생 시 사용)"""
        self._auth_tokens = None
        logger.debug("All tokens forcefully cleared")

    @property
    def has_valid_token(self) -> bool:
        """유효한 accessToken이 있는지 확인"""
        return (self._auth_tokens is not None and 
                self._auth_tokens.is_access_token_valid)

    @property
    def has_refresh_token(self) -> bool:
        """refreshToken이 있는지 확인"""
        return (self._auth_tokens is not None and 
                self._auth_tokens.has_refresh_token)

    @property
    def token_expires_in(self) -> Optional[int]:
        """accessToken 만료까지 남은 시간(초)"""
        if not self._auth_tokens:
            return None
        return max(0, int(self._auth_tokens.access_token.expiry - time.time()))

# 전역 인스턴스
_auth_client = AuthClient()

# 하위 호환성을 위한 함수들
async def get_token(force: bool = False) -> str:
    """토큰 획득 (하위 호환성)"""
    return await _auth_client.get_token(force_refresh=force)

async def get_auth_headers() -> Dict[str, str]:
    """인증 헤더 생성 (하위 호환성)"""
    return await _auth_client.get_auth_headers()

async def auth_request(method: str, url: str, **kwargs) -> httpx.Response:
    """인증 요청 (하위 호환성)"""
    return await _auth_client.auth_request(method, url, **kwargs)

async def logout():
    """로그아웃 (하위 호환성)"""
    await _auth_client.logout()

def clear_token():
    """토큰 클리어 (하위 호환성)"""
    _auth_client.clear_token()

# 추가 유틸리티 함수들
def has_valid_token() -> bool:
    """유효한 토큰 존재 여부 확인"""
    return _auth_client.has_valid_token

def has_refresh_token() -> bool:
    """refreshToken 존재 여부 확인 (새로 추가)"""
    return _auth_client.has_refresh_token

def get_token_expires_in() -> Optional[int]:
    """토큰 만료까지 남은 시간"""
    return _auth_client.token_expires_in
