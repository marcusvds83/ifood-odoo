import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.services.ifood_api import IFoodAPIClient
from app.services.odoo_client import OdooClient

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check():
    """General health check endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": settings.app_env,
    }


@router.get("/health/ifood")
async def health_ifood():
    """Test connectivity to the iFood API."""
    try:
        async with IFoodAPIClient(settings) as ifood_client:
            auth = ifood_client._auth_service
            raw_response = await auth.http_client.post(
                auth._config.ifood_auth_url,
                data={
                    "grantType": auth._config.ifood_grant_type,
                    "clientId": auth._config.ifood_client_id,
                    "clientSecret": auth._config.ifood_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            # Mostra a resposta RAW do iFood (sem o valor do token por seguranca)
            token_data = raw_response.json()
            safe_data = {k: (v[:20] + "..." if isinstance(v, str) and len(v) > 20 else v) for k, v in token_data.items()}

            return {
                "status": "ok" if raw_response.status_code == 200 else "error",
                "http_status": raw_response.status_code,
                "ifood_response": safe_data,
                "response_keys": list(token_data.keys()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as e:
        logger.error("iFood health check failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=f"iFood API health check failed: {str(e)}",
        )


@router.get("/health/odoo")
async def health_odoo():
    """Test connectivity to the Odoo instance."""
    try:
        odoo_client = OdooClient(settings)
        uid = odoo_client.authenticate()
        return {
            "status": "ok",
            "service": "odoo",
            "message": "Successfully authenticated with Odoo",
            "uid": uid,
            "url": settings.odoo_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error("Odoo health check failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"Odoo health check failed: {str(e)}",
        )
