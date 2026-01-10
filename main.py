from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import asyncio
import sys
import os
from pathlib import Path

sys.path.append(os.path.dirname(__file__))
from routers import cart, delivery, debug
from database import get_db_connection
from routers import cart_router 
# Import the auto-cancel function from cart router
from routers.cart import auto_cancel_expired_oos_orders

# Global task reference
_auto_cancel_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager to start/stop background tasks
    """
    global _auto_cancel_task
    
    # Startup: Start the auto-cancel background task
    print("🚀 [SERVER RESTART DETECTED] Starting OOS auto-cancel background task...")
    print("🚀 [SERVER RESTART DETECTED] BOGO flag logging should now be active!")
    _auto_cancel_task = asyncio.create_task(auto_cancel_expired_oos_orders())
    print("✅ OOS auto-cancel task started")
    
    yield  # Application runs here
    
    # Shutdown: Stop the background task
    print("Stopping OOS auto-cancel background task...")
    if _auto_cancel_task:
        _auto_cancel_task.cancel()
        try:
            await _auto_cancel_task
        except asyncio.CancelledError:
            pass
    print(" OOS auto-cancel task stopped")

app = FastAPI(
    title="Ordering Service",
    lifespan=lifespan  # Register lifespan handler
)

# CORS config
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5000",
        "http://192.168.100.14:5000",
        "http://127.0.0.1:4000",
        "http://localhost:4000",
        "http://127.0.0.1:7004",
        "http://localhost:7004",
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "http://127.0.0.1:7005",
        "http://localhost:7005",
        "http://localhost:4001",
        "http://localhost:9000",  # Add POS service
        "http://127.0.0.1:9000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include cart routes
app.include_router(cart.router, prefix="/cart", tags=["Orders"])
app.include_router(delivery.router, prefix="/delivery")
app.include_router(cart_router.router)
app.include_router(debug.router)

# Create uploads directory if it doesn't exist
uploads_dir = Path("uploads")
uploads_dir.mkdir(exist_ok=True)

# Mount static files for delivery images
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Health check
@app.get("/")
async def root():
    return {
        "message": "Ordering Service Running",
        "auto_cancel_enabled": _auto_cancel_task is not None and not _auto_cancel_task.done()
    }

# Run locally
if __name__ == "__main__":
    import uvicorn
    print("--- Starting OOS Service on http://127.0.0.1:7004 ---")
    print("Auto-cancel will start automatically on startup")
    uvicorn.run("main:app", host="127.0.0.1", port=7004, reload=True)