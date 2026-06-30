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

    async def _request(self, method: str, path: str, params: Optional[dict] = None, json_body = None) -> dict:
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

    async def get_cancellation_reasons(self, order_id: str) -> list:
        """Busca motivos de cancelamento disponiveis para o pedido.

        GET /order/v1.0/orders/{orderId}/cancellationReasons
        Retorna: {"reasons": [{"code": "501", "description": "..."}, ...]}
        """
        logger.info("[CANCELLATION] Buscando motivos de cancelamento para pedido %s", order_id)
        try:
            result = await self._request("GET", f"/order/v1.0/orders/{order_id}/cancellationReasons")
            # A API pode retornar {"reasons": [...]} ou a lista direta
            if isinstance(result, dict) and "reasons" in result:
                reasons = result["reasons"]
            elif isinstance(result, list):
                reasons = result
            else:
                reasons = []
            logger.info("[CANCELLATION] Motivos encontrados: %s", reasons)
            return reasons
        except httpx.HTTPStatusError as e:
            # Se o pedido ja esta cancelado, a API retorna 400
            if e.response.status_code == 400:
                logger.warning("[CANCELLATION] Pedido %s - 400 ao buscar motivos (pedido ja cancelado?): %s", order_id, e.response.text[:500])
                return []
            raise

    async def request_cancellation(self, order_id: str, cancellation_code: str = "", reason_desc: str = "") -> dict:
        """Solicita cancelamento de pedido no iFood.

        Doc oficial iFood:
          POST /order/v1.0/orders/{orderId}/requestCancellation
          Body: {"cancellationCode": "501", "reason": "Erro no sistema"}
          Response: 202 Accepted

        GET /cancellationReasons retorna:
          [{"description": "Erro no sistema", "cancelCodeId": "501"}, ...]

        Args:
            order_id: ID do pedido no iFood.
            cancellation_code: Codigo do motivo (ex: "501"). Se vazio, busca via API.
            reason_desc: Descricao do motivo. Se vazio, busca via API ou usa padrao.
        """
        if not cancellation_code:
            # Buscar motivos disponiveis via API
            try:
                reasons = await self.get_cancellation_reasons(order_id)
                if reasons:
                    first = reasons[0]
                    if isinstance(first, dict):
                        cancellation_code = str(first.get("cancelCodeId", "501"))
                        reason_desc = str(first.get("description", cancellation_code))
                    else:
                        cancellation_code = str(first)
                        reason_desc = cancellation_code
                else:
                    cancellation_code = "501"
                    reason_desc = "Erro no sistema"
            except Exception as e:
                logger.warning("[CANCELLATION] Nao foi possivel buscar motivos, usando default 501: %s", e)
                cancellation_code = "501"
                reason_desc = "Erro no sistema"

        if not reason_desc:
            reason_desc = cancellation_code

        # Body conforme doc oficial: AMBOS os campos sao obrigatórios
        body = {"cancellationCode": str(cancellation_code), "reason": str(reason_desc)}
        logger.info("[CANCELLATION] Solicitando cancelamento pedido %s - code: %s, reason: %s", order_id, cancellation_code, reason_desc)
        try:
            result = await self._request("POST", f"/order/v1.0/orders/{order_id}/requestCancellation", json_body=body)
            logger.info("[CANCELLATION] Cancelamento solicitado pedido %s - Resposta: %s", order_id, str(result)[:500])
            return result
        except httpx.HTTPStatusError as e:
            # Se o pedido ja foi cancelado (race condition), trata como sucesso
            if e.response.status_code == 400 and ("already cancelled" in e.response.text.lower() or "já está cancelado" in e.response.text.lower()):
                logger.info("[CANCELLATION] Pedido %s ja esta cancelado - tratando como sucesso", order_id)
                return {"status": "already_cancelled", "orderId": order_id}
            if e.response.status_code == 409:
                logger.info("[CANCELLATION] Pedido %s ja tem cancelamento em andamento - tratando como sucesso", order_id)
                return {"status": "cancellation_in_progress", "orderId": order_id}
            raise

    async def accept_cancellation(self, order_id: str, reason_code: str = "", reason_desc: str = "") -> dict:
        """Aceita cancelamento de pedido no iFood.

        Usa o endpoint /requestCancellation com body conforme doc oficial:
          {"cancellationCode": "501", "reason": "Erro no sistema"}

        Funciona tanto para evento CAN (customer-initiated) quanto para merchant-initiated.
        Se reason_code vazio, busca motivos via GET /cancellationReasons.
        """
        return await self.request_cancellation(order_id, cancellation_code=reason_code, reason_desc=reason_desc)

    # ── Event Polling & Acknowledgment (obrigatorio para homologacao) ──
    # Docs: https://developer.ifood.com.br/en-US/docs/guides/modules/events/polling-overview
    # GET /events/v1.0/events:polling — retrieves new events
    # POST /events/v1.0/events/acknowledgment — confirms receipt (body: list of event IDs, max 2000)

    async def poll_events(self) -> list:
        """Busca novos eventos via polling (modulo Events do iFood).

        GET /events/v1.0/events:polling
        Deve ser chamado a cada 30 segundos.
        Retorna lista de eventos pendentes.
        Sem eventos = 204 No Content.
        """
        logger.info("[POLLING] Buscando eventos via GET /events/v1.0/events:polling...")
        try:
            headers = await self._auth_service.get_authenticated_headers()
            url = self._build_url("/events/v1.0/events:polling")
            response = await self.http_client.get(url, headers=headers, timeout=30.0)
            logger.info("[POLLING] Response: HTTP %s | Body len: %s", response.status_code, len(response.content))
            if response.status_code == 204 or not response.content or response.content.strip() == b'':
                logger.debug("[POLLING] Nenhum evento pendente (204/vazio)")
                return []
            response.raise_for_status()
            data = response.json()
            events = data if isinstance(data, list) else data.get("events", data.get("orders", [data]))
            logger.info("[POLLING] %d evento(s) recebido(s)", len(events) if isinstance(events, list) else 1)
            return events if isinstance(events, list) else [events]
        except httpx.HTTPStatusError as e:
            logger.error("[POLLING] Erro: GET /events:polling -> HTTP %s - %s", e.response.status_code, e.response.text[:500])
            return []
        except Exception as e:
            logger.error("[POLLING] Erro inesperado: %s", e)
            return []

    async def acknowledge_events(self, event_ids: list) -> dict:
        """Confirma ao iFood que os eventos foram processados.

        Formato do body (TESTADO E CONFIRMADO 22/06 - retornou 202):
          [{"id": "event_id1"}, {"id": "event_id2"}]

        Endpoint: POST /events/v1.0/events/acknowledgment
        Max 2000 IDs por request.
        Eventos nao confirmados voltam no proximo polling.
        """
        if not event_ids:
            logger.debug("[ACK] Nenhum evento para acknowledge")
            return {}

        # Garantir max 2000 IDs por request (limite da API)
        chunks = [event_ids[i:i+2000] for i in range(0, len(event_ids), 2000)]
        last_result = {}

        for chunk in chunks:
            # Body correto (TESTADO 22/06 - 202 Accepted): [{"id": eid}, ...]
            body = [{"id": eid} for eid in chunk]
            logger.info("[ACK] Enviando acknowledgment para %d evento(s) - formato: [{\"id\": ...}]", len(chunk))

            try:
                result = await self._request("POST", "/events/v1.0/events/acknowledgment", json_body=body)
                logger.info("[ACK] Acknowledgment OK: %s", str(result)[:300])
                last_result = result
            except httpx.HTTPStatusError as e:
                logger.error("[ACK] Falha acknowledgment: HTTP %s - %s",
                             e.response.status_code, e.response.text[:300])

        return last_result

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