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
                logger.warning("[CANCELLATION] Pedido %s pode ja estar cancelado (400 ao buscar motivos): %s", order_id, e.response.text[:200])
                return []
            raise

    async def request_cancellation(self, order_id: str, reason: str = "") -> dict:
        """Solicita cancelamento de pedido no iFood.

        Este e o UNICO endpoint de cancelamento da API oficial iFood:
        POST /order/v1.0/orders/{orderId}/requestCancellation
        Body: {"cancellationCode": "codigo_do_motivo"}
        Resposta: 202 Accepted
        Resultado real chega via polling como evento CANCELLED.

        Usado tanto para cancelamento solicitado pelo cliente (CAN webhook)
        quanto para cancelamento iniciado pelo merchant (Odoo -> middleware).

        Args:
            order_id: ID do pedido no iFood.
            reason: Codigo do motivo (ex: "501"). Se vazio, busca via API.
        """
        if not reason:
            # Buscar motivos disponiveis via API
            try:
                reasons = await self.get_cancellation_reasons(order_id)
                if reasons:
                    # reasons e uma lista de dicts com "code" ou lista de strings
                    first = reasons[0]
                    if isinstance(first, dict):
                        reason = str(first.get("code", "501"))
                    else:
                        reason = str(first)
                else:
                    reason = "501"  # Default: Erro no sistema
            except Exception as e:
                logger.warning("[CANCELLATION] Nao foi possivel buscar motivos, usando default 501: %s", e)
                reason = "501"

        logger.info("[CANCELLATION] Solicitando cancelamento pedido %s - motivo: %s", order_id, reason)
        try:
            result = await self._request("POST", f"/order/v1.0/orders/{order_id}/requestCancellation", json_body={"cancellationCode": reason})
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

    async def acknowledge_cancellation_requested(self, order_id: str, cancellation_code: str = "") -> dict:
        """Confirma ao iFood que aceitamos o cancelamento solicitado pelo cliente.

        Estrategia de fallback:
        1. Tenta POST /requestCancellation (endpoint OFICIAL que funciona)
        2. Se falhar, tenta buscar motivos e repete
        3. Trata erros de pedido ja cancelado como sucesso

        O /statuses/cancellationRequested NAO EXISTE na API real (retorna 404).
        O unico endpoint funcional e /requestCancellation.
        """
        logger.info("[CANCELLATION] Aceitando cancelamento pedido %s - codigo: %s", order_id, cancellation_code)

        # Se nao recebeu codigo, buscar motivos disponiveis
        if not cancellation_code:
            try:
                reasons = await self.get_cancellation_reasons(order_id)
                if reasons:
                    first = reasons[0]
                    cancellation_code = first.get("code", "501") if isinstance(first, dict) else str(first)
                else:
                    cancellation_code = "501"
            except Exception as e:
                logger.warning("[CANCELLATION] Nao foi possivel buscar motivos: %s, usando 501", e)
                cancellation_code = "501"

        try:
            result = await self._request(
                "POST",
                f"/order/v1.0/orders/{order_id}/requestCancellation",
                json_body={"cancellationCode": cancellation_code}
            )
            logger.info("[CANCELLATION] Cancelamento ACEITO pedido %s via /requestCancellation: %s", order_id, str(result)[:500])
            return result
        except httpx.HTTPStatusError as e:
            # Pedido ja cancelado = sucesso
            if e.response.status_code == 400 and ("already cancelled" in e.response.text.lower() or "já está cancelado" in e.response.text.lower()):
                logger.info("[CANCELLATION] Pedido %s ja esta cancelado - tratando como sucesso", order_id)
                return {"status": "already_cancelled", "orderId": order_id}
            # Cancelamento em andamento = sucesso
            if e.response.status_code == 409:
                logger.info("[CANCELLATION] Pedido %s ja tem cancelamento em andamento - tratando como sucesso", order_id)
                return {"status": "cancellation_in_progress", "orderId": order_id}
            raise

    async def accept_cancellation(self, order_id: str, reason_code: str = "") -> dict:
        """Aceita cancelamento de pedido no iFood.

        Usa o endpoint /requestCancellation (unico endpoint real de cancelamento).
        Funciona tanto para evento CAN (customer-initiated) quanto para merchant-initiated.
        """
        return await self.request_cancellation(order_id, reason=reason_code)

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

        Formato do body (codigo Python oficial iFood):
          {"events": [{"id": "event_id1"}, {"id": "event_id2"}]}

        Endpoint: POST /events/acknowledgment (URL oficial iFood)
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
            # Body correto (codigo Python oficial iFood): {"events": [{"id": eid}, ...]}
            body = {"events": [{"id": eid} for eid in chunk]}
            logger.info("[ACK] Enviando acknowledgment para %d evento(s) - URL: /events/acknowledgment", len(chunk))

            try:
                result = await self._request("POST", "/events/acknowledgment", json_body=body)
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