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

    async def _request(self, method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None, allow_empty_body: bool = False) -> dict:
        headers = await self._auth_service.get_authenticated_headers()
        url = self._build_url(path)
        try:
            response = await self.http_client.request(method=method, url=url, headers=headers, params=params, json=json_body)
            response.raise_for_status()
            # Alguns endpoints (confirm, requestCancellation) retornam 202 com body vazio
            if not response.content or response.content.strip() == b'':
                logger.info("iFood API: %s %s -> HTTP %s (body vazio)", method, path, response.status_code)
                return {}
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
        """Solicita cancelamento de pedido no iFood (fluxo merchant).

        Endpoint: POST /order/v1.0/orders/{orderId}/requestCancellation
        Body: {"cancellationCode": "503", "reason": "503"}
        Resposta: 202 Accepted.
        """
        logger.info("[CANCELLATION] Solicitando cancelamento pedido %s - motivo: %s", order_id, reason)
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/requestCancellation", json_body={"cancellationCode": reason, "reason": reason})

    async def get_cancellation_reasons(self, order_id: str) -> list:
        """Busca motivos de cancelamento validos para um pedido.

        Endpoint: GET /order/v1.0/orders/{orderId}/cancellationReasons

        A resposta pode vir como:
        - Lista direta: ["OTHER", "ITEM_UNAVAILABLE", ...]
        - Dict com chave "reasons": {"reasons": [...]}
        """
        logger.info("[CANCELLATION] Buscando motivos de cancelamento para pedido %s", order_id)
        result = await self._request("GET", f"/order/v1.0/orders/{order_id}/cancellationReasons")
        logger.info("[CANCELLATION] Resposta cancellationReasons: %s", str(result)[:1000])
        # A resposta pode ser lista direta ou dict com "reasons"
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("reasons", [])
        return []

    async def merchant_request_cancellation(self, order_id: str, reason_code: str = "") -> dict:
        """Solicita cancelamento de pedido iniciado pelo MERCHANT (Odoo -> middleware).

        Endpoint: POST /order/v1.0/orders/{orderId}/requestCancellation
        Body: {"cancellationCode": "501", "reason": "501"}
        Resposta: 202 Accepted.

        IMPORTANTE: O campo 'reason' e obrigatorio alem de 'cancellationCode'.
        Se o pedido ja estiver cancelado, trata como sucesso.
        """
        if not reason_code:
            reason_code = "501"
        logger.info("[CANCELLATION] Merchant solicitando cancelamento pedido %s - motivo: %s", order_id, reason_code)
        body = {"cancellationCode": reason_code, "reason": reason_code}
        try:
            result = await self._request("POST", f"/order/v1.0/orders/{order_id}/requestCancellation", json_body=body)
            logger.info("[CANCELLATION] Solicitacao de cancelamento ENVIADA pedido %s (202 Accepted) - aguardando evento CANCELLED", order_id)
            return result
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and "already cancelled" in e.response.text.lower():
                logger.info("[CANCELLATION] Pedido %s ja esta cancelado no iFood - tratando como sucesso", order_id)
                return {"status": "already_cancelled", "orderId": order_id}
            logger.error("[CANCELLATION] FALHA ao solicitar cancelamento pedido %s: %s", order_id, e, exc_info=True)
            raise

    async def accept_cancellation(self, order_id: str) -> dict:
        """Aceita cancelamento SOLICITADO PELO CLIENTE (evento CANCELLATION_REQUESTED do iFood).

        Fluxo correto (obrigatorio para homologacao):
        1. Buscar motivos validos via GET /cancellationReasons
        2. Chamar POST /order/v1.0/orders/{orderId}/cancel com motivo valido

        NAO pode hardcode motivos - iFood exige buscar via API.

        IMPORTANTE: Se o pedido ja estiver cancelado (race condition com outro fluxo),
        trata como sucesso - o objetivo final (pedido cancelado) ja foi atingido.
        """
        logger.info("[CANCELLATION] Aceitando cancelamento pedido %s (solicitado pelo cliente)", order_id)
        try:
            # Passo 1: Buscar motivos de cancelamento validos
            logger.info("[CANCELLATION] Buscando motivos de cancelamento para pedido %s...", order_id)
            try:
                reasons = await self.get_cancellation_reasons(order_id)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400 and "already cancelled" in e.response.text.lower():
                    logger.info("[CANCELLATION] Pedido %s ja esta cancelado - nenhum acao necessaria (race condition ok)", order_id)
                    return {"status": "already_cancelled", "orderId": order_id}
                raise

            if not reasons:
                logger.warning("[CANCELLATION] Nenhum motivo retornado para pedido %s, usando 'OTHER'", order_id)
                reason_code = "OTHER"
            else:
                # Usar o primeiro motivo disponivel
                reason_code = reasons[0] if isinstance(reasons[0], str) else str(reasons[0].get("code", reasons[0].get("id", "OTHER")))
                logger.info("[CANCELLATION] Motivos disponiveis: %s | Selecionado: %s", reasons, reason_code)

            # Passo 2: Chamar endpoint de cancelamento com motivo valido
            logger.info("[CANCELLATION] Chamando POST /order/v1.0/orders/%s/cancel com reason=%s", order_id, reason_code)
            try:
                result = await self._request("POST", f"/order/v1.0/orders/{order_id}/cancel", json_body={"reason": reason_code})
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400 and "already cancelled" in e.response.text.lower():
                    logger.info("[CANCELLATION] Pedido %s ja esta cancelado no POST /cancel - sucesso", order_id)
                    return {"status": "already_cancelled", "orderId": order_id}
                raise
            logger.info("[CANCELLATION] Cancelamento SOLICITADO pedido %s - Resposta: %s", order_id, result)
            return result
        except Exception as e:
            logger.error("[CANCELLATION] FALHA cancelamento pedido %s: %s", order_id, e, exc_info=True)
            raise

    async def reject_cancellation(self, order_id: str, reason: str = "") -> dict:
        logger.info("[CANCELLATION] Rejeitando cancelamento pedido %s - Motivo: %s", order_id, reason)
        body = {"reason": reason} if reason else None
        return await self._request("POST", f"/order/v1.0/orders/{order_id}/cancellation/reject", json_body=body)

    async def acknowledge_cancellation(self, order_id: str) -> dict:
        """Confirma ao iFood que o evento CANCELLATION_REQUESTED foi recebido e processado.

        O acknowledgment do webhook e feito respondendo 202 ao webhook recebido.
        Nao existe um endpoint separado para isso - o 202 no webhook ja e o ack.
        Este metodo e um no-op que apenas loga para fins de auditoria.
        """
        logger.info("[CANCELLATION] Acknowledgment de cancelamento pedido %s - ja enviado via webhook 202 response", order_id)
        return {"status": "acknowledged", "orderId": order_id}

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
