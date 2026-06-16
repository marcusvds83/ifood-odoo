import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.config import settings
from app.services.ifood_api import IFoodAPIClient
from app.services.odoo_client import OdooClient
from app.services.odoo_sync import OdooSyncService

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Listar pedidos no Odoo ───────────────────────────────────

@router.get("/")
async def list_orders():
    """Lista pedidos iFood recentes do Odoo."""
    try:
        odoo_client = OdooClient(settings)

        orders = odoo_client.search_read(
            "sale.order",
            domain=[("x_studio_ifood_order_id", "!=", False)],
            fields=[
                "id", "name", "x_studio_ifood_order_id", "x_studio_ifood_display_id",
                "x_studio_ifood_status", "x_studio_ifood_order_type", "x_studio_ifood_created_at",
                "x_studio_ifood_payment_value", "partner_id", "state",
                "create_date",
            ],
            limit=50,
        )

        return {
            "status": "ok",
            "count": len(orders),
            "orders": orders,
        }

    except Exception as e:
        logger.error("Failed to list orders: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to list orders: {str(e)}")


# ── Detalhe de pedido no Odoo ────────────────────────────────

@router.get("/{order_id}")
async def get_order(order_id: int):
    """Busca detalhes de um pedido especifico no Odoo."""
    try:
        odoo_client = OdooClient(settings)

        orders = odoo_client.search_read(
            "sale.order",
            domain=[("id", "=", order_id)],
            fields=[
                "id", "name", "x_studio_ifood_order_id", "x_studio_ifood_display_id",
                "x_studio_ifood_status", "x_studio_ifood_order_type", "x_studio_ifood_created_at",
                "x_studio_ifood_payment_method", "x_studio_ifood_payment_value",
                "x_studio_ifood_delivery_fee", "x_studio_ifood_subtotal",
                "x_studio_ifood_delivery_address", "x_studio_ifood_customer_id",
                "partner_id", "state", "amount_total", "order_line",
            ],
            limit=1,
        )

        if not orders:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

        order = orders[0]
        if order.get("order_line"):
            lines = odoo_client.search_read(
                "sale.order.line",
                domain=[("id", "in", order["order_line"])],
                fields=[
                    "id", "product_id", "name", "product_uom_qty", "price_unit",
                    "price_subtotal",
                    "x_studio_ifood_item_id", "x_studio_ifood_observation",
                    "x_studio_ifood_category",
                ],
            )
            order["order_lines"] = lines
        else:
            order["order_lines"] = []

        return {
            "status": "ok",
            "order": order,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get order %s: %s", order_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to get order: {str(e)}")


# ── RESYNC: Buscar pedidos pendentes do iFood e sincronizar ──

