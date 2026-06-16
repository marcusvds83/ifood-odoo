import logging
from typing import Optional
import httpx
from app.config import Settings
from app.services.ifood_auth import IFoodAuthService

logger = logging.getLogger(__name__)

class IFoodAPIClient:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._auth_service = IFoodAuthService(config)
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    def _build_url(self, path: str) -> str:
        return f"{self._config.ifood_api_base_url}{path}"

    async def _request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> dict:
        headers = await self._auth_service.get_authenticated_headers()
        url = self._build_url(path)
        try:
            response = await self.http_client.request(method=method, url=url, headers=headers, params=params, json=json_body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("iFood API error: %s %s -> HTTP %s - %s", method, path, e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("iFood API connection error: %s %s -> %s", method, path, e)
            raise

    async def get_merchant_id(self) -> str:
        merchants = await self._request("GET", "/merchant/v1.0/merchants")
        if not merchants:
            raise ValueError("No merchants found")
        return merchants[0]["id"]

    async def get_order(self, order_id: str) -> dict:
        logger.info("Fetching order %s from iFood", order_id)
        return await self._request("GET", f"/order/v1.0/orders/{order_id}")

    async def get_orders(self, params: Optional[dict] = None) -> dict:
        return await self._request("GET", "/order/v1.0/orders", params=params)

    async def confirm_order(self, order_id: str) -> dict:
        logger.info("Confirming order %s on iFood", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/confirm")

    async def start_preparation(self, order_id: str) -> dict:
        logger.info("Starting preparation for order %s", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/startPreparation")

    async def ready_for_pickup(self, order_id: str) -> dict:
        logger.info("Marking order %s as ready for pickup", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/readyToPickup")

    async def dispatch_order(self, order_id: str) -> dict:
        logger.info("Dispatching order %s", order_id)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/dispatch")

    async def request_cancellation(self, order_id: str, reason: str) -> dict:
        logger.info("Requesting cancellation for order %s, reason: %s", order_id, reason)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/requestCancellation", json_body={"reason": reason})

    async def accept_cancellation(self, order_id: str, reason_code: str = "") -> dict:
        """Aceita/solicita cancelamento de pedido no iFood.

        Usado tanto para cancelamento solicitado pelo cliente (CAN webhook)
        quanto para cancelamento iniciado pelo merchant (Odoo -> middleware).

        Args:
            order_id: ID do pedido no iFood.
            reason_code: Codigo do motivo (501-509), opcional.
        """
        logger.info("[CANCELLATION] Aceitando cancelamento pedido %s - motivo: %s", order_id, reason_code)
        body = None
        if reason_code:
            body = {"cancellationCode": reason_code}
        try:
            result = await self._request("POST", f"/order/v1.0/orders/{order_id}/cancellation/accept", json_body=body)
            logger.info("[CANCELLATION] Cancelamento ACEITO pedido %s - Resposta: %s", order_id, result)
            return result
        except Exception as e:
            logger.error("[CANCELLATION] FALHA cancelamento pedido %s: %s", order_id, e, exc_info=True)
            raise

    async def reject_cancellation(self, order_id: str, reason: str = "") -> dict:
        logger.info("[CANCELLATION] Rejeitando cancelamento pedido %s - Motivo: %s", order_id, reason)
        body = {"reason": reason} if reason else None
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/cancellation/reject", json_body=body)

    async def get_catalog(self, merchant_id: str) -> dict:
        return await self._request("GET", f"/catalog/v1.0/merchants/{merchant_id}/catalog")

    async def get_financial(self, merchant_id: str, params: Optional[dict] = None) -> dict:
        return await self._request("GET", f"/financial/v1.0/merchants/{merchant_id}/financialExtracts", params=params)

    async def close(self) -> None:
        await self._auth_service.close()
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def __aenter__(self) -> "IFoodAPIClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
