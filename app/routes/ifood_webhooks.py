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
    if not settings.webhook_secret or settings.webhook_secret == "change-me-webhook-secret":
        return True
    signature = (request.headers.get("x-ifood-signature") or request.headers.get("X-IFood-Signature") or request.headers.get("x-ifood-signature-v1") or "")
    if not signature:
        return True
    expected = hmac.new(settings.webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(f"sha256={expected}", signature):
        logger.warning("Assinatura INVALIDA! Recebida: %s | Esperada: sha256=%s", signature, expected)
    else:
        logger.debug("Assinatura valida")
    return True


@router.post("/webhooks/ifood/orders")
@router.get("/webhooks/ifood/orders")
async def handle_order_webhook(request: Request, response: Response):
    try:
        body = await request.body()
        method = request.method
        logger.info("=== Webhook iFood recebido ===")
        logger.info("Method: %s | Content-Type: %s", method, request.headers.get("content-type", "N/A"))
        logger.debug("Headers: %s", dict(request.headers))
        logger.debug("Body RAW: %s", body.decode("utf-8", errors="replace")[:2000])
        _verify_webhook_signature(request, body)
        if method == "GET":
            logger.info("Teste de conexao (GET) - respondendo 200")
            return {"status": "ok", "message": "Webhook endpoint is alive"}
        payload = await request.json()
        event_type = payload.get("code") or payload.get("eventType") or payload.get("event") or payload.get("type") or "unknown"
        order_id = payload.get("orderId", "")
        merchant_id = payload.get("merchantId", "")
        logger.info("Evento: %s | Pedido: %s | Merchant: %s", event_type, order_id, merchant_id)
        ifood_client = IFoodAPIClient(settings)
        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)
        try:
            if event_type in ("PLC", "NEW", "orderCreated", "placed"):
                logger.info("[PLC] === INICIO FLUXO DE CONFIRMACAO ===")
                logger.info("[PLC] Pedido: %s | Merchant: %s", order_id, merchant_id)
                async with ifood_client:
                    order_data = await ifood_client.get_order(order_id)
                    logger.info("[PLC] Dados do pedido obtidos do iFood")
                    logger.info("[PLC] Chamando POST /order/v1.0/orders/%s/confirm", order_id)
                    confirm_result = await ifood_client.confirm_order(order_id)
                    logger.info("[PLC] Pedido CONFIRMADO no iFood com sucesso")
                    logger.info("[PLC] Resposta confirm: %s", confirm_result)
                sale_order_id = sync_service.sync_order(order_data)
                sync_service.update_order_status(order_id, "confirmed")
                logger.info("[PLC] Pedido %s -> Odoo sale.order %s (status: confirmed)", order_id, sale_order_id)
                logger.info("[PLC] === FIM FLUXO DE CONFIRMACAO ===")
            elif event_type in ("CAN", "CANCELLATION_REQUESTED", "cancellationRequested"):
                logger.info("[CANCELLATION] === INICIO FLUXO DE CANCELAMENTO ===")
                logger.info("[CANCELLATION] Evento: %s | Pedido: %s | Merchant: %s", event_type, order_id, merchant_id)
                logger.info("[CANCELLATION] Payload: %s", body.decode("utf-8", errors="replace")[:3000])
                cancellation_reason = payload.get("reason", payload.get("cancellationReason", "nao informado"))
                logger.info("[CANCELLATION] Motivo: %s", cancellation_reason)
                async with ifood_client:
                    logger.info("[CANCELLATION] Chamando POST /order/v1.0/orders/%s/cancellation/accept", order_id)
                    accept_result = await ifood_client.accept_cancellation(order_id)
                    logger.info("[CANCELLATION] Cancelamento ACEITO no iFood")
                    logger.info("[CANCELLATION] Resposta: %s", accept_result)
                odoo_updated = sync_service.update_order_status(order_id, "cancelled")
                logger.info("[CANCELLATION] Odoo atualizado cancelled: %s (pedido %s)", odoo_updated, order_id)
                logger.info("[CANCELLATION] === FIM FLUXO DE CANCELAMENTO ===")
            elif event_type in ("CAR", "CANCELLATION_ACCEPTED"):
                logger.info("[CAR] Cancelamento confirmado pelo iFood para pedido %s", order_id)
                sync_service.update_order_status(order_id, "cancelled")
            elif event_type in ("DSP", "DISPATCHED", "orderDispatched"):
                logger.info("[DSP] Pedido despachado: %s", order_id)
                sync_service.update_order_status(order_id, "dispatched")
            elif event_type == "orderStatusChanged":
                status = payload.get("newStatus", payload.get("status", ""))
                logger.info("[STATUS_CHANGE] Pedido %s - novo status: %s", order_id, status)
                if status:
                    sync_service.update_order_status(order_id, status)
            elif event_type == "KEEPALIVE":
                logger.debug("[KEEPALIVE] Heartbeat recebido")
            else:
                logger.info("Evento nao tratado: %s - ack", event_type)
        finally:
            await ifood_client.close()
        return {"status": "ok", "eventType": event_type, "orderId": order_id}
    except Exception as e:
        logger.error("Erro no webhook iFood: %s", e, exc_info=True)
        return {"status": "ok", "message": "received"}


@router.post("/webhooks/ifood/catalog")
@router.get("/webhooks/ifood/catalog")
async def handle_catalog_webhook(request: Request):
    try:
        body = await request.body()
        logger.info("Webhook catalogo iFood recebido (method: %s)", request.method)
        logger.debug("Body RAW: %s", body.decode("utf-8", errors="replace")[:3000])
        return {"status": "ok", "message": "received"}
    except Exception as e:
        logger.error("Erro no webhook catalogo: %s", e, exc_info=True)
        return {"status": "ok", "message": "received"}