@router.post("/resync")
async def resync_pending_orders():
    """Busca pedidos recentes do iFood que nao foram confirmados/sincronizados.

    IMPORTANTE: Para homologacao, quando o iFood ja gerou pedidos mas o
    middleware nao processou (app dormiu, erro, etc), este endpoint:
    1. Busca pedidos dos ultimos 24h no iFood
    2. Confirma cada pedido que ainda nao foi confirmado
    3. Sincroniza com Odoo

    Chame: POST /orders/resync
    """
    logger.info("[RESYNC] === INICIO RESYNC DE PEDIDOS PENDENTES ===")

    results = {
        "status": "ok",
        "fetched": 0,
        "processed": 0,
        "confirmed": 0,
        "synced": 0,
        "errors": [],
        "details": [],
    }

    try:
        async with IFoodAPIClient(settings) as ifood_client:
            # Buscar pedidos das ultimas 24h
            now = datetime.now(timezone.utc)
            date_start = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            date_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            logger.info("[RESYNC] Buscando pedidos de %s ate %s", date_start, date_end)

            try:
                orders_response = await ifood_client.get_orders({
                    "dateStart": date_start,
                    "dateEnd": date_end,
                })
            except Exception as fetch_err:
                logger.error("[RESYNC] FALHA ao buscar pedidos do iFood: %s", fetch_err, exc_info=True)
                results["errors"].append(f"Falha ao buscar pedidos: {str(fetch_err)}")
                return results

            # O iFood pode retornar lista direta ou paginado
            if isinstance(orders_response, list):
                orders_list = orders_response
            elif isinstance(orders_response, dict):
                orders_list = orders_response.get("data", orders_response.get("orders", []))
                if not isinstance(orders_list, list):
                    orders_list = [orders_response] if orders_response.get("id") else []
            else:
                orders_list = []

            results["fetched"] = len(orders_list)
            logger.info("[RESYNC] Encontrados %d pedidos no iFood", len(orders_list))

            odoo_client = OdooClient(settings)
            sync_service = OdooSyncService(odoo_client, ifood_client)

            for order in orders_list:
                if not isinstance(order, dict):
                    continue

                ifood_order_id = str(order.get("id", ""))
                ifood_display_id = str(order.get("displayId", order.get("orderId", "")))
                order_status = order.get("status", "")

                logger.info("[RESYNC] Processando pedido %s (displayId: %s, status: %s)",
                             ifood_order_id, ifood_display_id, order_status)

                order_result = {
                    "ifood_order_id": ifood_order_id,
                    "display_id": ifood_display_id,
                    "status": order_status,
                    "confirmed": False,
                    "synced": False,
                    "error": None,
                }

                try:
                    results["processed"] += 1

                    # 1. Confirmar pedido no iFood se ainda nao foi confirmado
                    if order_status in ("new", "PLACED", "placed"):
                        try:
                            logger.info("[RESYNC] Confirmando pedido %s no iFood...", ifood_order_id)
                            await ifood_client.confirm_order(ifood_order_id)
                            order_result["confirmed"] = True
                            results["confirmed"] += 1
                            logger.info("[RESYNC] Pedido %s CONFIRMADO no iFood", ifood_order_id)
                        except Exception as confirm_err:
                            logger.error("[RESYNC] Falha ao confirmar pedido %s: %s",
                                         ifood_order_id, confirm_err)
                            order_result["error"] = f"Confirm falhou: {str(confirm_err)}"
                    else:
                        order_result["confirmed"] = True
                        logger.info("[RESYNC] Pedido %s ja esta em status '%s' - pulando confirm",
                                     ifood_order_id, order_status)

                    # 2. Sincronizar com Odoo
                    try:
                        existing = odoo_client.search_read(
                            "sale.order",
                            domain=[("x_studio_ifood_order_id", "=", ifood_order_id)],
                            fields=["id"],
                            limit=1,
                        )
                        if existing:
                            logger.info("[RESYNC] Pedido %s ja existe no Odoo (ID: %s) - atualizando status",
                                         ifood_order_id, existing[0]["id"])
                            sync_service.update_order_status(ifood_order_id,
                                                             order_status or "confirmed")
                            order_result["synced"] = True
                            results["synced"] += 1
                        else:
                            logger.info("[RESYNC] Buscando dados completos do pedido %s...", ifood_order_id)
                            full_order = await ifood_client.get_order(ifood_order_id)
                            sale_id = sync_service.sync_order(full_order)
                            sync_service.update_order_status(ifood_order_id,
                                                             order_status or "confirmed")
                            order_result["synced"] = True
                            results["synced"] += 1
                            logger.info("[RESYNC] Pedido %s criado no Odoo (sale.order %s)",
                                         ifood_order_id, sale_id)
                    except Exception as sync_err:
                        logger.error("[RESYNC] Falha ao sincronizar pedido %s: %s",
                                     ifood_order_id, sync_err, exc_info=True)
                        order_result["error"] = f"Sync falhou: {str(sync_err)}"

                except Exception as e:
                    logger.error("[RESYNC] Erro inesperado ao processar pedido %s: %s",
                                 ifood_order_id, e, exc_info=True)
                    order_result["error"] = str(e)

                results["details"].append(order_result)

    except Exception as e:
        logger.error("[RESYNC] Erro fatal no resync: %s", e, exc_info=True)
        results["errors"].append(f"Erro fatal: {str(e)}")

    logger.info("[RESYNC] === RESUMO: buscados=%d | processados=%d | confirmados=%d | sincronizados=%d ===",
                 results["fetched"], results["processed"], results["confirmed"], results["synced"])
    logger.info("[RESYNC] === FIM RESYNC ===")

    return results


