import os
import json
import uuid
import time
import asyncio
import logging
import base64
import secrets
from typing import Dict, Tuple, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, Request, Response, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse
import httpx

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RenderBridge-Server")

# Server state
server_start_time = time.time()

# Dictionary to manage active tunnels: {client_id: WebSocket}
active_tunnels: Dict[str, WebSocket] = {}

# Dictionary to track pending requests: {request_id: (Future, client_id)}
pending_requests: Dict[str, Tuple[asyncio.Future, str]] = {}

# Dictionary to manage external WebSocket proxy sessions: {request_id: WebSocket}
active_ws_sessions: Dict[str, WebSocket] = {}

# Dynamic self-URL detection for keep-alive (if env vars are not pre-configured)
detected_self_url: Optional[str] = None

# Secret token for client authentication
SECRET_TOKEN = os.environ.get("TUNNEL_SECRET_TOKEN", "default_secret_token")

# Max request payload size limit (default 50MB to prevent Render free-tier 512MB OOM crash)
MAX_PAYLOAD_SIZE = int(os.environ.get("MAX_PAYLOAD_SIZE", 50 * 1024 * 1024))

def update_detected_url(request: Request):
    """Auto-detect public external URL from incoming requests for keep-alive."""
    global detected_self_url
    if not detected_self_url and not os.environ.get("RENDER_EXTERNAL_URL") and not os.environ.get("SELF_URL"):
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        proto = request.headers.get("x-forwarded-proto") or "https"
        if host and "localhost" not in host and "127.0.0.1" not in host:
            detected_self_url = f"{proto}://{host}"
            logger.info(f"Auto-detected external URL for keep-alive: {detected_self_url}")

async def heartbeat_sender():
    """Background task to send pings to all active tunnels every 20 seconds."""
    while True:
        await asyncio.sleep(20)
        for client_id, ws in list(active_tunnels.items()):
            try:
                await ws.send_text(json.dumps({"type": "ping"}))
            except Exception as e:
                logger.warning(f"Failed to ping client {client_id}, cleaning up: {e}")
                if client_id in active_tunnels:
                    try:
                        await ws.close()
                    except:
                        pass
                    del active_tunnels[client_id]

