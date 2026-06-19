"""Servico de polling de eventos iFood.

O Firefly Audit valida que o merchant:
1. Le eventos via GET /orders:polling (a cada 30s)
2. Processa cada evento (para CAN: cancellationReasons -> requestCancellation)
3. Confirma via POST /orders:acknowledgment

Este modulo roda como background task e executa esse loop continuamente.
"""

import asyncio
import logging
from typing import Optional

from app.config import settings
from app.services.ifood_api import IFoodAPIClient
from app.routes.ifood_webhooks import _handle_can_background, _handle_plc

logger = logging.getLogger(__name__)

# Controle do loop de polling
_polling_running = False
_polling_task: Optional[asyncio.Task] = None


async def process_polled_event(event: dict, ifood_client: IFoodAPIClient) -> bool:
    """Processa um evento recebido via polling.

    Retorna True se processado com sucesso (deve ser acknowledgado).
    """
    # Extrair dados do evento (formato pode variar)
    event_code = (
        event.get("code")
        or event.get("eventType")
        or event.get("event")
        or event.get("type")
        or ""
    )
    order_id = str(event.get("orderId", "") or "")
    merchant_id = str(event.get("merchantId", "") or "")

    logger.info("[POLLING-PROCESS] Evento: %s | Pedido: %s | Merchant: %s", event_code, order_id, merchant_id)
    logger.info("[POLLING-PROCESS] Payload: %s", str(event)[:2000])

    try:
        # ── CAN / CANCELLATION_REQUESTED ──
        if event_code in ("CAN", "CANCELLATION_REQUESTED", "cancellationRequested",
                          "CancellationRequested", "cancellation_requested"):
            logger.info("[POLLING-PROCESS] Processando cancelamento para pedido %s", order_id)
            await _handle_can_background(order_id, merchant_id, event, str(event).encode())
            return True

        # ── PLC (raramente vem via polling, mas trata se vier) ──
        if event_code in ("PLC", "NEW", "orderCreated", "placed"):
            logger.info("[POLLING-PROCESS] Processando novo pedido %s via polling", order_id)
            await _handle_plc(order_id, merchant_id, event, str(event).encode())
            return True

        # ── CANCELLED ──
        if event_code in ("CANCELLED", "cancelled", "CANCELLATION_ACCEPTED"):
            logger.info("[POLLING-PROCESS] Pedido %s CANCELADO (via polling) - atualizando Odoo", order_id)
            try:
                from app.routes.ifood_webhooks import _update_odoo_status, _update_odoo_state
                _update_odoo_status(order_id, "cancelled")
                _update_odoo_state(order_id, "cancel")
            except Exception as odoo_err:
                logger.error("[POLLING-PROCESS] Falha ao atualizar Odoo: %s", odoo_err)
            return True

        # ── Outros eventos: apenas acknowledge, sem acao extra ──
        logger.info("[POLLING-PROCESS] Evento %s nao requer acao especial, apenas acknowledge", event_code)
        return True

    except Exception as e:
        logger.error("[POLLING-PROCESS] Erro ao processar evento %s para pedido %s: %s",
                     event_code, order_id, e, exc_info=True)
        # Mesmo com erro, tentamos acknowledge para nao ficar em loop
        return True


async def _polling_loop() -> None:
    """Loop principal de polling de eventos iFood.

    Executa a cada 30 segundos:
    1. GET /orders:polling
    2. Processa cada evento
    3. POST /orders:acknowledgment
    """
    global _polling_running
    _polling_running = True
    logger.info("[POLLING] === INICIANDO LOOP DE POLLING (30s) ===")

    while _polling_running:
        try:
            ifood_client = IFoodAPIClient(settings)

            try:
                # Passo 1: Buscar eventos
                events = await ifood_client.poll_events()

                if events:
                    logger.info("[POLLING] %d evento(s) para processar", len(events))

                    # Passo 2: Processar cada evento
                    processed_codes = []
                    for event in events:
                        event_code = (
                            event.get("code")
                            or event.get("fullCode")
                            or event.get("id", "")
                        )
                        success = await process_polled_event(event, ifood_client)
                        if success and event_code:
                            processed_codes.append(event_code)

                    # Passo 3: Acknowledge todos os eventos processados
                    if processed_codes:
                        try:
                            await ifood_client.acknowledge_events(processed_codes)
                            logger.info("[POLLING] Acknowledgment enviado para %d evento(s)", len(processed_codes))
                        except Exception as ack_err:
                            logger.error("[POLLING] Falha no acknowledgment (eventos voltarao): %s", ack_err)

                else:
                    logger.debug("[POLLING] Sem eventos pendentes")

            finally:
                await ifood_client.close()

        except Exception as e:
            logger.error("[POLLING] Erro no ciclo de polling: %s", e, exc_info=True)

        # Aguardar 30 segundos antes do proximo ciclo
        await asyncio.sleep(30)

    logger.info("[POLLING] === LOOP DE POLLING ENCERRADO ===")


def start_polling() -> None:
    """Inicia o loop de polling como background task."""
    global _polling_task, _polling_running
    if _polling_running and _polling_task and not _polling_task.done():
        logger.info("[POLLING] Loop ja esta rodando")
        return
    _polling_task = asyncio.create_task(_polling_loop())
    logger.info("[POLLING] Background task criada")


def stop_polling() -> None:
    """Para o loop de polling."""
    global _polling_running
    _polling_running = False
    logger.info("[POLLING] Solicitada parada do loop de polling")