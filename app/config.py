from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "case_sensitive": False}

    # iFood OAuth 2.0
    ifood_client_id: str = Field(..., description="iFood API client ID")
    ifood_client_secret: str = Field(..., description="iFood API client secret")
    ifood_grant_type: str = Field(default="client_credentials", description="OAuth grant type")
    ifood_auth_url: str = Field(
        default="https://merchant-api.ifood.com.br/authentication/v1.0/oauth/token",
        description="iFood OAuth token endpoint",
    )
    ifood_api_base_url: str = Field(
        default="https://merchant-api.ifood.com.br",
        description="iFood API base URL",
    )

    # Odoo Connection
    odoo_url: str = Field(..., description="Odoo instance URL")
    odoo_db: str = Field(..., description="Odoo database name")
    odoo_user: str = Field(..., description="Odoo username for XML-RPC")
    odoo_password: str = Field(..., description="Odoo password for XML-RPC")

    # App Settings
    app_env: str = Field(default="development", description="Application environment")
    app_port: int = Field(default=10000, description="Application port")
    app_host: str = Field(default="0.0.0.0", description="Application host")
    secret_key: str = Field(..., description="Secret key for signing")
    log_level: str = Field(default="INFO", description="Logging level")
    webhook_secret: str = Field(default="", description="Secret for validating iFood webhooks")

    @property
    def odoo_xmlrpc_url(self) -> str:
        """Full URL for Odoo XML-RPC common endpoint."""
        return f"{self.odoo_url}/xmlrpc/2/common"

    @property
    def odoo_xmlrpc_object_url(self) -> str:
        """Full URL for Odoo XML-RPC object endpoint."""
        return f"{self.odoo_url}/xmlrpc/2/object"

    @property
    def is_production(self) -> bool:
        """Whether the app is running in production mode."""
        return self.app_env.lower() == "production"


settings = Settings()
