import time
import logging
from typing import Optional

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class IFoodAuthService:
    """Handles OAuth 2.0 authentication with the iFood API.

    Uses client_credentials grant type to obtain bearer tokens.
    Tokens are cached and automatically refreshed when expired.
    """

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._http_client: Optional[httpx.AsyncClient] = None
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def get_token(self) -> dict:
        """Request a new access token from the iFood OAuth endpoint.

        Returns:
            Dictionary with access_token, expires_in, and token_type.
        """
        logger.info("Requesting new access token from iFood")
        logger.info("Auth URL: %s", self._config.ifood_auth_url)
        logger.info("Client ID: %s...%s", self._config.ifood_client_id[:8], self._config.ifood_client_id[-4:])

        # iFood OAuth usa parametros no body como form-urlencoded
        data = {
            "grantType": self._config.ifood_grant_type,
            "clientId": self._config.ifood_client_id,
            "clientSecret": self._config.ifood_client_secret,
        }

        try:
            response = await self.http_client.post(
                self._config.ifood_auth_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            # Log resposta completa em caso de erro
            if response.status_code != 200:
                logger.error("iFood OAuth response: HTTP %s - Body: %s",
                             response.status_code, response.text)
                logger.error("Request data: grantType=%s, clientId=%s",
                             self._config.ifood_grant_type, self._config.ifood_client_id)

            response.raise_for_status()
            token_data = response.json()

            # Log completo da resposta para debug
            logger.info("iFood token response keys: %s", list(token_data.keys()))

            # iFood pode retornar access_token ou accessToken
            token_value = (
                token_data.get("access_token")
                or token_data.get("accessToken")
                or token_data.get("token")
            )
            if not token_value:
                logger.error("Nenhum campo de token encontrado! Resposta: %s", token_data)
                raise ValueError(f"Campo de token nao encontrado na resposta: {list(token_data.keys())}")

            self._access_token = token_value
            expires_in = token_data.get("expiresIn") or token_data.get("expires_in") or 3600
            self._expires_at = time.time() + int(expires_in) - 60  # 60s buffer

            logger.info("Successfully obtained iFood access token (expires in %ds)", expires_in)

            return {
                "access_token": self._access_token,
                "expires_in": expires_in,
                "token_type": token_data.get("tokenType") or token_data.get("token_type") or "Bearer",
            }

        except httpx.HTTPStatusError as e:
            logger.error("Failed to get iFood token: HTTP %s - %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("Failed to connect to iFood auth endpoint: %s", e)
            raise

    async def _is_token_expired(self) -> bool:
        """Check if the cached token is expired or about to expire.

        Returns:
            True if the token is expired or not set.
        """
        if self._access_token is None:
            return True
        return time.time() >= self._expires_at

    async def get_authenticated_headers(self) -> dict:
        """Get HTTP headers with a valid Bearer token.

        Automatically refreshes the token if expired.

        Returns:
            Dictionary with Authorization header.
        """
        if await self._is_token_expired():
            await self.get_token()

        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            logger.debug("iFood auth HTTP client closed")

    async def __aenter__(self) -> "IFoodAuthService":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()
