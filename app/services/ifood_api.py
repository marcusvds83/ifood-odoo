import logging
from typing import Optional

import httpx

from app.config import Settings
from app.services.ifood_auth import IFoodAuthService

logger = logging.getLogger(__name__)


class IFoodAPIClient:
    """Client for interacting with the iFood Merchant API.

    Provides methods for managing orders, catalog, and financial data.
    Authentication is handled automatically via IFoodAuthService.
    """

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._auth_service = IFoodAuthService(config)
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    def _build_url(self, path: str) -> str:
        """Build a full API URL from a path."""
        return f"{self._config.ifood_api_base_url}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated request to the iFood API.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., /order/v1.0/orders)
            params: Query parameters
            json_body: JSON request body

        Returns:
            Response JSON as dictionary
        """
        headers = await self._auth_service.get_authenticated_headers()
        url = self._build_url(path)

        try:
            response = await self.http_client.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_body,
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            logger.error(
                "iFood API error: %s %s -> HTTP %s - %s",
                method, path, e.response.status_code, e.response.text,
            )
            raise
        except httpx.RequestError as e:
            logger.error("iFood API connection error: %s %s -> %s", method, path, e)
            raise

    # ── Merchant ───────────────────────────────────────────────

    async def get_merchant_id(self) -> str:
        """Get the first merchant ID associated with the authenticated client.

        Returns:
            The merchant ID string.
        """
        merchants = await self._request("GET", "/merchant/v1.0/merchants")
        if not merchants:
            raise ValueError("No merchants found for this client")

        merchant_id = merchants[0]["id"]
        logger.info("Retrieved merchant ID: %s", merchant_id)
        return merchant_id

    # ── Orders ─────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> dict:
        """Get details of a specific order.

        Args:
            order_id: The iFood order ID.

        Returns:
            Order details dictionary.
        """
        logger.info("Fetching order %s from iFood", order_id)
        return await self._request("GET", f"/order/v1.0/orders/{order_id}")

    async def get_orders(self, params: Optional[dict] = None) -> dict:
        """Get a list of orders with optional filters.

        Args:
            params: Query parameters such as dateStart, dateEnd, states, page, etc.

        Returns:
            Orders list dictionary.
        """
        logger.info("Fetching orders from iFood with params: %s", params)
        return await self._request("GET", "/order/v1.0/orders", params=params)

    async def confirm_order(self, order_id: str) -> dict:
        """Confirm an order on iFood.

        Args:
            order_id: The iFood order ID.

        Returns:
            Confirmation response.
        """
        logger.info("Confirming order %s on iFood", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/confirm")

    async def start_preparation(self, order_id: str) -> dict:
        """Mark an order as in preparation.

        Args:
            order_id: The iFood order ID.

        Returns:
            Response from iFood.
        """
        logger.info("Starting preparation for order %s", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/startPreparation")

    async def ready_for_pickup(self, order_id: str) -> dict:
        """Mark an order as ready for pickup.

        Args:
            order_id: The iFood order ID.

        Returns:
            Response from iFood.
        """
        logger.info("Marking order %s as ready for pickup", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/readyToPickup")

    async def dispatch_order(self, order_id: str) -> dict:
        """Dispatch an order (hand it to the delivery person).

        Args:
            order_id: The iFood order ID.

        Returns:
            Response from iFood.
        """
        logger.info("Dispatching order %s", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/dispatch")

    async def request_cancellation(self, order_id: str, reason: str) -> dict:
        """Request cancellation of an order.

        Args:
            order_id: The iFood order ID.
            reason: Cancellation reason string.

        Returns:
            Response from iFood.
        """
        logger.info("Requesting cancellation for order %s, reason: %s", order_id, reason)
        return await self._request(
            "POST",
            f"/order/v1.0/orders/{order_id}/requestCancellation",
            json_body={"reason": reason},
        )

    # ── Catalog ────────────────────────────────────────────────

    async def get_catalog(self, merchant_id: str) -> dict:
        """Get the catalog for a merchant.

        Args:
            merchant_id: The merchant ID.

        Returns:
            Catalog data dictionary.
        """
        logger.info("Fetching catalog for merchant %s", merchant_id)
        return await self._request("GET", f"/catalog/v1.0/merchants/{merchant_id}/catalog")

    # ── Financial ──────────────────────────────────────────────

    async def get_financial(self, merchant_id: str, params: Optional[dict] = None) -> dict:
        """Get financial extracts for a merchant.

        Args:
            merchant_id: The merchant ID.
            params: Optional query parameters (dateStart, dateEnd, etc.)

        Returns:
            Financial data dictionary.
        """
        logger.info("Fetching financial data for merchant %s", merchant_id)
        return await self._request(
            "GET",
            f"/financial/v1.0/merchants/{merchant_id}/financialExtracts",
            params=params,
        )

    # ── Lifecycle ──────────────────────────────────────────────

    async def close(self) -> None:
        """Close HTTP clients."""
        await self._auth_service.close()
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            logger.debug("iFood API HTTP client closed")

    async def __aenter__(self) -> "IFoodAPIClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()
