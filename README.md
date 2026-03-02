# RenderBridge: A Custom Reverse Proxy Tunnel

RenderBridge allows you to expose your local services to the internet using a server deployed on Render (or any cloud provider) as a bridge.

## Project Structure

```text
render-bridge/
├── server/
│   ├── main.py          # FastAPI Server (deploy to Render)
│   └── requirements.txt
├── client/
│   ├── client.py        # Local Client (runs on your machine)
│   └── requirements.txt
└── README.md
```

## Setup Instructions

### 1. Server Setup (Cloud)
- Deploy the `server/` directory to Render as a **Web Service**.
- Set the `TUNNEL_SECRET_TOKEN` environment variable on Render.
- Render will provide you with a URL (e.g., `https://your-app.onrender.com`).

### 2. Client Setup (Local)
- Install dependencies: `pip install -r client/requirements.txt`
- Set environment variables:
  - `RENDERBRIDGE_SERVER`: `wss://your-app.onrender.com`
  - `CLIENT_ID`: A unique name for your tunnel (e.g., `my-app`).
  - `LOCAL_SERVICE_URL`: The URL of your local service (e.g., `http://localhost:8080`).
  - `TUNNEL_SECRET_TOKEN`: Must match the server's token.
- Run the client: `python client/client.py`

### 3. Accessing Your Local Service
Once connected, anyone can access your local service at:
`https://your-app.onrender.com/my-app/`

All requests to this path will be forwarded to your local machine.

## Key Features
- **WebSocket Tunneling**: Bypasses firewalls and NAT.
- **Request ID Tracking**: Supports concurrent requests.
- **Heartbeat Mechanism**: Keeps the connection alive.
- **Auto-reconnect**: Resilience against network drops.
