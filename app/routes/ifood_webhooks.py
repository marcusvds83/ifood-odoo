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
                logger.info("[PLC] Novo pedido recebido: %s", order_id)
                async with ifood_client:
                    order_data = await ifood_client.get_order(order_id)
                sale_order_id = sync_service.sync_order(order_data)
                logger.info("[PLC] Pedido %s sincronizado -> Odoo sale.order %s", order_id, sale_order_id)
            elif event_type == "orderStatusChanged":
                status = payload.get("newStatus", payload.get("status", ""))
                logger.info("[STATUS_CHANGE] Pedido %s - novo status: %s", order_id, status)
                if status:
                    sync_service.update_order_status(order_id, status)
            elif event_type in ("CAN", "CANCELLATION_REQUESTED", "cancellationRequested"):
                logger.info("[CANCELLATION] === INICIO FLUXO DE CANCELAMENTO ===")
                logger.info("[CANCELLATION] Evento: %s | Pedido: %s | Merchant: %s", event_type, order_id, merchant_id)
                logger.info("[CANCELLATION] Payload completo: %s", body.decode("utf-8", errors="replace")[:3000])
                cancellation_reason = payload.get("reason", payload.get("cancellationReason", "nao informado"))
                logger.info("[CANCELLATION] Motivo: %s", cancellation_reason)
                odoo_updated = sync_service.update_order_status(order_id, "cancelled")
                logger.info("[CANCELLATION] Odoo atualizado para cancelled: %s (pedido %s)", odoo_updated, order_id)
                try:
                    async with ifood_client:
                        accept_result = await ifood_client.accept_cancellation(order_id)
                    logger.info("[CANCELLATION] Confirmacao ENVIADA ao iFood com sucesso")
                    logger.info("[CANCELLATION] Resposta: %s", accept_result)
                except Exception as cancel_err:
                    logger.error("[CANCELLATION] FALHA confirmacao cancelamento: %s", cancel_err, exc_info=True)
                logger.info("[CANCELLATION] === FIM FLUXO DE CANCELAMENTO ===")
            elif event_type in ("CAR", "CANCELLATION_ACCEPTED"):
                logger.info("[CANCELLATION] Cancelamento confirmado pelo iFood para pedido %s", order_id)
                sync_service.update_order_status(order_id, "cancelled")
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
        return {"status": "ok", "message": "received"}
    except Exception as e:
        logger.error("Erro no webhook catalogo: %s", e, exc_info=True)
        return {"status": "ok", "message": "received"}
