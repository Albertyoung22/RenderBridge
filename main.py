import os
import json
import uuid
import asyncio
import logging
import base64
from typing import Dict
from fastapi import FastAPI, WebSocket, Request, Response, WebSocketDisconnect
from fastapi.responses import JSONResponse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RenderBridge-Server")

app = FastAPI(title="RenderBridge Server")

# Dictionary to manage active tunnels: {client_id: WebSocket}
active_tunnels: Dict[str, WebSocket] = {}

# Dictionary to track pending requests: {request_id: Future}
pending_requests: Dict[str, asyncio.Future] = {}

# Secret token for client authentication (should be set via environment variable)
SECRET_TOKEN = os.environ.get("TUNNEL_SECRET_TOKEN", "default_secret_token")

@app.get("/")
async def root():
    return {
        "status": "online",
        "active_tunnels": list(active_tunnels.keys()),
        "message": "RenderBridge Reverse Proxy Tunnel is running."
    }

@app.websocket("/tunnel/{client_id}")
async def tunnel_endpoint(websocket: WebSocket, client_id: str, token: str = None):
    # Simple token validation (can be passed via query param or headers)
    if token != SECRET_TOKEN:
        await websocket.close(code=4003) # Forbidden
        logger.warning(f"Unauthorized connection attempt for client: {client_id}")
        return

    # CRITICAL: Prevent ID collisions. Disconnect existing client if same ID connects.
    if client_id in active_tunnels:
        logger.info(f"Closing existing connection for client_id: {client_id} to allow new connection.")
        try:
            old_ws = active_tunnels[client_id]
            await old_ws.close(code=1000, reason="New connection with same ID")
        except:
            pass
        del active_tunnels[client_id]

    await websocket.accept()
    active_tunnels[client_id] = websocket
    logger.info(f"Tunnel established for client: {client_id}")
    
    try:
        while True:
            # Wait for messages from the client (Responses to proxied requests)
            data = await websocket.receive_text()
            message = json.loads(data)
            
            # Handle Response from Client
            if message.get("type") == "response":
                request_id = message.get("request_id")
                if request_id in pending_requests:
                    pending_requests[request_id].set_result(message)
            
            # Handle Pong (Heartbeat)
            elif message.get("type") == "pong":
                # Heartbeat received, no action needed for now
                pass
                
    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
    except Exception as e:
        logger.error(f"Error in tunnel {client_id}: {e}")
    finally:
        # Only delete if it's the SAME websocket instance (to avoid race conditions)
        if active_tunnels.get(client_id) == websocket:
            del active_tunnels[client_id]

@app.api_route("/{client_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_handler(client_id: str, path: str, request: Request):
    target_client = client_id
    actual_path = path

    # 如果請求的 ID 不在線
    if target_client not in active_tunnels:
        # 方便功能：只要有任一 Client 在線，就自動選一個（最後連線的）來處理
        if len(active_tunnels) > 0:
            # 優先使用歷史連線中最晚加入的那個
            target_client = list(active_tunnels.keys())[-1]
            
            # 重構路徑：如果是沒帶 ID 的請求，把 client_id 部分也當作 path 的一部分
            full_path = f"{client_id}/{path}" if path else client_id
            actual_path = full_path
            
            logger.info(f"自動路由：將 /{full_path} 轉發至預設客戶端 {target_client} (總在線: {len(active_tunnels)})")
        else:
            return JSONResponse(status_code=404, content={
                "error": "目前沒有任何客戶端連線，請啟動本地 RenderBridge 程式。",
                "available_tunnels": []
            })

    websocket = active_tunnels[target_client]
    request_id = str(uuid.uuid4())
    
    # Extract headers (excluding host)
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    
    # CRITICAL: Use Base64 for ALL bodies to support binary/MP3
    body_bytes = await request.body()
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
    
    # Send request to client
    try:
        await websocket.send_text(json.dumps(tunnel_packet))
    except Exception as e:
        logger.error(f"Failed to send packet to client {client_id}: {e}")
        return JSONResponse(status_code=502, content={"error": "Failed to reach tunnel client."})
    
    # Create a future to wait for the client's response
    future = asyncio.get_event_loop().create_future()
    pending_requests[request_id] = future
    
    try:
        # Wait for response from client with increased timeout for larger files
        response_data = await asyncio.wait_for(future, timeout=60.0)
        
        # Decode response body from base64
        resp_body_b64 = response_data.get("body")
        resp_body_bytes = base64.b64decode(resp_body_b64) if resp_body_b64 else b""
        
        # Prepare response headers
        resp_headers = dict(response_data.get("headers", {}))
        
        # CRITICAL: Remove Content-Length because the original length (from local)
        # doesn't match the final processed length after tunnel transport.
        # FastAPI/Starlette will recalculate this correctly.
        resp_headers.pop("content-length", None)
        resp_headers.pop("Content-Length", None)
        
        # Construct and return the response
        return Response(
            content=resp_body_bytes,
            status_code=response_data.get("status_code", 200),
            headers=resp_headers
        )
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={"error": "Gateway Timeout: Client took too long to respond."})
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return JSONResponse(status_code=500, content={"error": "Internal Proxy Error"})
    finally:
        if request_id in pending_requests:
            del pending_requests[request_id]

async def heartbeat_sender():
    """Background task to send pings to all active tunnels."""
    while True:
        for client_id, ws in list(active_tunnels.items()):
            try:
                await ws.send_text(json.dumps({"type": "ping"}))
            except:
                # If sending fails, the disconnect will be handled in the websocket loop
                pass
        await asyncio.sleep(20) # Ping every 20 seconds

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(heartbeat_sender())

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # Note: On Render, you usually don't run __main__, uvicorn handles it.
    uvicorn.run(app, host="0.0.0.0", port=port)
