import logging
from datetime import datetime
from typing import Optional

from app.services.odoo_client import OdooClient
from app.services.ifood_api import IFoodAPIClient

logger = logging.getLogger(__name__)


class OdooSyncService:
    """Servico de sincronizacao entre iFood e Odoo.

    Cria/atualiza sale orders, clientes e produtos usando
    campos nativos do Odoo sempre que possivel.
    """

    def __init__(self, odoo_client: OdooClient, ifood_client: IFoodAPIClient) -> None:
        self._odoo = odoo_client
        self._ifood = ifood_client

    # ── Sincronizacao principal ───────────────────────────────

    def sync_order(self, ifood_order: dict) -> int:
        """Sincroniza um pedido do iFood para o Odoo.

        Fluxo:
        1. Cria/encontra o cliente (res.partner)
        2. Cria o sale.order com campos iFood + nativos
        3. Cria as sale.order.line com produtos

        Args:
            ifood_order: Dados completos do pedido iFood.

        Returns:
            ID do sale.order criado no Odoo.
        """
        logger.info("Sincronizando pedido iFood %s para Odoo", ifood_order.get("id", "desconhecido"))

        # 1. Cliente
        customer_data = self._extract_customer_data(ifood_order)
        partner_id = self.create_or_find_customer(customer_data)
        logger.info("Partner ID do cliente: %s", partner_id)

        # 2. Dados do pedido
        order_data = self._extract_order_data(ifood_order)

        # 3. Linhas do pedido
        lines = self._extract_order_lines(ifood_order)

        # 4. Criar sale.order
        sale_order_id = self.create_sale_order(partner_id, order_data, lines)
        logger.info("Criado sale.order ID %s para pedido iFood %s",
                     sale_order_id, ifood_order.get("id"))

        return sale_order_id

    # ── Cliente (res.partner) ────────────────────────────────

    def create_or_find_customer(self, customer_data: dict) -> int:
        """Busca cliente por x_studio_ifood_customer_id ou cria novo.

        Usa campos NATIVOS do Odoo:
        - name, phone, mobile, email, street, street2,
          district/city/state_id/zip/country_id, vat

        Args:
            customer_data: Dicionario com dados do cliente iFood.

        Returns:
            ID do res.partner.
        """
        ifood_customer_id = customer_data.get("ifood_customer_id")

        # Buscar cliente existente pelo ID do iFood
        existing = self._odoo.search_read(
            "res.partner",
            domain=[("x_studio_ifood_customer_id", "=", ifood_customer_id)],
            fields=["id"],
            limit=1,
        )

        if existing:
            partner_id = existing[0]["id"]
            logger.info("Cliente existente encontrado: %s (iFood: %s)", partner_id, ifood_customer_id)

            # Atualizar dados nativos se vieram novos
            update_values = {}
            if customer_data.get("name"):
                update_values["name"] = customer_data["name"]
            if customer_data.get("phone"):
                update_values["phone"] = customer_data["phone"]
            if customer_data.get("mobile"):
                update_values["mobile"] = customer_data["mobile"]
            if customer_data.get("email"):
                update_values["email"] = customer_data["email"]
            if customer_data.get("street"):
                update_values["street"] = customer_data["street"]
            if customer_data.get("street2"):
                update_values["street2"] = customer_data["street2"]
            if customer_data.get("district"):
                update_values["district"] = customer_data["district"]
            if customer_data.get("city"):
                update_values["city"] = customer_data["city"]
            if customer_data.get("zip"):
                update_values["zip"] = customer_data["zip"]
            if customer_data.get("vat"):
                update_values["vat"] = customer_data["vat"]

            if update_values:
                self._odoo.write("res.partner", partner_id, update_values)
                logger.info("Cliente %s atualizado com novos dados", partner_id)

            return partner_id

        # Criar novo cliente usando campos NATIVOS
        partner_values = {
            "name": customer_data.get("name", "Cliente iFood"),
            "customer_rank": 1,
            # Campo customizado: link com iFood
            "x_studio_ifood_customer_id": ifood_customer_id,
        }

        # Campos nativos do endereco
        native_fields = ["phone", "mobile", "email", "street", "street2",
                         "district", "city", "zip", "vat"]
        for field in native_fields:
            if customer_data.get(field):
                partner_values[field] = customer_data[field]

        # Tentar definir estado por sigla
        if customer_data.get("state_code"):
            state_id = self._find_state_id(customer_data["state_code"])
            if state_id:
                partner_values["state_id"] = state_id

        # País Brasil por padrão
        country_id = self._find_country_id("BR")
        if country_id:
            partner_values["country_id"] = country_id

        partner_id = self._odoo.create("res.partner", partner_values)
        logger.info("Novo cliente criado: %s - %s", partner_id, customer_data.get("name"))
        return partner_id

    # ── Pedido (sale.order) ──────────────────────────────────

    def create_sale_order(
        self,
        partner_id: int,
        order_data: dict,
        lines: list,
    ) -> int:
        """Cria sale.order no Odoo com dados do iFood.

        Usa campos NATIVOS: partner_id, state, note
        Usa campos CUSTOM: x_studio_ifood_*

        Args:
            partner_id: ID do res.partner.
            order_data: Dicionario com campos do pedido.
            lines: Lista de linhas do pedido.

        Returns:
            ID do sale.order criado.
        """
        # Verificar se pedido ja existe
        ifood_order_id = order_data.get("x_studio_ifood_order_id")
        existing = self._odoo.search_read(
            "sale.order",
            domain=[("x_studio_ifood_order_id", "=", ifood_order_id)],
            fields=["id"],
            limit=1,
        )

        if existing:
            logger.info("Pedido ja existe: iFood %s -> Odoo %s",
                        ifood_order_id, existing[0]["id"])
            return existing[0]["id"]

        # Preparar valores do pedido
        order_values = {
            "partner_id": partner_id,
            "state": "draft",
        }

        # Adicionar campos customizados do iFood
        ifood_custom_fields = [
            "x_studio_ifood_order_id", "x_studio_ifood_display_id", "x_studio_ifood_order_type",
            "x_studio_ifood_customer_id", "x_studio_ifood_delivery_address", "x_studio_ifood_payment_method",
            "x_studio_ifood_payment_value", "x_studio_ifood_delivery_fee", "x_studio_ifood_subtotal",
            "x_studio_ifood_created_at", "x_studio_ifood_status",
        ]

        for field in ifood_custom_fields:
            if field in order_data and order_data[field] is not None:
                order_values[field] = order_data[field]

        # Criar linhas do pedido usando campos NATIVOS
        order_line_commands = []
        for line_data in lines:
            product_id = self._find_or_create_product(line_data)

            order_line_commands.append((0, 0, {
                # Campos NATIVOS
                "product_id": product_id,
                "product_uom_qty": line_data.get("quantity", 1),     # NATIVO: quantidade
                "price_unit": line_data.get("unit_price", 0.0),      # NATIVO: preco unitario
                "name": line_data.get("name", ""),                   # NATIVO: descricao
                # Campos CUSTOM iFood
                "x_studio_ifood_item_id": line_data.get("ifood_item_id", ""),
                "x_studio_ifood_observation": line_data.get("observation", ""),
                "x_studio_ifood_category": line_data.get("category", ""),
            }))

        order_values["order_line"] = order_line_commands

        sale_order_id = self._odoo.create("sale.order", order_values)
        logger.info("sale.order %s criado com %d linhas", sale_order_id, len(order_line_commands))
        return sale_order_id

    def update_order_status(self, ifood_order_id: str, status: str) -> bool:
        """Atualiza o status iFood no sale.order do Odoo.

        Args:
            ifood_order_id: ID do pedido no iFood.
            status: Novo status.

        Returns:
            True se atualizado com sucesso.
        """
        existing = self._odoo.search_read(
            "sale.order",
            domain=[("x_studio_ifood_order_id", "=", ifood_order_id)],
            fields=["id", "x_studio_ifood_status"],
            limit=1,
        )

        if not existing:
            logger.warning("Pedido iFood %s nao encontrado no Odoo", ifood_order_id)
            return False

        order_id = existing[0]["id"]
        self._odoo.write("sale.order", order_id, {"x_studio_ifood_status": status})
        logger.info("Status do pedido %s atualizado para: %s", order_id, status)
        return True

    # ── Produto (product.product) ────────────────────────────

    def find_product_by_ifood_id(self, ifood_product_id: str) -> Optional[int]:
        """Busca produto pelo ID do iFood.

        Args:
            ifood_product_id: ID do produto no iFood.

        Returns:
            ID do product.product ou None.
        """
        results = self._odoo.search_read(
            "product.product",
            domain=[("x_studio_ifood_product_id", "=", ifood_product_id)],
            fields=["id"],
            limit=1,
        )
        return results[0]["id"] if results else None

    def _find_or_create_product(self, line_data: dict) -> int:
        """Busca ou cria produto para uma linha do pedido.

        Busca por: x_studio_ifood_product_id > default_code (SKU) > name

        Args:
            line_data: Dados da linha com info do produto.

        Returns:
            ID do product.product.
        """
        ifood_item_id = line_data.get("ifood_item_id", "")

        # 1. Buscar por ID do iFood
        product_id = self.find_product_by_ifood_id(ifood_item_id)
        if product_id:
            return product_id

        # 2. Buscar por SKU nativo (default_code)
        sku = line_data.get("sku", "")
        if sku:
            results = self._odoo.search_read(
                "product.product",
                domain=[("default_code", "=", sku)],
                fields=["id"],
                limit=1,
            )
            if results:
                self._odoo.write("product.product", results[0]["id"], {
                    "x_studio_ifood_product_id": ifood_item_id,
                    "x_studio_ifood_synced_at": datetime.utcnow().isoformat(),
                })
                return results[0]["id"]

        # 3. Buscar por nome nativo
        name = line_data.get("name", "")
        if name:
            results = self._odoo.search_read(
                "product.product",
                domain=[("name", "=", name)],
                fields=["id"],
                limit=1,
            )
            if results:
                self._odoo.write("product.product", results[0]["id"], {
                    "x_studio_ifood_product_id": ifood_item_id,
                    "x_studio_ifood_synced_at": datetime.utcnow().isoformat(),
                })
                return results[0]["id"]

        # 4. Criar novo produto
        return self._create_product(line_data)

    def _create_product(self, item_data: dict) -> int:
        """Cria novo produto no Odoo.

        Usa campos NATIVOS: name, default_code, list_price, sale_ok, etc.
        Usa campos CUSTOM: x_studio_ifood_product_id, x_studio_ifood_category, x_studio_ifood_synced_at

        Args:
            item_data: Dados do item iFood.

        Returns:
            ID do product.product criado.
        """
        product_values = {
            # Campos NATIVOS
            "name": item_data.get("name", "Produto iFood"),
            "default_code": item_data.get("sku", ""),       # NATIVO: SKU
            "list_price": item_data.get("unit_price", 0.0),  # NATIVO: preco de venda
            "sale_ok": True,
            "purchase_ok": True,
            "type": "consu",
            # Campos CUSTOM iFood
            "x_studio_ifood_product_id": item_data.get("ifood_item_id", ""),
            "x_studio_ifood_category": item_data.get("category", ""),
            "x_studio_ifood_synced_at": datetime.utcnow().isoformat(),
        }

        product_id = self._odoo.create("product.product", product_values)
        logger.info("Produto criado ID %s: %s", product_id, item_data.get("name", ""))
        return product_id

    # ── Helpers ──────────────────────────────────────────────

    def _find_country_id(self, code: str) -> Optional[int]:
        """Busca ID do pais pelo codigo (ex: 'BR')."""
        results = self._odoo.search_read(
            "res.country",
            domain=[("code", "=", code)],
            fields=["id"],
            limit=1,
        )
        return results[0]["id"] if results else None

    def _find_state_id(self, state_code: str) -> Optional[int]:
        """Busca ID do estado brasileiro pela sigla (ex: 'SP')."""
        results = self._odoo.search_read(
            "res.country.state",
            domain=[("code", "=", state_code)],
            fields=["id"],
            limit=1,
        )
        return results[0]["id"] if results else None

    # ── Extracao de dados do iFood ───────────────────────────

    def _extract_customer_data(self, ifood_order: dict) -> dict:
        """Extrai dados do cliente do pedido iFood.

        Mapeia para campos NATIVOS do res.partner:
        name, phone, mobile, email, street, street2,
        district, city, state_code, zip, vat

        Returns:
            Dicionario pronto para criar/atualizar res.partner.
        """
        customer = ifood_order.get("customer", {}) or {}
        delivery_address = ifood_order.get("deliveryAddress", {}) or {}

        # Nome: tentar customer.name ou deliveryAddress.recipientName
        name = (customer.get("name") or delivery_address.get("recipientName") or "").strip()

        # Telefones
        phone = (customer.get("phone") or delivery_address.get("phone") or "").strip()
        mobile = (customer.get("mobile") or "").strip()

        # Email
        email = (customer.get("email") or "").strip()

        # Documento (CPF/CNPJ)
        vat = (customer.get("document") or customer.get("cpfCnpj") or "").strip()

        # Endereco de entrega
        street = (delivery_address.get("street") or "").strip()
        number = (delivery_address.get("number") or "").strip()
        district = (delivery_address.get("neighborhood") or delivery_address.get("district") or "").strip()
        city = (delivery_address.get("city") or "").strip()
        state_code = (delivery_address.get("state") or "").strip()
        zip_code = (delivery_address.get("zipCode") or "").strip()
        complement = (delivery_address.get("complement") or delivery_address.get("reference") or "").strip()

        # Montar street completa (rua + numero)
        full_street = f"{street}, {number}".strip(", ")

        return {
            "ifood_customer_id": str(customer.get("id", ifood_order.get("customerId", ""))),
            # --- Campos NATIVOS res.partner ---
            "name": name or "Cliente iFood",
            "phone": phone,
            "mobile": mobile,
            "email": email,
            "street": full_street,
            "street2": complement,
            "district": district,
            "city": city,
            "state_code": state_code,
            "zip": zip_code,
            "vat": vat,
        }

    def _extract_order_data(self, ifood_order: dict) -> dict:
        """Extrai dados do pedido iFood.

        Mapeia para campos CUSTOM x_studio_ifood_* do sale.order.

        Returns:
            Dicionario com campos do sale.order.
        """
        customer = ifood_order.get("customer", {}) or {}
        delivery_address = ifood_order.get("deliveryAddress", {}) or {}
        payment = ifood_order.get("payment", {}) or {}

        # Montar endereco de entrega como texto
        address_parts = [
            delivery_address.get("street", ""),
            delivery_address.get("number", ""),
            delivery_address.get("neighborhood", ""),
            delivery_address.get("complement", ""),
            delivery_address.get("city", ""),
            delivery_address.get("state", ""),
            delivery_address.get("zipCode", ""),
            delivery_address.get("reference", ""),
        ]
        address_text = ", ".join(part.strip() for part in address_parts if part and part.strip())

        # Converter data criacao
        created_at = ifood_order.get("createdAt", "")
        if isinstance(created_at, str):
            try:
                created_at = created_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(created_at)
                created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass

        return {
            # --- Campos CUSTOM x_studio_ifood_* ---
            "x_studio_ifood_order_id": str(ifood_order.get("id", "")),
            "x_studio_ifood_display_id": str(ifood_order.get("displayId", "")),
            "x_studio_ifood_order_type": (ifood_order.get("orderType", "") or "").upper(),
            "x_studio_ifood_customer_id": str(customer.get("id", "")),
            "x_studio_ifood_delivery_address": address_text,
            "x_studio_ifood_payment_method": payment.get("method", ""),
            "x_studio_ifood_payment_value": float(payment.get("value", 0) or 0),
            "x_studio_ifood_delivery_fee": float(ifood_order.get("deliveryFee", 0) or 0),
            "x_studio_ifood_subtotal": float(ifood_order.get("subtotal", 0) or 0),
            "x_studio_ifood_created_at": created_at,
            "x_studio_ifood_status": ifood_order.get("status", ""),
        }

    def _extract_order_lines(self, ifood_order: dict) -> list:
        """Extrai linhas do pedido iFood.

        Mapeia para:
        - NATIVOS: name, product_uom_qty, price_unit
        - CUSTOM: x_studio_ifood_item_id, x_studio_ifood_observation, x_studio_ifood_category

        Returns:
            Lista de dicionarios com dados das linhas.
        """
        items = ifood_order.get("items", []) or []
        lines = []

        for item in items:
            quantity = float(item.get("quantity", 1) or 1)
            total_price = float(item.get("totalPrice", 0) or 0)
            unit_price = total_price / quantity if quantity > 0 else total_price

            # Montar observacao (obs + extras + options)
            observation_parts = []
            if item.get("observation"):
                observation_parts.append(str(item["observation"]))

            extras = item.get("extras", []) or []
            for extra in extras:
                extra_name = extra.get("name", "")
                extra_price = extra.get("price", 0)
                if extra_name:
                    observation_parts.append(f"+ {extra_name} (R${extra_price})")

            options = item.get("options", []) or []
            for option in options:
                option_name = option.get("name", "")
                if option_name:
                    observation_parts.append(f"- {option_name}")

            observation = " | ".join(observation_parts)

            # Categoria
            category = ""
            if isinstance(item.get("category"), dict):
                category = item["category"].get("name", "")
            else:
                category = str(item.get("category", ""))

            lines.append({
                # Dados para buscar/criar produto
                "ifood_item_id": str(item.get("id", "") or ""),
                "sku": str(item.get("sku", "") or item.get("id", "")),
                # --- Campos NATIVOS sale.order.line ---
                "name": item.get("name", ""),
                "quantity": quantity,           # -> product_uom_qty
                "unit_price": round(unit_price, 2),  # -> price_unit
                # --- Campos CUSTOM sale.order.line ---
                "observation": observation,      # -> x_studio_ifood_observation
                "category": category,            # -> x_studio_ifood_category
            })

        return lines
