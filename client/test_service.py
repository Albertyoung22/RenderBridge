from fastapi import FastAPI
from datetime import datetime

app = FastAPI(title="Local Test Service")

@app.get("/")
async def index():
    return {
        "message": "Hello from your local machine!",
        "timestamp": datetime.now().isoformat(),
        "status": "Target service reached via RenderBridge tunnel"
    }

@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}, your local service is working perfectly!"}

if __name__ == "__main__":
    import uvicorn
    print("--- Local Test Service Running on http://localhost:8080 ---")
    uvicorn.run(app, host="0.0.0.0", port=8080)
