import hashlib
import hmac
import logging
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, Request, Response

from app.config import settings
from app.services.ifood_api import IFoodAPIClient
from app.services.odoo_client import OdooClient
from app.services.odoo_sync import OdooSyncService

logger = logging.getLogger(__name__)
router = APIRouter()

# ── In-memory event log for debugging ─────────────────────────
_WEBHOOK_LOG: deque = deque(maxlen=100)


def _log_event(entry: dict) -> None:
    """Append event to in-memory log."""
    entry["_ts"] = time.time()
    _WEBHOOK_LOG.append(entry)


@router.get("/webhooks/ifood/debug-log")
async def get_webhook_log():
    """Retorna os ultimos eventos de webhook processados (para debug)."""
    return {"count": len(_WEBHOOK_LOG), "events": list(_WEBHOOK_LOG)}


# ── Signature validation ─────────────────────────────────────

def _verify_webhook_signature(request: Request, body: bytes) -> bool:
    """Verifica a assinatura do webhook iFood.

    iFood envia header 'x-ifood-signature-v1' com valor 'sha256=<hex>'.
    """
    if not settings.webhook_secret or settings.webhook_secret == "change-me-webhook-secret":
        logger.debug("WEBHOOK_SECRET nao configurado - pulando verificacao")
        return True

    signature = (
        request.headers.get("x-ifood-signature-v1")
        or request.headers.get("x-ifood-signature")
        or request.headers.get("X-IFood-Signature")
        or ""
    )

    if not signature:
        logger.info("Webhook recebido SEM assinatura - possivel teste de conexao")
        return True

    # Calcular hash esperado
    expected_hash = hmac.new(
        settings.webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    # iFood envia no formato "sha256=<hex>" - comparar o hash diretamente
    received_hash = signature
    if signature.startswith("sha256="):
        received_hash = signature[7:]  # remove "sha256=" prefix

    if not hmac.compare_digest(expected_hash, received_hash):
        logger.warning(
            "Assinatura INVALIDA! Recebida: %s | Hash calculado: %s | Body len: %d",
            signature, expected_hash, len(body),
        )
    else:
        logger.debug("Assinatura do webhook valida")

    # Sempre retorna True para nao rejeitar o iFood
    return True


# ── Main webhook handler ─────────────────────────────────────

@router.post("/webhooks/ifood/orders")
@router.get("/webhooks/ifood/orders")
async def handle_order_webhook(request: Request, response: Response):
    """Recebe eventos de pedidos do iFood.

    Regra #1: SEMPRE retorna 200 para o iFood (nao retry).
    Regra #2: Cada passo (auth, confirm, sync) e independente.
    Regra #3: Tudo e logado para debug.

    Fluxo PLC (novo pedido):
      1. Buscar dados completos do pedido no iFood
      2. Confirmar pedido no iFood (POST /confirm)
      3. Sincronizar com Odoo

    Fluxo CAN (cancelamento):
      1. Aceitar cancelamento no iFood (POST /cancellation/accept)
      2. Atualizar Odoo
    """
    # Variaveis de contexto para logging
    event_type = "unknown"
    order_id = ""

    try:
        body = await request.body()
        method = request.method

        logger.info("=" * 60)
        logger.info("=== Webhook iFood recebido ===")
        logger.info("Method: %s | Content-Type: %s | Body len: %d",
                     method, request.headers.get("content-type", "N/A"), len(body))
        logger.debug("Headers: %s", dict(request.headers))
        logger.debug("Body RAW: %s", body.decode("utf-8", errors="replace")[:3000])

        _verify_webhook_signature(request, body)

        # GET = teste de conexao do iFood
        if method == "GET":
            logger.info("Teste de conexao (GET) - respondendo 200")
            return {"status": "ok", "message": "Webhook endpoint is alive"}

        # Parsear payload
        try:
            payload = await request.json()
        except Exception as parse_err:
            logger.error("[FATAL] Nao foi possivel parsear JSON do webhook: %s", parse_err)
            logger.error("[FATAL] Body: %s", body.decode("utf-8", errors="replace")[:2000])
            _log_event({"level": "error", "message": "JSON parse failed", "error": str(parse_err)})
            return {"status": "ok", "message": "received"}  # sempre 200

        event_type = (
            payload.get("code")
            or payload.get("eventType")
            or payload.get("event")
            or payload.get("type")
            or "unknown"
        )
        order_id = str(payload.get("orderId", "") or "")
        merchant_id = str(payload.get("merchantId", "") or "")

        logger.info("Evento: %s | Pedido: %s | Merchant: %s", event_type, order_id, merchant_id)

        _log_event({
            "level": "info",
            "event_type": event_type,
            "order_id": order_id,
            "merchant_id": merchant_id,
            "method": method,
        })

        # ── KEEPALIVE: heartbeat do iFood ─────────────────────
        if event_type == "KEEPALIVE":
            logger.debug("[KEEPALIVE] Heartbeat recebido - ack silencioso")
            return {"status": "ok", "eventType": "KEEPALIVE"}

        # ── PLC: Novo Pedido ──────────────────────────────────
        if event_type in ("PLC", "NEW", "orderCreated", "placed"):
            return await _handle_plc(order_id, merchant_id, payload, body)

        # ── CAN: Cancelamento Solicitado ──────────────────────
        if event_type in ("CAN", "CANCELLATION_REQUESTED", "cancellationRequested"):
            return await _handle_can(order_id, merchant_id, payload, body)

        # ── CAR: Cancelamento Aceito ──────────────────────────
        if event_type in ("CAR", "CANCELLATION_ACCEPTED"):
            logger.info("[CAR] Cancelamento confirmado pelo iFood para pedido %s", order_id)
            try:
                _update_odoo_status(order_id, "cancelled")
            except Exception as odoo_err:
                logger.error("[CAR] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)
            return {"status": "ok", "eventType": event_type, "orderId": order_id}

        # ── DSP: Despacho ─────────────────────────────────────
        if event_type in ("DSP", "DISPATCHED", "orderDispatched"):
            logger.info("[DSP] Pedido despachado: %s", order_id)
            try:
                _update_odoo_status(order_id, "dispatched")
            except Exception as odoo_err:
                logger.error("[DSP] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)
            return {"status": "ok", "eventType": event_type, "orderId": order_id}

        # ── Status Change generico ────────────────────────────
        if event_type == "orderStatusChanged":
            status = payload.get("newStatus", payload.get("status", ""))
            logger.info("[STATUS_CHANGE] Pedido %s - novo status: %s", order_id, status)
            if status:
                try:
                    _update_odoo_status(order_id, status)
                except Exception as odoo_err:
                    logger.error("[STATUS_CHANGE] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)
            return {"status": "ok", "eventType": event_type, "orderId": order_id}

        # ── Evento desconhecido ───────────────────────────────
        logger.info("Evento nao tratado: %s | Payload: %s",
                     event_type, str(payload)[:500])
        _log_event({
            "level": "warning",
            "event_type": event_type,
            "order_id": order_id,
            "message": "Evento nao tratado",
        })
        return {"status": "ok", "eventType": event_type, "orderId": order_id}

    except Exception as e:
        # CAPTURA QUALQUER ERRO IMPREVISTO - mas SEMPRE retorna 200
        logger.error("[FATAL] Erro NAO TRATADO no webhook iFood: %s", e, exc_info=True)
        _log_event({
            "level": "error",
            "event_type": event_type,
            "order_id": order_id,
            "message": "Erro fatal nao tratado",
            "error": str(e),
        })
        return {"status": "ok", "message": "received"}


# ── PLC Handler: Novo Pedido ─────────────────────────────────

async def _handle_plc(order_id: str, merchant_id: str, payload: dict, body: bytes) -> dict:
    """Processa evento PLC (novo pedido do iFood).

    Passos independentes:
    1. Buscar dados do pedido e confirmar no iFood
    2. Sincronizar com Odoo

    Se um passo falha, o outro ainda e tentado.
    """
    logger.info("[PLC] === INICIO FLUXO DE CONFIRMACAO ===")
    logger.info("[PLC] Pedido: %s | Merchant: %s", order_id, merchant_id)

    ifood_client = IFoodAPIClient(settings)
    order_data = None
    confirmed = False
    synced = False

    try:
        # ── PASSO 1: Auth + Get Order + Confirm no iFood ─────
        try:
            logger.info("[PLC] Passo 1: Buscando dados do pedido no iFood...")
            async with ifood_client:
                # 1a. Buscar dados completos
                order_data = await ifood_client.get_order(order_id)
                logger.info("[PLC] Dados do pedido obtidos. Campos: %s", list(order_data.keys()) if isinstance(order_data, dict) else type(order_data))

                # 1b. CONFIRMAR pedido no iFood (obrigatorio para homologacao)
                logger.info("[PLC] Chamando POST /order/v1.0/orders/%s/confirm", order_id)
                confirm_result = await ifood_client.confirm_order(order_id)
                confirmed = True
                logger.info("[PLC] Pedido CONFIRMADO no iFood! Resposta: %s", str(confirm_result)[:500])

        except Exception as ifood_err:
            logger.error("[PLC] FALHA na comunicacao com iFood: %s", ifood_err, exc_info=True)
            _log_event({
                "level": "error",
                "event_type": "PLC",
                "order_id": order_id,
                "step": "ifood_api",
                "message": "Falha ao buscar/confirmar pedido no iFood",
                "error": str(ifood_err),
            })

        # ── PASSO 2: Sincronizar com Odoo ────────────────────
        if order_data:
            try:
                logger.info("[PLC] Passo 2: Sincronizando com Odoo...")
                odoo_client = OdooClient(settings)
                sync_service = OdooSyncService(odoo_client, ifood_client)
                sale_order_id = sync_service.sync_order(order_data)
                synced = True
                logger.info("[PLC] Pedido sincronizado -> Odoo sale.order %s", sale_order_id)

                # Atualizar status no Odoo
                sync_service.update_order_status(order_id, "confirmed")
                logger.info("[PLC] Status atualizado para 'confirmed' no Odoo")

            except Exception as odoo_err:
                logger.error("[PLC] FALHA na sincronizacao com Odoo: %s", odoo_err, exc_info=True)
                _log_event({
                    "level": "error",
                    "event_type": "PLC",
                    "order_id": order_id,
                    "step": "odoo_sync",
                    "message": "Falha ao sincronizar pedido com Odoo",
                    "error": str(odoo_err),
                })
        else:
            logger.error("[PLC] Nenhum dado de pedido disponivel - pulando sync Odoo")

        # ── Resumo do processamento ──────────────────────────
        result = {
            "status": "ok",
            "eventType": "PLC",
            "orderId": order_id,
            "confirmed_on_ifood": confirmed,
            "synced_to_odoo": synced,
        }
        logger.info("[PLC] === RESUMO: confirmado=%s | sync_odoo=%s ===",
                     confirmed, synced)
        logger.info("[PLC] === FIM FLUXO DE CONFIRMACAO ===")
        logger.info("=" * 60)

        _log_event({
            "level": "info",
            "event_type": "PLC",
            "order_id": order_id,
            "confirmed": confirmed,
            "synced": synced,
        })

        return result

    finally:
        # Fechar cliente iFood de forma segura
        try:
            await ifood_client.close()
        except Exception:
            pass


# ── CAN Handler: Cancelamento Solicitado ─────────────────────

async def _handle_can(order_id: str, merchant_id: str, payload: dict, body: bytes) -> dict:
    """Processa evento CAN (solicitacao de cancelamento do iFood).

    Passos independentes:
    1. Aceitar cancelamento no iFood
    2. Atualizar Odoo
    """
    logger.info("[CANCELLATION] === INICIO FLUXO DE CANCELAMENTO ===")
    logger.info("[CANCELLATION] Pedido: %s | Merchant: %s", order_id, merchant_id)
    logger.info("[CANCELLATION] Payload: %s", body.decode("utf-8", errors="replace")[:3000])

    cancellation_reason = payload.get("reason", payload.get("cancellationReason", "nao informado"))
    logger.info("[CANCELLATION] Motivo: %s", cancellation_reason)

    ifood_client = IFoodAPIClient(settings)
    accepted = False
    odoo_updated = False

    try:
        # ── PASSO 1: Aceitar cancelamento no iFood ───────────
        try:
            logger.info("[CANCELLATION] Chamando POST /order/v1.0/orders/%s/cancellation/accept", order_id)
            async with ifood_client:
                accept_result = await ifood_client.accept_cancellation(order_id)
            accepted = True
            logger.info("[CANCELLATION] Cancelamento ACEITO no iFood: %s",
                         str(accept_result)[:500])
        except Exception as ifood_err:
            logger.error("[CANCELLATION] FALHA ao aceitar cancelamento no iFood: %s",
                         ifood_err, exc_info=True)
            _log_event({
                "level": "error",
                "event_type": "CAN",
                "order_id": order_id,
                "step": "ifood_api",
                "message": "Falha ao aceitar cancelamento",
                "error": str(ifood_err),
            })

        # ── PASSO 2: Atualizar Odoo ──────────────────────────
        try:
            odoo_updated = _update_odoo_status(order_id, "cancelled")
        except Exception as odoo_err:
            logger.error("[CANCELLATION] FALHA ao atualizar Odoo: %s", odoo_err, exc_info=True)

        result = {
            "status": "ok",
            "eventType": "CAN",
            "orderId": order_id,
            "accepted_on_ifood": accepted,
            "odoo_updated": odoo_updated,
        }
        logger.info("[CANCELLATION] === RESUMO: aceito=%s | odoo=%s ===", accepted, odoo_updated)
        logger.info("[CANCELLATION] === FIM FLUXO DE CANCELAMENTO ===")

        _log_event({
            "level": "info",
            "event_type": "CAN",
            "order_id": order_id,
            "accepted": accepted,
            "odoo_updated": odoo_updated,
        })

        return result

    finally:
        try:
            await ifood_client.close()
        except Exception:
            pass


# ── Helper: Atualizar status no Odoo ─────────────────────────

def _update_odoo_status(ifood_order_id: str, status: str) -> bool:
    """Atualiza o status iFood no sale.order do Odoo."""
    try:
        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, IFoodAPIClient(settings))
        return sync_service.update_order_status(ifood_order_id, status)
    except Exception as e:
        logger.error("[ODOO] Falha ao atualizar status %s para pedido %s: %s",
                     status, ifood_order_id, e, exc_info=True)
        raise


# ── Catalog webhook ──────────────────────────────────────────

@router.post("/webhooks/ifood/catalog")
@router.get("/webhooks/ifood/catalog")
async def handle_catalog_webhook(request: Request):
    """Recebe eventos de catalogo do iFood."""
    try:
        body = await request.body()
        logger.info("Webhook catalogo iFood recebido (method: %s, body_len: %d)",
                     request.method, len(body))
        logger.debug("Body RAW: %s", body.decode("utf-8", errors="replace")[:3000])
        return {"status": "ok", "message": "received"}

    except Exception as e:
        logger.error("Erro no webhook catalogo: %s", e, exc_info=True)
        return {"status": "ok", "message": "received"}