# ── POLL: Verificar cancelamentos Odoo → iFood ──────────────

@router.post("/poll-cancellations")
async def poll_odoo_cancellations():
    """Verifica pedidos cancelados no Odoo e propaga o cancelamento ao iFood.

    Busca sale.order onde state='cancel' e x_studio_ifood_status != 'cancelled'.
    Para cada pedido encontrado, chama iFood cancellation/accept com o
    motivo do campo x_studio_ifood_cancel_reason.

    Tambem e chamado automaticamente a cada KEEPALIVE do iFood.
    """
    from app.routes.ifood_webhooks import _check_odoo_pending_cancellations
    return await _check_odoo_pending_cancellations()


# ── Sync manual de pedido especifico ─────────────────────────

@router.post("/sync/{ifood_order_id}")
async def sync_order(ifood_order_id: str, background_tasks: BackgroundTasks):
    """Sincroniza manualmente um pedido do iFood para o Odoo."""
    logger.info("Manual sync triggered for iFood order %s", ifood_order_id)

    try:
        async with IFoodAPIClient(settings) as ifood_client:
            order_data = await ifood_client.get_order(ifood_order_id)

        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)
        sale_order_id = sync_service.sync_order(order_data)

        return {
            "status": "ok",
            "message": f"Order {ifood_order_id} synced to Odoo sale.order {sale_order_id}",
            "ifood_order_id": ifood_order_id,
            "odoo_sale_order_id": sale_order_id,
        }

    except Exception as e:
        logger.error("Failed to sync order %s: %s", ifood_order_id, e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync order {ifood_order_id}: {str(e)}",
        )


# ── Confirmar pedido no iFood ────────────────────────────────

@router.post("/{ifood_order_id}/confirm")
async def confirm_order(ifood_order_id: str):
    """Confirma pedido no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.confirm_order(ifood_order_id)

        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)
        sync_service.update_order_status(ifood_order_id, "confirmed")

        return {"status": "ok", "message": f"Order {ifood_order_id} confirmed", "result": result}

    except Exception as e:
        logger.error("Failed to confirm order %s: %s", ifood_order_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to confirm order: {str(e)}")


# ── Iniciar preparacao ───────────────────────────────────────

@router.post("/{ifood_order_id}/start-preparation")
async def start_preparation(ifood_order_id: str):
    """Inicia preparacao do pedido no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.start_preparation(ifood_order_id)

        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)
        sync_service.update_order_status(ifood_order_id, "preparation_started")

        return {"status": "ok", "message": f"Preparation started for {ifood_order_id}", "result": result}

    except Exception as e:
        logger.error("Failed to start preparation: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to start preparation: {str(e)}")


# ── Pronto para retirada ─────────────────────────────────────

