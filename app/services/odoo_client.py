import logging
from typing import Any, Optional

import xmlrpc.client

from app.config import Settings

logger = logging.getLogger(__name__)


class OdooClient:
    """Synchronous XML-RPC client for Odoo.

    Provides methods for authenticating and executing operations
    on Odoo models (search, read, create, write, etc.).
    """

    def __init__(self, config: Settings) -> None:
        self._url = config.odoo_url
        self._db = config.odoo_db
        self._user = config.odoo_user
        self._password = config.odoo_password
        self._uid: Optional[int] = None

        self._common = xmlrpc.client.ServerProxy(config.odoo_xmlrpc_url)
        self._models = xmlrpc.client.ServerProxy(config.odoo_xmlrpc_object_url)

    def authenticate(self) -> int:
        """Authenticate with Odoo and return the user ID.

        Returns:
            The authenticated user's ID (uid).

        Raises:
            xmlrpc.client.Fault: If authentication fails.
        """
        try:
            self._uid = self._common.authenticate(self._db, self._user, self._password, {})
            logger.info("Authenticated with Odoo (uid=%s, db=%s)", self._uid, self._db)
            return self._uid
        except xmlrpc.client.Fault as e:
            logger.error("Odoo authentication failed: %s", e)
            raise
        except Exception as e:
            logger.error("Failed to connect to Odoo at %s: %s", self._url, e)
            raise

    def _ensure_authenticated(self) -> int:
        """Ensure we have a valid UID, authenticating if necessary."""
        if self._uid is None:
            self.authenticate()
        return self._uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: Optional[list] = None,
        kwargs: Optional[dict] = None,
    ) -> Any:
        """Execute an XML-RPC call on an Odoo model.

        Args:
            model: Odoo model name (e.g., 'sale.order').
            method: Method name (e.g., 'search_read', 'create').
            args: Positional arguments for the method.
            kwargs: Keyword arguments (dict) for the method.

        Returns:
            The result of the Odoo method call.
        """
        uid = self._ensure_authenticated()
        args = args or []
        kwargs = kwargs or {}

        try:
            result = self._models.execute_kw(
                self._db, uid, self._password, model, method, args, kwargs
            )
            return result
        except xmlrpc.client.Fault as e:
            logger.error("Odoo XML-RPC fault on %s.%s: %s", model, method, e)
            raise
        except Exception as e:
            logger.error("Error calling %s.%s: %s", model, method, e)
            raise

    def search_read(
        self,
        model: str,
        domain: list,
        fields: Optional[list] = None,
        limit: Optional[int] = None,
    ) -> list:
        """Search and read records from an Odoo model.

        Args:
            model: Odoo model name.
            domain: Search domain filter.
            fields: List of field names to return.
            limit: Maximum number of records.

        Returns:
            List of record dictionaries.
        """
        kwargs: dict = {"fields": fields or []}
        if limit is not None:
            kwargs["limit"] = limit

        return self.execute_kw(model, "search_read", [domain], kwargs)

    def create(self, model: str, values: dict) -> int:
        """Create a new record in an Odoo model.

        Args:
            model: Odoo model name.
            values: Dictionary of field values.

        Returns:
            The ID of the created record.
        """
        return self.execute_kw(model, "create", [values])

    def write(self, model: str, record_id: int, values: dict) -> bool:
        """Write values to an existing Odoo record.

        Args:
            model: Odoo model name.
            record_id: ID of the record to update.
            values: Dictionary of field values to update.

        Returns:
            True if the write was successful.
        """
        return self.execute_kw(model, "write", [[record_id], values])

    def search(
        self,
        model: str,
        domain: list,
        limit: Optional[int] = None,
    ) -> list:
        """Search for record IDs in an Odoo model.

        Args:
            model: Odoo model name.
            domain: Search domain filter.
            limit: Maximum number of records.

        Returns:
            List of record IDs.
        """
        kwargs: dict = {}
        if limit is not None:
            kwargs["limit"] = limit

        return self.execute_kw(model, "search", [domain], kwargs)
