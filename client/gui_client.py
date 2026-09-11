import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import asyncio
import threading
import json
import os
import websockets
import httpx
import logging
import uuid
import base64
import datetime

# Configuration file path
CONFIG_FILE = "config.json"

class RenderBridgeGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("🚀 RenderBridge Client")
        self.root.geometry("600x550")
        self.root.configure(bg="#f0f2f5")
        
        self.loop = None
        self.client_thread = None
        self.is_running = False
        
        self.setup_ui()
        self.load_config()

    def setup_ui(self):
        style = ttk.Style()
        style.configure("TLabel", background="#f0f2f5", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10, "bold"))
        
        # Header
        header = tk.Label(self.root, text="RenderBridge Tunnel Client", font=("Segoe UI", 16, "bold"), bg="#1a73e8", fg="white", pady=10)
        header.pack(fill=tk.X)

        # Form Frame
        form_frame = tk.Frame(self.root, bg="#f0f2f5", padx=20, pady=20)
        form_frame.pack(fill=tk.X)

        # Server URL
        tk.Label(form_frame, text="Server URL (WSS/WS):", bg="#f0f2f5", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky=tk.W, pady=5)
        self.server_url_entry = ttk.Entry(form_frame, width=50)
        self.server_url_entry.grid(row=0, column=1, pady=5, padx=5)
        tk.Label(form_frame, text="Example: wss://your-app.onrender.com", fg="#666", bg="#f0f2f5", font=("Segoe UI", 8)).grid(row=1, column=1, sticky=tk.W)

        # Client ID
        tk.Label(form_frame, text="Client ID (Subpath):", bg="#f0f2f5", font=("Segoe UI", 9, "bold")).grid(row=2, column=0, sticky=tk.W, pady=5)
        self.client_id_entry = ttk.Entry(form_frame, width=50)
        self.client_id_entry.grid(row=2, column=1, pady=5, padx=5)
        tk.Label(form_frame, text="Example: zi-wei (Access via URL/zi-wei/)", fg="#666", bg="#f0f2f5", font=("Segoe UI", 8)).grid(row=3, column=1, sticky=tk.W)

        # Local Service URL
        tk.Label(form_frame, text="Local Service URL:", bg="#f0f2f5", font=("Segoe UI", 9, "bold")).grid(row=4, column=0, sticky=tk.W, pady=5)
        self.local_service_entry = ttk.Entry(form_frame, width=50)
        self.local_service_entry.grid(row=4, column=1, pady=5, padx=5)
        tk.Label(form_frame, text="Example: http://localhost:8080", fg="#666", bg="#f0f2f5", font=("Segoe UI", 8)).grid(row=5, column=1, sticky=tk.W)

        # Secret Token
        tk.Label(form_frame, text="Secret Token:", bg="#f0f2f5", font=("Segoe UI", 9, "bold")).grid(row=6, column=0, sticky=tk.W, pady=5)
        self.token_entry = ttk.Entry(form_frame, width=50, show="*")
        self.token_entry.grid(row=6, column=1, pady=5, padx=5)

        # Control Buttons
        btn_frame = tk.Frame(self.root, bg="#f0f2f5")
        btn_frame.pack(pady=10)

        self.start_btn = tk.Button(btn_frame, text="START TUNNEL", command=self.toggle_tunnel, bg="#28a745", fg="white", font=("Segoe UI", 10, "bold"), width=20, relief=tk.FLAT)
        self.start_btn.pack(side=tk.LEFT, padx=10)

        # Status Indicator
        self.status_label = tk.Label(self.root, text="● Status: Disconnected", fg="#dc3545", bg="#f0f2f5", font=("Segoe UI", 10, "bold"))
        self.status_label.pack()

        # Log Area
        tk.Label(self.root, text="Logs:", bg="#f0f2f5", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, padx=20)
        self.log_area = scrolledtext.ScrolledText(self.root, height=10, width=70, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log_area.pack(padx=20, pady=5)

    def log(self, message):
        self.log_area.insert(tk.END, f"[{self.get_time()}] {message}\n")
        self.log_area.see(tk.END)

    def get_time(self):
        import datetime
        return datetime.datetime.now().strftime("%H:%M:%S")

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    config = json.load(f)
                    self.server_url_entry.insert(0, config.get("server_url", ""))
                    self.client_id_entry.insert(0, config.get("client_id", ""))
                    self.local_service_entry.insert(0, config.get("local_service_url", "http://localhost:8080"))
                    self.token_entry.insert(0, config.get("token", ""))
            except:
                pass

    def save_config(self):
        config = {
            "server_url": self.server_url_entry.get(),
            "client_id": self.client_id_entry.get(),
            "local_service_url": self.local_service_entry.get(),
            "token": self.token_entry.get()
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f)

    def toggle_tunnel(self):
        if not self.is_running:
            self.start_tunnel()
        else:
            self.stop_tunnel()

    def start_tunnel(self):
        self.save_config()
        server_url = self.server_url_entry.get()
        client_id = self.client_id_entry.get()
        local_url = self.local_service_entry.get()
        token = self.token_entry.get()

        if not all([server_url, client_id, local_url, token]):
            messagebox.showerror("Error", "All fields are required!")
            return

        self.is_running = True
        self.start_btn.config(text="STOP TUNNEL", bg="#dc3545")
        self.status_label.config(text="● Status: Connecting...", fg="#ffc107")
        self.log(f"Starting tunnel to {server_url}...")
        
        self.client_thread = threading.Thread(target=self.run_async_loop, args=(server_url, client_id, local_url, token), daemon=True)
        self.client_thread.start()

    def stop_tunnel(self):
        self.is_running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.start_btn.config(text="START TUNNEL", bg="#28a745")
        self.status_label.config(text="● Status: Disconnected", fg="#dc3545")
        self.log("Tunnel stopped.")

    def run_async_loop(self, server_url, client_id, local_url, token):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.async_client_task(server_url, client_id, local_url, token))

    async def async_client_task(self, server_url, client_id, local_url, token):
        async with httpx.AsyncClient() as http_client:
            while self.is_running:
                try:
                    ws_uri = f"{server_url.rstrip('/')}/tunnel/{client_id}?token={token}"
                    async with websockets.connect(ws_uri) as websocket:
                        self.root.after(0, lambda: self.status_label.config(text="● Status: Connected", fg="#28a745"))
                        self.root.after(0, lambda: self.log(f"Connected! ID: {client_id}"))
                        
                        while self.is_running:
                            message_str = await websocket.recv()
                            message = json.loads(message_str)
                            
                            if message.get("type") == "request":
                                asyncio.create_task(self.handle_request(websocket, http_client, local_url, message))
                            elif message.get("type") == "ping":
                                await websocket.send(json.dumps({"type": "pong"}))
                                
                except Exception as e:
                    self.root.after(0, lambda: self.status_label.config(text="● Status: Retrying...", fg="#ffc107"))
                    self.root.after(0, lambda: self.log(f"Connection lost: {e}"))
                    if not self.is_running: break
                    await asyncio.sleep(3) # Faster retry (from 5s to 3s)

    async def handle_request(self, websocket, http_client, local_url, request_data):
        method = request_data.get("method")
        path = request_data.get("path")
        headers = request_data.get("headers", {})
        body_data = request_data.get("body")
        
        # Decode body if it's base64 encoded
        if request_data.get("is_base64") and body_data:
            body_data = base64.b64decode(body_data)
        
        url = f"{local_url.rstrip('/')}/{path.lstrip('/')}"
        if request_data.get("query"):
            url += f"?{request_data['query']}"
            
        self.root.after(0, lambda: self.log(f"Forwarding {method} {path}"))
        
        try:
            response = await http_client.request(
                method=method,
                url=url,
                headers=headers,
                content=body_data,
                timeout=60.0 # Increased timeout for large files
            )
            
            # Encode response body to base64 for safe transport
            resp_body_b64 = base64.b64encode(response.content).decode('utf-8')
            
            response_packet = {
                "type": "response",
                "request_id": request_data.get("request_id"),
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": resp_body_b64
            }
            await websocket.send(json.dumps(response_packet))
        except Exception as e:
            self.root.after(0, lambda: self.log(f"Local Error: {e}"))

if __name__ == "__main__":
    root = tk.Tk()
    app = RenderBridgeGUI(root)
    root.mainloop()
