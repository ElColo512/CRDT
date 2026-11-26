import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Set

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .crdt import RGA

# Configuración recibida desde compose.yaml y el archivo .env de cada máquina.
NODE_ID = os.getenv("NODE_ID", "A")
NODE_NAME = os.getenv("NODE_NAME", f"Nodo {NODE_ID}")
PEERS = [peer.strip().rstrip("/") for peer in os.getenv("PEERS", "").split(",") if peer.strip()]
PEER_TOKEN = os.getenv("PEER_TOKEN", "")
DATA_FILE = Path(os.getenv("DATA_FILE", f"/data/{NODE_ID}.json"))
SEED = os.getenv("SEED_TEXT", "hola mundo!")
SYNC_INTERVAL = float(os.getenv("SYNC_INTERVAL", "1.0"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "2.5"))

# Estado en memoria del nodo. Se restaura del volumen al arrancar.
doc = RGA(NODE_ID, SEED)
sync_enabled = True
clients: Set[WebSocket] = set()
lock = asyncio.Lock()
sync_task: asyncio.Task | None = None


class TextChange(BaseModel):
    old_text: str
    new_text: str


class MergePayload(BaseModel):
    atoms: list[dict]


def load_state() -> None:
    """Carga el último estado persistido de este nodo, si existe."""
    if not DATA_FILE.exists():
        return
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    doc.merge(raw.get("atoms", []))


def save_state() -> None:
    """Guarda de manera atómica para evitar un JSON incompleto ante un corte."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"atoms": doc.state()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(DATA_FILE)


async def snapshot() -> dict:
    """Resumen enviado a la interfaz web."""
    return {
        "node_id": NODE_ID,
        "node_name": NODE_NAME,
        "text": doc.text(),
        "atoms": doc.state(),
        "visible_count": len(doc.visible_atoms()),
        "tombstones": sum(1 for atom in doc.atoms.values() if atom.deleted),
        "sync_enabled": sync_enabled,
        "peers": PEERS,
    }


async def broadcast() -> None:
    """Actualiza todos los navegadores conectados a este nodo por WebSocket."""
    message = json.dumps(await snapshot(), ensure_ascii=False)
    disconnected = []
    for websocket in list(clients):
        try:
            await websocket.send_text(message)
        except Exception:
            disconnected.append(websocket)
    for websocket in disconnected:
        clients.discard(websocket)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Restaura el estado y mantiene la sincronización periódica en segundo plano."""
    global sync_task
    load_state()
    sync_task = asyncio.create_task(sync_loop())
    yield
    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title=f"Editor CRDT - {NODE_NAME}", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
async def health():
    """Comprobación sencilla para Docker, scripts y demostraciones."""
    return {"status": "ok", "node_id": NODE_ID, "sync_enabled": sync_enabled}


@app.get("/api/state")
async def get_state():
    return await snapshot()


@app.post("/api/edit")
async def edit(change: TextChange):
    """Aplica una edición local incluso durante una partición simulada."""
    async with lock:
        doc.apply_text_change(change.old_text, change.new_text)
        save_state()
    await broadcast()
    return await snapshot()


@app.post("/api/merge")
async def merge(payload: MergePayload, x_peer_token: str = Header(default="")):
    """Recibe el estado de otro nodo y devuelve el estado local ya combinado."""
    if not PEER_TOKEN or x_peer_token != PEER_TOKEN:
        raise HTTPException(401, "Token de nodo inválido")

    if not sync_enabled:
        raise HTTPException(503, "Nodo aislado: sincronización desactivada")

    async with lock:
        changed = doc.merge(payload.atoms)
        if changed:
            save_state()

    if changed:
        await broadcast()
    return {"changed": changed, "atoms": doc.state()}


@app.post("/api/sync/toggle")
async def toggle_sync():
    """Activa o desactiva la partición simulada desde la interfaz."""
    global sync_enabled
    sync_enabled = not sync_enabled
    await broadcast()
    return await snapshot()


@app.post("/api/reset")
async def reset():
    """Reinicia únicamente el estado de este nodo; no borra el nodo remoto."""
    global doc
    async with lock:
        doc = RGA(NODE_ID, SEED)
        save_state()
    await broadcast()
    return await snapshot()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Canal de actualizaciones entre este nodo y sus navegadores."""
    await websocket.accept()
    clients.add(websocket)
    await websocket.send_text(json.dumps(await snapshot(), ensure_ascii=False))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)


async def sync_once() -> None:
    """Intercambia el estado completo con cada peer configurado.

    Este envío es state-based: enviar el mismo estado más de una vez no cambia el
    resultado porque merge es idempotente.
    """
    if not sync_enabled:
        return

    headers = {"X-Peer-Token": PEER_TOKEN}
    timeout = httpx.Timeout(REQUEST_TIMEOUT)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for peer in PEERS:
            try:
                async with lock:
                    local_atoms = doc.state()

                response = await client.post(
                    f"{peer}/api/merge",
                    json={"atoms": local_atoms},
                    headers=headers,
                )
                response.raise_for_status()
                remote_atoms = response.json().get("atoms", [])

                async with lock:
                    changed = doc.merge(remote_atoms)
                    if changed:
                        save_state()
                if changed:
                    await broadcast()
            except (httpx.HTTPError, ValueError):
                # Un peer apagado o inaccesible no detiene este nodo. En el próximo ciclo se vuelve a intentar automáticamente.
                continue


async def sync_loop() -> None:
    """Reintenta la sincronización periódicamente durante toda la ejecución."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        await sync_once()
