import os

# Worker class: Use Uvicorn's ASGI worker for FastAPI & WebSocket support
worker_class = "uvicorn.workers.UvicornWorker"

# Bind to 0.0.0.0 and the port provided by Render ($PORT)
port = os.environ.get("PORT", "10000")
bind = f"0.0.0.0:{port}"

# Concurrency & timeout settings
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
timeout = 120
keepalive = 65
