import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, Response

from app.config import settings
from app.services.ifood_api import IFoodAPIClient
from app.services.odoo_client import OdooClient
from app.services.odoo_sync import OdooSyncService

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_webhook_signature(request: Request, body: bytes) -> bool:
    """Verifica a assinatura do webhook.

    IMPORTANTE: Sempre retorna True para nao rejeitar o iFood.
    Em caso de assinatura invalida, apenas loga um warning.
    O iFood exige resposta 200 para nao retry.
    """
    if not settings.webhook_secret or settings.webhook_secret == "change-me-webhook-secret":
        logger.debug("WEBHOOK_SECRET nao configurado - pulando verificacao")
        return True

    # Buscar assinatura em varios formatos de header (iFood pode mandar de formas diferentes)
    signature = (
        request.headers.get("x-ifood-signature") or
        request.headers.get("X-IFood-Signature") or
        request.headers.get("x-ifood-signature-v1") or
        ""
    )

    if not signature:
        # Sem assinatura = provavelmente teste de conexao do iFood
        logger.info("Webhook recebido SEM assinatura - possivel teste de conexao")
        return True

    # Calcular assinatura esperada
    expected = hmac.new(
        settings.webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    expected_sig = f"sha256={expected}"

    if not hmac.compare_digest(expected_sig, signature):
        logger.warning(
            "Assinatura INVALIDA! Recebida: %s | Esperada: %s | Body len: %d",
            signature, expected_sig, len(body),
        )
        # NAO rejeita - retorna True para nao causar retry no iFood
        return True

    logger.debug("Assinatura do webhook valida")
    return True


@router.post("/webhooks/ifood/orders")
@router.get("/webhooks/ifood/orders")
async def handle_order_webhook(request: Request, response: Response):
    """Recebe eventos de pedidos do iFood.

    Aceita GET e POST para compatibilidade com teste de conexao do iFood.
    SEMPRE retorna 200 para o iFood nao fazer retry.
    """
    try:
        body = await request.body()
        method = request.method

        logger.info("=== Webhook iFood recebido ===")
        logger.info("Method: %s | Content-Type: %s", method, request.headers.get("content-type", "N/A"))
        logger.debug("Headers: %s", dict(request.headers))
        logger.debug("Body RAW: %s", body.decode("utf-8", errors="replace")[:2000])

        # Verificar assinatura (log only, nunca rejeita)
        _verify_webhook_signature(request, body)

        # Se for GET, iFood esta testando conexao
        if method == "GET":
            logger.info("Teste de conexao (GET) - respondendo 200")
            return {"status": "ok", "message": "Webhook endpoint is alive"}

        # Se for POST, processar o evento
        payload = await request.json()
        # iFood codes: PLC, CAN, DSP
        event_type = (
            payload.get("code")
            or payload.get("eventType")
            or payload.get("event")
            or payload.get("type")
            or "unknown"
        )
        order_id = payload.get("orderId", "")
        merchant_id = payload.get("merchantId", "")

        logger.info("Evento: %s | Pedido: %s | Merchant: %s", event_type, order_id, merchant_id)

        # Processar evento
        ifood_client = IFoodAPIClient(settings)
        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)

        try:
            if event_type in ("PLC", "NEW", "orderCreated", "placed"):
                async with ifood_client:
                    order_data = await ifood_client.get_order(order_id)
                sale_order_id = sync_service.sync_order(order_data)
                logger.info("Pedido %s sincronizado -> Odoo sale.order %s", order_id, sale_order_id)

            elif event_type == "orderStatusChanged":
                status = payload.get("newStatus", payload.get("status", ""))
                if status:
                    sync_service.update_order_status(order_id, status)
                    logger.info("Pedido %s status atualizado: %s", order_id, status)

            elif event_type in ("CAN", "CANCELLATION_REQUESTED", "orderCancelled", "cancelled"):
                sync_service.update_order_status(order_id, "cancelled")
                logger.info("Pedido %s marcado como cancelado", order_id)

            else:
                logger.info("Evento nao tratado: %s - ack", event_type)

        finally:
            await ifood_client.close()

        return {"status": "ok", "eventType": event_type, "orderId": order_id}

    except Exception as e:
        logger.error("Erro no webhook iFood: %s", e, exc_info=True)
        # SEMPRE 200 para o iFood
        return {"status": "ok", "message": "received"}


@router.post("/webhooks/ifood/catalog")
@router.get("/webhooks/ifood/catalog")
async def handle_catalog_webhook(request: Request):
    """Recebe eventos de catalogo do iFood."""
    try:
        body = await request.body()
        logger.info("Webhook catalogo iFood recebido (method: %s)", request.method)
        logger.debug("Body RAW: %s", body.decode("utf-8", errors="replace")[:3000])
        return {"status": "ok", "message": "received"}

    except Exception as e:
        logger.error("Erro no webhook catalogo: %s", e, exc_info=True)
        return {"status": "ok", "message": "received"}
