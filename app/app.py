"""Main FastAPI application."""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pathlib import Path
import os

from app.config import load_config
from app.services.volume_service import get_volume_service
from app.services.migration_service import get_migration_service
from app.services.backup_service import get_backup_service
from app.api.routes import router

# Initialize application
app = FastAPI(
    title="v-shipper",
    description="Docker Volume Migration Application",
    version="1.0.0"
)

# Load configuration
try:
    config = load_config()
    print(f"[APP] Configuration loaded successfully", flush=True)
except Exception as e:
    print(f"[FATAL] Failed to load configuration: {e}", flush=True)
    raise


# Initialize services
get_volume_service(config)
get_migration_service(config, get_volume_service(config))
get_backup_service(config, get_volume_service(config))


# Mount static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
else:
    print(f"[WARNING] Static directory not found: {static_dir}", flush=True)


# Include API routes
app.include_router(router)


# Serve index.html on root path
@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve main UI."""
    template_path = Path(__file__).parent / "templates" / "index.html"
    
    try:
        with open(template_path) as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Template not found</h1>"


# CORS middleware for development
@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    """Add CORS headers."""
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@app.on_event("startup")
async def startup_event():
    """Startup event."""
    print(f"[APP] v-shipper started successfully", flush=True)
    print(f"[APP] Listening on port {config.web_ui.port}", flush=True)


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown event."""
    print(f"[APP] v-shipper shutting down", flush=True)


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config.web_ui.port,
        log_level="info"
    )