@router.post("/{ifood_order_id}/ready")
async def ready_for_pickup(ifood_order_id: str):
    """Marca pedido como pronto para retirada no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.ready_for_pickup(ifood_order_id)

        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)
        sync_service.update_order_status(ifood_order_id, "ready_to_pickup")

        return {"status": "ok", "message": f"Order {ifood_order_id} ready", "result": result}

    except Exception as e:
        logger.error("Failed to mark order as ready: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to mark order as ready: {str(e)}")


# ── Despachar pedido ─────────────────────────────────────────

@router.post("/{ifood_order_id}/dispatch")
async def dispatch_order(ifood_order_id: str):
    """Despacha pedido no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.dispatch_order(ifood_order_id)

        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)
        sync_service.update_order_status(ifood_order_id, "dispatched")

        return {"status": "ok", "message": f"Order {ifood_order_id} dispatched", "result": result}

    except Exception as e:
        logger.error("Failed to dispatch order %s: %s", ifood_order_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to dispatch order: {str(e)}")


# ── Aceitar cancelamento ─────────────────────────────────────

@router.post("/{ifood_order_id}/accept-cancellation")
async def accept_cancellation(ifood_order_id: str):
    """Aceita solicitacao de cancelamento no iFood e atualiza Odoo."""
    logger.info("[CANCELLATION] Aceitacao manual de cancelamento para pedido %s", ifood_order_id)

    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.accept_cancellation(ifood_order_id)

        logger.info("[CANCELLATION] iFood aceitou o cancelamento: %s", result)

        odoo_client = OdooClient(settings)
        sync_service = OdooSyncService(odoo_client, ifood_client)
        sync_service.update_order_status(ifood_order_id, "cancelled")

        return {
            "status": "ok",
            "message": f"Cancellation accepted for order {ifood_order_id}",
            "ifood_response": result,
        }

    except Exception as e:
        logger.error("[CANCELLATION] Falha ao aceitar cancelamento do pedido %s: %s",
                     ifood_order_id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to accept cancellation: {str(e)}",
        )


# ── Cancelar pedido originado do ODOO ────────────────────────
# Este endpoint e chamado pela Automacao do Odoo quando o usuario
# clica no botao "Cancelar" (action_cancel) em uma cotacao/pedido.
# Tambem pode ser chamado manualmente para forcar a verificacao.

@router.post("/cancel-from-odoo/{ifood_order_id}")
async def cancel_from_odoo(ifood_order_id: str, body: dict = None):
    """Cancela pedido no iFood originado do Odoo (botao Cancelar).

    Chamado pela Automacao do Odoo quando action_cancel e acionado.
    O motivo de cancelamento (codigo 501-509) vem do campo
    x_studio_ifood_cancel_reason da sale.order no Odoo.

    Body esperado: {"reason": "503"}
    """
    reason = ""
    if body and isinstance(body, dict):
        reason = str(body.get("reason", ""))

    IFOOD_REASONS = {
        "501": "Erro no sistema",
        "502": "Pedido duplicado",
        "503": "Item ou loja indisponivel",
        "504": "Sem entregador disponivel",
        "505": "Problema com o cardapio",
        "506": "Endereco fora da area de entrega",
        "507": "Suspeita de fraude",
        "508": "Fora do horario de funcionamento",
        "509": "Erro interno da operacao",
    }

    reason_label = IFOOD_REASONS.get(reason, reason)
    logger.info("[ODOO_CANCEL] Pedido %s | Codigo motivo: %s (%s)",
                ifood_order_id, reason, reason_label)

    try:
        # 1. Cancelar no iFood
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.accept_cancellation(ifood_order_id, reason_code=reason)
        logger.info("[ODOO_CANCEL] iFood confirmou cancelamento: %s", str(result)[:500])

        # 2. Atualizar status no Odoo
        try:
            odoo_client = OdooClient(settings)
            sync_service = OdooSyncService(odoo_client, ifood_client)
            sync_service.update_order_status(ifood_order_id, "cancelled")
        except Exception as odoo_err:
            logger.warning("[ODOO_CANCEL] Falha ao atualizar Odoo (nao critico): %s", odoo_err)

        return {
            "status": "ok",
            "message": f"Order {ifood_order_id} cancelled on iFood",
            "reason_code": reason,
            "reason_label": reason_label,
            "ifood_response": result,
        }

    except Exception as e:
        logger.error("[ODOO_CANCEL] FALHA ao cancelar pedido %s no iFood: %s",
                     ifood_order_id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cancel order on iFood: {str(e)}",
        )


# ── Rejeitar cancelamento ────────────────────────────────────

@router.post("/{ifood_order_id}/reject-cancellation")
async def reject_cancellation(ifood_order_id: str, reason: str = ""):
    """Rejeita solicitacao de cancelamento no iFood."""
    logger.info("[CANCELLATION] Rejeicao manual de cancelamento para pedido %s - Motivo: %s",
                ifood_order_id, reason)

    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.reject_cancellation(ifood_order_id, reason)

        logger.info("[CANCELLATION] iFood rejeitou o cancelamento: %s", result)

        return {
            "status": "ok",
            "message": f"Cancellation rejected for order {ifood_order_id}",
            "ifood_response": result,
        }

    except Exception as e:
        logger.error("[CANCELLATION] Falha ao rejeitar cancelamento do pedido %s: %s",
                     ifood_order_id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reject cancellation: {str(e)}",
        )