async def keep_alive_task():
    """Self-ping to keep Render free tier service awake (Render sleeps after 15m)."""
    logger.info("Keep-alive background task started.")
    headers = {"User-Agent": "RenderBridge-KeepAlive", "X-Keep-Alive": "true"}
    async with httpx.AsyncClient(headers=headers) as client:
        while True:
            # 540s (9 minutes) gives a solid safety buffer before the 15-minute Render sleep threshold
            await asyncio.sleep(540)
            target_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SELF_URL") or detected_self_url
            if not target_url:
                logger.debug("Keep-alive idle: waiting for first request or env var.")
                continue
                
            try:
                resp = await client.get(target_url, timeout=15.0)
                logger.info(f"Keep-alive self-ping successful: {target_url} (HTTP {resp.status_code})")
            except Exception as e:
                logger.warning(f"Keep-alive self-ping attempt failed for {target_url}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern lifespan management replacing deprecated @app.on_event."""
    hb_task = asyncio.create_task(heartbeat_sender())
    ka_task = asyncio.create_task(keep_alive_task())
    logger.info("RenderBridge Server initialized.")
    yield
    # Graceful shutdown
    hb_task.cancel()
    ka_task.cancel()
    for client_id, ws in list(active_tunnels.items()):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except:
            pass
    active_tunnels.clear()
    logger.info("RenderBridge Server shutdown complete.")

app = FastAPI(title="RenderBridge Server", lifespan=lifespan)

@app.get("/")
@app.get("/health")
async def root(request: Request):
    """Health check & status dashboard, or automatic proxy based on cookie."""
    update_detected_url(request)

    # 檢查 Cookie 是否記錄了活躍客戶端，若是則自動代理根目錄請求
    cookie_client = request.cookies.get("tunnel_client")
    if cookie_client and cookie_client in active_tunnels:
        logger.info(f"根目錄自動路由：依據 Cookie 導向至 {cookie_client}")
        return await proxy_handler(cookie_client, "", request)

    target_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SELF_URL") or detected_self_url
    return {
        "status": "online",
        "uptime_seconds": int(time.time() - server_start_time),
        "active_tunnels": list(active_tunnels.keys()),
        "pending_requests": len(pending_requests),
        "keep_alive": {
            "enabled": True,
            "target_url": target_url or "Waiting for first incoming request or SELF_URL env var",
            "interval_seconds": 540
        },
        "message": "RenderBridge 隧道伺服器運行中 (已啟用 Cookie 記憶與自適應防休眠)",
        "hint": "請使用 https://您的網址/您的ID/ 進行初始連線。"
    }

@app.websocket("/tunnel/{client_id}")
async def tunnel_endpoint(websocket: WebSocket, client_id: str, token: str = None):
    """WebSocket control & data tunnel for local clients."""
    if token is None:
        token = websocket.query_params.get("token")
    if token is None:
        token = websocket.headers.get("X-Tunnel-Token")
        
    # Constant-time token validation against timing attacks
    if not token or not secrets.compare_digest(token, SECRET_TOKEN):
        await websocket.close(code=4003)  # Forbidden
        logger.warning(f"Unauthorized connection attempt for client: {client_id}")
        return

    await websocket.accept()
    
    # Close duplicate connection if client_id already exists
    if client_id in active_tunnels:
        try:
            await active_tunnels[client_id].close(code=1000, reason="Replaced by new connection")
        except:
            pass

    active_tunnels[client_id] = websocket
    logger.info(f"Tunnel established for client: {client_id}")
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            
            # Handle HTTP Response from Client
            if msg_type == "response":
                request_id = message.get("request_id")
                if request_id in pending_requests:
                    future, _ = pending_requests[request_id]
                    if future and not future.done():
                        future.set_result(message)
            
            # Handle Pong (Heartbeat)
            elif msg_type == "pong":
                pass

            # Handle WebSocket Message from Local Client to External Browser
            elif msg_type == "ws_message":
                request_id = message.get("request_id")
                if request_id in active_ws_sessions:
                    try:
                        await active_ws_sessions[request_id].send_text(message.get("data", ""))
                    except Exception as e:
                        logger.debug(f"Failed to forward WS message to browser: {e}")

            # Handle WebSocket Close from Local Client
            elif msg_type == "ws_close":
                request_id = message.get("request_id")
                if request_id in active_ws_sessions:
                    try:
                        await active_ws_sessions[request_id].close()
                    except:
                        pass
                    active_ws_sessions.pop(request_id, None)
                
    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
    except Exception as e:
        logger.error(f"Error in tunnel {client_id}: {e}")
    finally:
        if active_tunnels.get(client_id) == websocket:
            del active_tunnels[client_id]
            
        # Fast-fail all pending HTTP requests waiting on this disconnected client
        orphaned = [req_id for req_id, (_, cid) in pending_requests.items() if cid == client_id]
        for req_id in orphaned:
            future, _ = pending_requests.pop(req_id, (None, None))
            if future and not future.done():
                future.set_exception(HTTPException(status_code=502, detail="Tunnel client disconnected abruptly"))

@app.websocket("/{client_id}/{path:path}")
async def ws_proxy_handler(websocket: WebSocket, client_id: str, path: str):
    """Proxy external WebSocket connections down through the tunnel to the local service."""
    target_client = client_id
    actual_path = path

    # 1. 檢查目標 client 是否在 active_tunnels
    if target_client not in active_tunnels:
        # 嘗試 Cookie 路由
        cookie_client = websocket.cookies.get("tunnel_client")
        if cookie_client and cookie_client in active_tunnels:
            target_client = cookie_client
            actual_path = f"{client_id}/{path}" if path else client_id
        elif len(active_tunnels) == 1:
            target_client = list(active_tunnels.keys())[0]
            actual_path = f"{client_id}/{path}" if path else client_id
        else:
            await websocket.close(code=4004)
            return

    tunnel_ws = active_tunnels[target_client]
    request_id = str(uuid.uuid4())
    
    await websocket.accept()
    logger.info(f"WS Proxy: Upgrading connection for {client_id}/{path} -> {target_client}")

    # Register external browser websocket session
    active_ws_sessions[request_id] = websocket

    tunnel_packet = {
        "type": "ws_open",
        "request_id": request_id,
        "path": actual_path,
        "query": str(websocket.query_params),
        "headers": dict(websocket.headers)
    }
    
    try:
        await tunnel_ws.send_text(json.dumps(tunnel_packet))
    except:
        await websocket.close()
        active_ws_sessions.pop(request_id, None)
        return

    try:
        while True:
            data = await websocket.receive_text()
            await tunnel_ws.send_text(json.dumps({
                "type": "ws_message",
                "request_id": request_id,
                "data": data
            }))
    except Exception as e:
        logger.info(f"WS Proxy disconnected: {e}")
    finally:
        active_ws_sessions.pop(request_id, None)
        try:
            await tunnel_ws.send_text(json.dumps({
                "type": "ws_close",
                "request_id": request_id
            }))
        except:
            pass

@app.api_route("/{client_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_handler(client_id: str, path: str, request: Request):
    """Proxy HTTP requests through the tunnel."""
    update_detected_url(request)
    target_client = client_id
    actual_path = path
    id_from_url = True

    # If the requested client_id is NOT a connected client
    if target_client not in active_tunnels:
        id_from_url = False
        # 1. 嘗試從 Cookie 讀取記憶中的 Client ID
        cookie_client = request.cookies.get("tunnel_client")
        if cookie_client and cookie_client in active_tunnels:
            target_client = cookie_client
            actual_path = f"{client_id}/{path}" if path else client_id
            logger.info(f"Cookie 路由：依據 Cookie 導向至 {target_client} (路徑: {actual_path})")
        # 2. Smart routing fallback if exactly ONE client is connected
        elif len(active_tunnels) == 1:
            target_client = list(active_tunnels.keys())[0]
            actual_path = f"{client_id}/{path}" if path else client_id
            logger.info(f"Smart-routing request /{actual_path} to single client: {target_client}")
        else:
            return JSONResponse(status_code=404, content={
                "error": f"找不到客戶端 '{client_id}' 且無有效會話 Cookie。",
                "message": "請先存取 https://您的網址/您的客戶端ID/ 以建立連線記憶。",
                "available_tunnels": list(active_tunnels.keys())
            })

    websocket = active_tunnels[target_client]
    request_id = str(uuid.uuid4())
    
    # Check Content-Length header against max payload size
    content_len = request.headers.get("content-length")
    if content_len and int(content_len) > MAX_PAYLOAD_SIZE:
        return JSONResponse(
            status_code=413,
            content={"error": f"Payload Too Large (Max {MAX_PAYLOAD_SIZE // (1024*1024)}MB)"}
        )

    # Extract headers (excluding host)
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    
    # Read body bytes with payload limit guard
    body_bytes = await request.body()
    if len(body_bytes) > MAX_PAYLOAD_SIZE:
        return JSONResponse(
            status_code=413,
            content={"error": f"Payload Too Large (Max {MAX_PAYLOAD_SIZE // (1024*1024)}MB)"}
        )

    body_b64 = base64.b64encode(body_bytes).decode('utf-8') if body_bytes else None

    tunnel_packet = {
        "type": "request",
        "request_id": request_id,
        "method": request.method,
        "path": actual_path,
        "query": str(request.query_params),
        "headers": headers,
        "body": body_b64,
        "is_base64": True
    }
    
    # Create future and track with target_client for fast-fail cleanup
    future = asyncio.get_event_loop().create_future()
    pending_requests[request_id] = (future, target_client)
    
    # Send request to tunnel client
    try:
        await websocket.send_text(json.dumps(tunnel_packet))
    except Exception as e:
        logger.error(f"Failed to send packet to client {target_client}: {e}")
        pending_requests.pop(request_id, None)
        return JSONResponse(status_code=502, content={"error": "Failed to reach tunnel client."})
    
    try:
        # Wait for response with 60s timeout
        response_data = await asyncio.wait_for(future, timeout=60.0)
        
        # Decode response body from base64
        resp_body_b64 = response_data.get("body")
        resp_body_bytes = base64.b64decode(resp_body_b64) if resp_body_b64 else b""
        
        # Prepare response headers
        resp_headers = dict(response_data.get("headers", {}))
        
        # Remove Content-Length to let FastAPI/Starlette recalculate correctly
        resp_headers.pop("content-length", None)
        resp_headers.pop("Content-Length", None)
        
        response = Response(
            content=resp_body_bytes,
            status_code=response_data.get("status_code", 200),
            headers=resp_headers
        )

        # 若是由明確的 Client ID 路徑進入，為瀏覽器設置 Cookie 記憶 (有效期 24 小時)
        if id_from_url:
            response.set_cookie(
                key="tunnel_client",
                value=target_client,
                max_age=3600 * 24,
                httponly=True,
                samesite="lax",
                secure=True
            )

        return response
    except HTTPException as he:
        # Client abruptly disconnected
        return JSONResponse(status_code=he.status_code, content={"error": he.detail})
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={"error": "Gateway Timeout: Client took too long to respond."})
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return JSONResponse(status_code=500, content={"error": "Internal Proxy Error"})
    finally:
        pending_requests.pop(request_id, None)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
