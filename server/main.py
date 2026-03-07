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
        if client_id in active_tunnels:
            del active_tunnels[client_id]

@app.websocket("/{client_id}/{path:path}")
async def ws_proxy_handler(websocket: WebSocket, client_id: str, path: str):
    target_client = client_id
    actual_path = path

    # Smart routing for single client
    if target_client not in active_tunnels:
        if len(active_tunnels) == 1:
            target_client = list(active_tunnels.keys())[0]
            actual_path = f"{client_id}/{path}" if path else client_id
        else:
            await websocket.close(code=4004)
            return

    tunnel_ws = active_tunnels[target_client]
    request_id = str(uuid.uuid4())
    
    await websocket.accept()
    logger.info(f"WS Proxy: Upgrading connection for {client_id}/{path} -> {target_client}")

    # Step 1: Tell the Tunnel Client (Teacher Bridge) to open a local WS connection
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
        return

    # Step 2: Create a bi-directional relay
    # We need a way to correlate the messages. 
    # For simplicity in this specific bridge, we'll store the browser WS in a dict
    pending_requests[request_id] = websocket
    
    try:
        while True:
            # Receive from Browser/Student
            data = await websocket.receive_text()
            # Send to Tunnel Client (Teacher Bridge)
            await tunnel_ws.send_text(json.dumps({
                "type": "ws_message",
                "request_id": request_id,
                "data": data
            }))
    except Exception as e:
        logger.info(f"WS Proxy disconnected: {e}")
    finally:
        if request_id in pending_requests:
            del pending_requests[request_id]
        # Notify Tunnel Client to close local connection
        try:
            await tunnel_ws.send_text(json.dumps({
                "type": "ws_close",
                "request_id": request_id
            }))
        except: pass

@app.api_route("/{client_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_handler(client_id: str, path: str, request: Request):
    target_client = client_id
    actual_path = path

    # If the requested client_id is NOT a connected client
    if target_client not in active_tunnels:
        # Check if there is exactly ONE client connected
        if len(active_tunnels) == 1:
            # Fallback: Assume this is a path that belongs to the only connected client
            target_client = list(active_tunnels.keys())[0]
            # Reconstruct the full path
            full_path = f"{client_id}/{path}" if path else client_id
            actual_path = full_path
            logger.info(f"Smart-routing request /{full_path} to single client: {target_client}")
        else:
            return JSONResponse(status_code=404, content={
                "error": f"Tunnel '{client_id}' not found.",
                "available_tunnels": list(active_tunnels.keys())
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
