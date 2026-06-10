"""Main FastAPI application."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pathlib import Path
import shutil

from app.config import load_config
from app.services.task_queue import get_task_queue
from app.services.volume_service import get_volume_service
from app.services.migration_service import get_migration_service
from app.services.backup_service import get_backup_service
from app.api.routes import router

# Load configuration
try:
    config = load_config()
    print(f"[APP] Configuration loaded successfully", flush=True)
except Exception as e:
    print(f"[FATAL] Failed to load configuration: {e}", flush=True)
    raise

# Initialize task queue first so tmp_dir is set before any service touches it
get_task_queue(tmp_dir=config.tmp_dir)
print(f"[APP] Using tmp_dir: {config.tmp_dir}", flush=True)

# Initialize services
get_volume_service(config)
get_migration_service(config, get_volume_service(config))
get_backup_service(config, get_volume_service(config))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _cleanup_orphaned_restore_dirs()
    print(f"[APP] v-shipper started successfully", flush=True)
    print(f"[APP] Listening on port {config.web_ui.port}", flush=True)
    yield
    print(f"[APP] v-shipper shutting down", flush=True)


# Initialize application
app = FastAPI(
    title="v-shipper",
    description="Docker Volume Migration Application",
    version="0.0.8",
    lifespan=lifespan
)


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


def _cleanup_orphaned_restore_dirs():
    """Remove orphaned .restore_temp_* directories from crashed restore operations."""
    try:
        for pool in config.docker_hosts:
            pool_path = Path(pool.pool)
            if not pool_path.exists():
                continue
            
            for item in pool_path.iterdir():
                if item.is_dir() and item.name.startswith('.restore_temp_'):
                    try:
                        shutil.rmtree(item)
                        print(f"[APP] Cleaned up orphaned restore directory: {item}", flush=True)
                    except Exception as e:
                        print(f"[WARNING] Failed to remove {item}: {e}", flush=True)
        
        for pool in config.backup_pools:
            # Skip remote pools - they don't have local paths
            if pool.pool_type == "remote":
                continue
            
            pool_path = Path(pool.pool)
            if not pool_path.exists():
                continue
            
            for item in pool_path.iterdir():
                if item.is_dir() and item.name.startswith('.restore_temp_'):
                    try:
                        shutil.rmtree(item)
                        print(f"[APP] Cleaned up orphaned restore directory: {item}", flush=True)
                    except Exception as e:
                        print(f"[WARNING] Failed to remove {item}: {e}", flush=True)
    except Exception as e:
        print(f"[WARNING] Error during cleanup of orphaned restore dirs: {e}", flush=True)


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config.web_ui.port,
        log_level="info"
    )
