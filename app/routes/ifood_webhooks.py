import hashlib
import hmac
import logging
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, Response

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
async def handle_order_webhook(request: Request, response: Response, background_tasks: BackgroundTasks):
    """Recebe eventos de pedidos do iFood.

    Regra #1: SEMPRE retorna 200/202 para o iFood (nao retry).
    Regra #2: CANCELLATION_REQUESTED retorna 202 imediato e processa em background (iFood exige resp < 5s).
    Regra #3: Cada passo (auth, confirm, sync) e independente.
    Regra #4: Tudo e logado para debug.

    Fluxo PLC (novo pedido):
      1. Buscar dados completos do pedido no iFood
      2. Confirmar pedido no iFood (POST /confirm)
      3. Sincronizar com Odoo

    Fluxo CANCELLATION_REQUESTED (cliente solicita cancelamento):
      1. Responder 202 imediatamente
      2. Em background: aceitar cancelamento no iFood (POST /cancellation/accept)
      3. Aguardar evento CANCELLED para atualizar Odoo

    Fluxo CANCELLED (cancelamento concluido):
      1. Atualizar status no Odoo para 'cancelled'
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
            logger.debug("[KEEPALIVE] Heartbeat recebido")
            # Aproveitar o heartbeat para verificar acoes pendentes no Odoo
            try:
                await _check_odoo_pending_cancellations()
            except Exception as poll_err:
                logger.warning("[KEEPALIVE] Falha ao verificar cancelamentos Odoo: %s", poll_err)
            try:
                await _check_odoo_pending_dispatches()
            except Exception as poll_err:
                logger.warning("[KEEPALIVE] Falha ao verificar despachos Odoo: %s", poll_err)
            return {"status": "ok", "eventType": "KEEPALIVE"}

        # ── PLC: Novo Pedido ──────────────────────────────────
        if event_type in ("PLC", "NEW", "orderCreated", "placed"):
            return await _handle_plc(order_id, merchant_id, payload, body)

        # ── CAN / CANCELLATION_REQUESTED: Cancelamento Solicitado ──
        # iFood exige resposta 202 em ate 5s. Processar em background.
        if event_type in ("CAN", "CANCELLATION_REQUESTED", "cancellationRequested",
                         "CancellationRequested", "cancellation_requested"):
            logger.info("[CANCELLATION_REQUESTED] Pedido %s | Merchant %s | Adicionando ao background", order_id, merchant_id)
            background_tasks.add_task(_handle_can_background, order_id, merchant_id, payload, body)
            return Response(status_code=202, content=b'')

        # ── CANCELLED: Cancelamento Concluido (por merchant ou cliente) ──
        if event_type in ("CANCELLED", "cancelled", "CANCELLATION_ACCEPTED", "CAR",
                          "CancellationAccepted", "cancellation_accepted"):
            logger.info("[CANCELLED] Pedido %s CANCELADO no iFood - atualizando Odoo", order_id)
            _log_event({
                "level": "info",
                "event_type": "CANCELLED",
                "order_id": order_id,
                "message": "Evento CANCELLED recebido, atualizando Odoo",
            })
            try:
                _update_odoo_status(order_id, "cancelled")
                _update_odoo_state(order_id, "cancel")
                logger.info("[CANCELLED] Pedido %s - Odoo atualizado para cancelled + state=cancel", order_id)
            except Exception as odoo_err:
                logger.error("[CANCELLED] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)
            return {"status": "ok", "eventType": event_type, "orderId": order_id}

        # ── CANCELLATION_REQUEST_FAILED: Falha no cancelamento ──
        if event_type in ("CANCELLATION_REQUEST_FAILED", "cancellationRequestFailed",
                          "CancellationRequestFailed", "cancellation_request_failed"):
            logger.warning("[CANCEL_FAILED] Falha no cancelamento do pedido %s no iFood", order_id)
            _log_event({
                "level": "warning",
                "event_type": "CANCELLATION_REQUEST_FAILED",
                "order_id": order_id,
                "message": "Cancelamento falhou no iFood",
            })
            try:
                _update_odoo_status(order_id, "cancellation_failed")
            except Exception as odoo_err:
                logger.error("[CANCEL_FAILED] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)
            return {"status": "ok", "eventType": event_type, "orderId": order_id}

        # ── DSP: Despacho ─────────────────────────────────────
        if event_type in ("DSP", "DISPATCHED", "orderDispatched"):
            logger.info("[DSP] Pedido despachado: %s", order_id)
            try:
                _update_odoo_status(order_id, "dispatched")
            except Exception as odoo_err:
                logger.error("[DSP] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)
            return {"status": "ok", "eventType": event_type, "orderId": order_id}

        # ── CONCLUDED: Pedido concluido (entrega finalizada) ─────
        if event_type in ("CONCLUDED", "concluded", "DELIVERED", "delivered"):
            logger.info("[CONCLUDED] Pedido %s CONCLUIDO no iFood - atualizando Odoo", order_id)
            try:
                _update_odoo_status(order_id, "concluded")
                # Tambem tentar mudar state para 'done' no Odoo
                _update_odoo_state(order_id, "done")
            except Exception as odoo_err:
                logger.error("[CONCLUDED] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)
            return {"status": "ok", "eventType": event_type, "orderId": order_id}

        # ── Status Change generico ────────────────────────────
        if event_type == "orderStatusChanged":
            status = payload.get("newStatus", payload.get("status", ""))
            logger.info("[STATUS_CHANGE] Pedido %s - novo status: %s", order_id, status)
            if status:
                try:
                    _update_odoo_status(order_id, status)
                    # Se o status for concluded/delivered, tambem mudar state
                    if status in ("CONCLUDED", "concluded", "DELIVERED", "delivered"):
                        _update_odoo_state(order_id, "done")
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

async def _handle_can_background(order_id: str, merchant_id: str, payload: dict, body: bytes) -> None:
    """Processa evento CANCELLATION_REQUESTED em background (nao bloqueia o webhook response).

    Fluxo iFood para cancelamento (OBRIGATORIO para homologacao):
    1. Receber CANCELLATION_REQUESTED via webhook (ja respondido com 202)
    2. Buscar motivos validos via GET /cancellationReasons (fallback 501 se falhar)
    3. Chamar POST /orders/{orderId}/requestCancellation com {"reason": codigo}
    4. Aguardar evento CANCELLED (ou CANCELLATION_REQUEST_FAILED)
    5. Atualizar Odoo com o status final

    IMPORTANTE: O endpoint correto e POST /requestCancellation (NAO /cancel).
    Body: {"reason": "codigo"} (NAO cancellationCode).
    """
    logger.info("[CANCELLATION_REQUESTED] === INICIO FLUXO DE CANCELAMENTO (background) ===")
    logger.info("[CANCELLATION_REQUESTED] Pedido: %s | Merchant: %s", order_id, merchant_id)
    logger.info("[CANCELLATION_REQUESTED] Payload: %s", body.decode("utf-8", errors="replace")[:3000])

    _log_event({
        "level": "info",
        "event_type": "CANCELLATION_REQUESTED",
        "order_id": order_id,
        "merchant_id": merchant_id,
        "message": "Evento CANCELLATION_REQUESTED recebido e ack com 202 - iniciando processamento",
    })

    ifood_client = IFoodAPIClient(settings)
    cancelled = False

    try:
        # ── UNICA sessao OAuth: busca motivos + POST requestCancellation ─────
        # Tudo dentro de UM unico async with para evitar dupla autenticacao.
        # O Firefly Audit requer ver o POST requestCancellation na resposta.
        async with ifood_client:
            try:
                logger.info("[CANCELLATION_REQUESTED] Chamando accept_cancellation (GET reasons + POST /requestCancellation)...")
                accept_result = await ifood_client.accept_cancellation(order_id)
                cancelled = True
                logger.info("[CANCELLATION_REQUESTED] Cancelamento ACEITO no iFood: %s",
                             str(accept_result)[:500])
                _log_event({
                    "level": "info",
                    "event_type": "CANCELLATION_REQUESTED",
                    "order_id": order_id,
                    "step": "cancel_ifood",
                    "message": "requestCancellation enviado com sucesso",
                    "cancelled": True,
                })
            except Exception as ifood_err:
                logger.error("[CANCELLATION_REQUESTED] FALHA ao enviar requestCancellation: %s",
                             ifood_err, exc_info=True)
                _log_event({
                    "level": "error",
                    "event_type": "CANCELLATION_REQUESTED",
                    "order_id": order_id,
                    "step": "cancel_ifood",
                    "message": "Falha ao enviar requestCancellation ao iFood",
                    "error": str(ifood_err),
                    "cancelled": False,
                })

        # ── Atualizar Odoo ──
        # NAO colocar 'cancelled' ainda! Aguardar o evento CANCELLED.
        try:
            sync_status = "cancellation_accepted" if cancelled else "cancellation_requested"
            _update_odoo_status(order_id, sync_status)
            logger.info("[CANCELLATION_REQUESTED] Odoo atualizado para '%s' (aguardando evento CANCELLED)", sync_status)
        except Exception as odoo_err:
            logger.error("[CANCELLATION_REQUESTED] Falha ao atualizar Odoo: %s", odoo_err, exc_info=True)

        logger.info("[CANCELLATION_REQUESTED] === RESUMO: cancelado=%s ===", cancelled)
        logger.info("[CANCELLATION_REQUESTED] === FIM PROCESSAMENTO (aguardando evento CANCELLED) ===")

    finally:
        try:
            await ifood_client.close()
        except Exception:
            pass


# ── Helper: Atualizar status no Odoo ─────────────────────────

def _update_odoo_status(ifood_order_id: str, status: str) -> bool:
    """Atualiza o campo x_studio_ifood_status no sale.order do Odoo."""
    try:
        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, IFoodAPIClient(settings))
        return sync_service.update_order_status(ifood_order_id, status)
    except Exception as e:
        logger.error("[ODOO] Falha ao atualizar status %s para pedido %s: %s",
                     status, ifood_order_id, e, exc_info=True)
        raise


def _update_odoo_state(ifood_order_id: str, state: str) -> None:
    """Muda o state do sale.order no Odoo (ex: 'done', 'cancel')."""
    try:
        odoo_client = OdooClient(settings)
        # Buscar o ID do sale.order pelo x_studio_ifood_order_id
        orders = odoo_client.search_read(
            "sale.order",
            domain=[("x_studio_ifood_order_id", "=", ifood_order_id)],
            fields=["id", "state"],
        )
        if not orders:
            logger.warning("[ODOO] Pedido %s nao encontrado no Odoo para mudar state", ifood_order_id)
            return
        for order in orders:
            if order.get("state") == state:
                continue
            odoo_client.execute_kw(
                "sale.order",
                "write",
                [[order["id"]], {"state": state}],
            )
            logger.info("[ODOO] Pedido %s (Odoo %d) -> state='%s'", ifood_order_id, order["id"], state)
    except Exception as e:
        logger.error("[ODOO] Falha ao mudar state '%s' para pedido %s: %s",
                     state, ifood_order_id, e, exc_info=True)


# ── Polling: Cancelamentos Odoo → iFood ────────────────────

async def _check_odoo_pending_cancellations() -> dict:
    """Verifica pedidos cancelados no Odoo que ainda nao foram cancelados no iFood.

    Busca sale.order onde:
      - state = 'cancel' (cancelado no Odoo)
      - x_studio_ifood_order_id esta preenchido
      - x_studio_ifood_status != 'cancelled' (ainda nao propagado)

    Para cada pedido encontrado, envia requestCancellation ao iFood
    e marca status como 'cancellation_requested' no Odoo.
    O cancelamento final acontece quando o iFood envia o evento CANCELLED.
    """
    logger.info("[ODOO_POLL] Verificando cancelamentos pendentes no Odoo...")

    odoo_client = OdooClient(settings)
    results = {"checked": 0, "requested": 0, "errors": []}

    try:
        orders = odoo_client.search_read(
            "sale.order",
            domain=[
                ("state", "=", "cancel"),
                ("x_studio_ifood_order_id", "!=", False),
            ],
            fields=[
                "id", "x_studio_ifood_order_id",
                "x_studio_ifood_cancel_reason",
                "x_studio_ifood_status",
            ],
        )
    except Exception as e:
        logger.error("[ODOO_POLL] Falha ao buscar pedidos no Odoo: %s", e, exc_info=True)
        return results

    results["checked"] = len(orders)
    if not orders:
        logger.info("[ODOO_POLL] Nenhum cancelamento pendente encontrado")
        return results

    logger.info("[ODOO_POLL] Encontrados %d pedidos cancelados no Odoo", len(orders))

    async with IFoodAPIClient(settings) as ifood_client:
        for order in orders:
            ifood_id = str(order.get("x_studio_ifood_order_id", ""))
            ifood_status = str(order.get("x_studio_ifood_status", ""))

            if not ifood_id:
                continue

            # Ja foi cancelado ou ja tem solicitacao em andamento?
            if ifood_status in ("cancelled", "cancellation_requested", "cancellation_accepted"):
                continue

            reason_code = str(order.get("x_studio_ifood_cancel_reason", ""))
            # Extrair apenas o codigo numerico (ex: "506:Endereco..." -> "506")
            if ":" in reason_code:
                reason_code = reason_code.split(":")[0].strip()
            if not reason_code or not reason_code.isdigit():
                reason_code = "501"  # default: Erro no sistema

            logger.info("[ODOO_POLL] Solicitando cancelamento pedido %s no iFood (motivo: %s)...",
                         ifood_id, reason_code)

            try:
                cancel_result = await ifood_client.merchant_request_cancellation(ifood_id, reason_code=reason_code)
                results["requested"] += 1

                # Se ja estava cancelado, marcar direto como 'cancelled'
                final_status = "cancelled" if cancel_result.get("status") == "already_cancelled" else "cancellation_requested"
                try:
                    sync_service = OdooSyncService(odoo_client, ifood_client)
                    sync_service.update_order_status(ifood_id, final_status)
                except Exception:
                    pass

                logger.info("[ODOO_POLL] Cancelamento %s para pedido %s - status Odoo: %s",
                             "ja estava cancelado" if final_status == "cancelled" else "ENVIADO",
                             ifood_id, final_status)

            except Exception as e:
                logger.error("[ODOO_POLL] Falha ao solicitar cancelamento pedido %s: %s",
                             ifood_id, e, exc_info=True)
                results["errors"].append(f"{ifood_id}: {str(e)}")

    logger.info("[ODOO_POLL] Resultado: verificados=%d, solicitados=%d",
                 results["checked"], results["requested"])
    return results


# ── Polling: Despachos Odoo → iFood ──────────────────────────

async def _check_odoo_pending_dispatches() -> dict:
    """Verifica pedidos confirmados no Odoo que ainda nao foram despachados no iFood.

    Busca sale.order onde:
      - state = 'sale' (confirmado via action_confirm no Odoo)
      - x_studio_ifood_order_id esta preenchido
      - x_studio_ifood_status != 'dispatched' e != 'cancelled'

    Para cada pedido encontrado, chama dispatch no iFood.
    """
    logger.info("[ODOO_DISPATCH] Verificando despachos pendentes no Odoo...")

    odoo_client = OdooClient(settings)
    results = {"checked": 0, "dispatched": 0, "errors": []}

    try:
        orders = odoo_client.search_read(
            "sale.order",
            domain=[
                ("state", "=", "sale"),
                ("x_studio_ifood_order_id", "!=", False),
            ],
            fields=[
                "id", "x_studio_ifood_order_id",
                "x_studio_ifood_status",
            ],
        )
    except Exception as e:
        logger.error("[ODOO_DISPATCH] Falha ao buscar pedidos no Odoo: %s", e, exc_info=True)
        return results

    results["checked"] = len(orders)
    if not orders:
        logger.info("[ODOO_DISPATCH] Nenhum despacho pendente encontrado")
        return results

    async with IFoodAPIClient(settings) as ifood_client:
        for order in orders:
            ifood_id = str(order.get("x_studio_ifood_order_id", ""))
            ifood_status = str(order.get("x_studio_ifood_status", ""))

            if not ifood_id:
                continue

            # Ja foi despachado ou cancelado?
            if ifood_status in ("dispatched", "cancelled", "cancellation_requested", "delivered", "concluded"):
                continue

            logger.info("[ODOO_DISPATCH] Despachando pedido %s no iFood (status atual: %s)...",
                         ifood_id, ifood_status)

            try:
                await ifood_client.dispatch_order(ifood_id)
                results["dispatched"] += 1

                # Atualizar status no Odoo
                try:
                    sync_service = OdooSyncService(odoo_client, ifood_client)
                    sync_service.update_order_status(ifood_id, "dispatched")
                except Exception:
                    pass

                logger.info("[ODOO_DISPATCH] Pedido %s DESPACHADO no iFood com sucesso!", ifood_id)

            except Exception as e:
                logger.error("[ODOO_DISPATCH] Falha ao despachar pedido %s: %s",
                             ifood_id, e, exc_info=True)
                results["errors"].append(f"{ifood_id}: {str(e)}")

    if results["dispatched"] > 0:
        logger.info("[ODOO_DISPATCH] Resultado: verificados=%d, despachados=%d",
                     results["checked"], results["dispatched"])
    return results


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