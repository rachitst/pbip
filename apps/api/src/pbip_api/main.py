"""Application factory."""

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from pbip_api import __version__
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.errors import register_error_handlers
from pbip_api.core.logging import configure_logging
from pbip_api.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from pbip_api.modules.accounts.router import router as accounts_router
from pbip_api.modules.auth.admin_router import router as admin_router
from pbip_api.modules.auth.router import router as auth_router
from pbip_api.modules.compliance.router import router as compliance_router
from pbip_api.modules.customers.router import router as customers_router
from pbip_api.modules.fraud.router import router as fraud_router
from pbip_api.modules.health.router import router as health_router
from pbip_api.modules.notifications.router import router as notifications_router
from pbip_api.modules.payees.router import router as payees_router
from pbip_api.modules.payments.router import router as payments_router

ROUTERS = (
    health_router,
    auth_router,
    admin_router,
    customers_router,
    accounts_router,
    payees_router,
    payments_router,
    fraud_router,
    compliance_router,
    notifications_router,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    docs = settings.docs_enabled
    app = FastAPI(
        title="PBIP API",
        version=__version__,
        description="Personal Banking Intelligence Platform - backend API",
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )

    # Middleware order: the LAST added is the OUTERMOST. Request context must wrap everything.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Request-ID",
            "X-Device-ID",
        ],
        expose_headers=["X-Request-ID", "Idempotent-Replayed"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookie_secure)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus scrape endpoint. Not proxied by nginx; internal Docker network only."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
