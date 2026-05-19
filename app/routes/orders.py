import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.config import settings
from app.services.ifood_api import IFoodAPIClient
from app.services.odoo_client import OdooClient
from app.services.odoo_sync import OdooSyncService

logger = logging.getLogger(__name__)

router = APIRouter()


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

        # Buscar linhas do pedido
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


@router.post("/{ifood_order_id}/confirm")
async def confirm_order(ifood_order_id: str):
    """Confirma pedido no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.confirm_order(ifood_order_id)

        sync_service = OdooSyncService(OdooClient(settings), ifood_client)
        sync_service.update_order_status(ifood_order_id, "confirmed")

        return {"status": "ok", "message": f"Order {ifood_order_id} confirmed", "result": result}

    except Exception as e:
        logger.error("Failed to confirm order %s: %s", ifood_order_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to confirm order: {str(e)}")


@router.post("/{ifood_order_id}/start-preparation")
async def start_preparation(ifood_order_id: str):
    """Inicia preparacao do pedido no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.start_preparation(ifood_order_id)

        sync_service = OdooSyncService(OdooClient(settings), ifood_client)
        sync_service.update_order_status(ifood_order_id, "preparation_started")

        return {"status": "ok", "message": f"Preparation started for {ifood_order_id}", "result": result}

    except Exception as e:
        logger.error("Failed to start preparation: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to start preparation: {str(e)}")


@router.post("/{ifood_order_id}/ready")
async def ready_for_pickup(ifood_order_id: str):
    """Marca pedido como pronto para retirada no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.ready_for_pickup(ifood_order_id)

        sync_service = OdooSyncService(OdooClient(settings), ifood_client)
        sync_service.update_order_status(ifood_order_id, "ready_to_pickup")

        return {"status": "ok", "message": f"Order {ifood_order_id} ready", "result": result}

    except Exception as e:
        logger.error("Failed to mark order as ready: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to mark order as ready: {str(e)}")


@router.post("/{ifood_order_id}/dispatch")
async def dispatch_order(ifood_order_id: str):
    """Despacha pedido no iFood."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            result = await ifood_client.dispatch_order(ifood_order_id)

        sync_service = OdooSyncService(OdooClient(settings), ifood_client)
        sync_service.update_order_status(ifood_order_id, "dispatched")

        return {"status": "ok", "message": f"Order {ifood_order_id} dispatched", "result": result}

    except Exception as e:
        logger.error("Failed to dispatch order %s: %s", ifood_order_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to dispatch order: {str(e)}")
