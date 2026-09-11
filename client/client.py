import asyncio
import json
import logging
import httpx
import websockets
import os
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RenderBridge-Client")

# Configuration
SERVER_URL = os.environ.get("RENDERBRIDGE_SERVER", "ws://localhost:8000")
CLIENT_ID = os.environ.get("CLIENT_ID", "my-local-app")
LOCAL_SERVICE_URL = os.environ.get("LOCAL_SERVICE_URL", "http://localhost:8080")
SECRET_TOKEN = os.environ.get("TUNNEL_SECRET_TOKEN", "default_secret_token")

async def forward_request(client: httpx.AsyncClient, request_data: dict):
    """Forward a request from the server to the local service."""
    method = request_data.get("method")
    path = request_data.get("path")
    headers = request_data.get("headers", {})
    body = request_data.get("body")
    
    # Construct local URL
    url = f"{LOCAL_SERVICE_URL.rstrip('/')}/{path.lstrip('/')}"
    if request_data.get("query"):
        url += f"?{request_data['query']}"
        
    logger.info(f"Forwarding {method} {url}")
    
    try:
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
            timeout=25.0
        )
        
        return {
            "type": "response",
            "request_id": request_data.get("request_id"),
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text
        }
    except Exception as e:
        logger.error(f"Error forwarding request: {e}")
        return {
            "type": "response",
            "request_id": request_data.get("request_id"),
            "status_code": 502,
            "body": f"Error from tunnel client: {str(e)}"
        }

async def start_client():
    """Main client loop with auto-reconnect."""
    async with httpx.AsyncClient() as http_client:
        while True:
            try:
                uri = f"{SERVER_URL.rstrip('/')}/tunnel/{CLIENT_ID}?token={SECRET_TOKEN}"
                logger.info(f"Connecting to {SERVER_URL} as {CLIENT_ID}...")
                
                async with websockets.connect(uri) as websocket:
                    logger.info("Connected to RenderBridge Server!")
                    
                    while True:
                        message_str = await websocket.recv()
                        message = json.loads(message_str)
                        
                        if message.get("type") == "request":
                            # Handle incoming proxy request
                            # We can run this in a task to handle multiple concurrent requests
                            asyncio.create_task(
                                handle_and_respond(websocket, http_client, message)
                            )
                        elif message.get("type") == "ping":
                            # Respond to heartbeat
                            await websocket.send(json.dumps({"type": "pong"}))
                            
            except (websockets.ConnectionClosed, ConnectionRefusedError, Exception) as e:
                logger.error(f"Connection lost: {e}. Retrying in 5 seconds...")
                await asyncio.sleep(5)

async def handle_and_respond(websocket, http_client, request_data):
    """Handle a single request and send the response back."""
    response_packet = await forward_request(http_client, request_data)
    try:
        await websocket.send(json.dumps(response_packet))
    except Exception as e:
        logger.error(f"Failed to send response back to server: {e}")

if __name__ == "__main__":
    print("--- RenderBridge Client Starting ---")
    print(f"Local Service: {LOCAL_SERVICE_URL}")
    print(f"Server Target: {SERVER_URL}")
    print(f"Client ID:    {CLIENT_ID}")
    print("------------------------------------")
    try:
        asyncio.run(start_client())
    except KeyboardInterrupt:
        logger.info("Client stopped by user.")
