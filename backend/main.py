from __future__ import annotations

import asyncio
import json
import logging
import shutil
import string
from ctypes import windll
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import (
    MESH_SOURCE_FILE,
    PRINTER_HOST,
    PRINTER_PORT,
    PROJECT_DIR,
    SERVER_HOST,
    SERVER_PORT,
    TEMP_LOG_CSV,
    TEMP_LOG_JSON,
    UPDATE_INTERVAL,
)
from mesh_parser import parse_bed_mesh_from_file
from printer import AD5XPrinter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ad5x-monitor")

printer = AD5XPrinter(PRINTER_HOST, PRINTER_PORT, TEMP_LOG_CSV, TEMP_LOG_JSON)
active_websockets: set[WebSocket] = set()
updater_task: asyncio.Task[None] | None = None


def _read_tail_csv(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not rows:
        return []
    header = rows[0].split(",")
    data_rows = rows[1:]
    sliced = data_rows[-limit:]
    out: list[dict[str, Any]] = []
    for row in sliced:
        cols = row.split(",")
        rec = {header[i]: cols[i] if i < len(cols) else "" for i in range(len(header))}
        out.append(rec)
    return out


async def _broadcast_snapshot() -> None:
    if not active_websockets:
        return

    payload = printer.get_cache()
    dead: list[WebSocket] = []
    for ws in list(active_websockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)

    for ws in dead:
        active_websockets.discard(ws)


async def background_updater() -> None:
    while True:
        try:
            await printer.update_all()
            await _broadcast_snapshot()
        except Exception as exc:
            logger.warning("Background update error: %s", exc)
        await asyncio.sleep(UPDATE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global updater_task
    await printer.start()
    await printer.connect()

    mesh = parse_bed_mesh_from_file(MESH_SOURCE_FILE)
    if mesh is not None:
        printer.cache["bed_mesh"] = mesh

    updater_task = asyncio.create_task(background_updater())

    yield

    if updater_task:
        updater_task.cancel()
        try:
            await updater_task
        except asyncio.CancelledError:
            pass
    await printer.stop()


app = FastAPI(
    title="AD5X Monitor",
    description="Safe external web monitor for Flashforge AD5X over port 8899",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_dir = PROJECT_DIR / "frontend"
cfg_dir = frontend_dir / "cfg"
cfg_dir.mkdir(parents=True, exist_ok=True)

mesh_tool_config_path = cfg_dir / "mesh-tool.config.json"
allowed_mesh_ext = {".cfg", ".txt", ".log"}
default_mesh_tool_config: dict[str, Any] = {
    "pitch": "0.5",
    "tolerance": 0.05,
    "vizMode": "heatmap",
    "screwMode": "plane",
    "rotationMode": "cw_lower",
}


def _safe_cfg_filename(name: str) -> bool:
    p = Path(name)
    return p.name == name and p.suffix.lower() in allowed_mesh_ext


def _list_cfg_files() -> list[str]:
    out: list[Path] = []
    for p in cfg_dir.iterdir():
        if p.is_file() and p.suffix.lower() in allowed_mesh_ext:
            out.append(p)
    out.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [p.name for p in out]


def _load_mesh_tool_config() -> dict[str, Any]:
    if not mesh_tool_config_path.exists():
        return dict(default_mesh_tool_config)
    try:
        loaded = json.loads(mesh_tool_config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return {**default_mesh_tool_config, **loaded}
    except Exception:
        pass
    return dict(default_mesh_tool_config)


def _drive_type(root_path: str) -> int:
    return int(windll.kernel32.GetDriveTypeW(root_path))


def _find_usb_printer_cfg() -> Path | None:
    # DRIVE_REMOVABLE (2): scan letters in alphabetical order and take the first root printer.cfg.
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        if not root.exists():
            continue
        if _drive_type(str(root)) != 2:
            continue
        candidate = root / "printer.cfg"
        if candidate.is_file():
            return candidate
    return None


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _copy_to_cfg(src: Path) -> None:
    dst = cfg_dir / src.name
    shutil.copy2(src, dst)

app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(frontend_dir / "index.html")


@app.get("/mesh-tool")
async def mesh_tool() -> FileResponse:
    return FileResponse(frontend_dir / "mesh-tool.html")


@app.get("/api/mesh-tool/bootstrap")
async def mesh_tool_bootstrap() -> dict[str, Any]:
    cfg_files = _list_cfg_files()
    config = _load_mesh_tool_config()

    selected: dict[str, Any] | None = None
    usb_file = await asyncio.to_thread(_find_usb_printer_cfg)
    if usb_file is not None:
        text = await asyncio.to_thread(_read_text_file, usb_file)
        selected = {
            "source": "usb",
            "file_name": usb_file.name,
            "file_path": str(usb_file),
            "text": text,
        }
        # Background copy to cfg directory.
        asyncio.create_task(asyncio.to_thread(_copy_to_cfg, usb_file))
    elif cfg_files:
        selected_name = cfg_files[0]
        selected_path = cfg_dir / selected_name
        text = await asyncio.to_thread(_read_text_file, selected_path)
        selected = {
            "source": "cfg",
            "file_name": selected_name,
            "file_path": str(selected_path),
            "text": text,
        }

    return {
        "config": config,
        "cfg_files": cfg_files,
        "selected": selected,
    }


@app.get("/api/mesh-tool/cfg-files")
async def mesh_tool_cfg_files() -> dict[str, Any]:
    return {"files": _list_cfg_files()}


@app.get("/api/mesh-tool/cfg-file")
async def mesh_tool_cfg_file(name: str = Query(..., min_length=1)) -> dict[str, Any]:
    if not _safe_cfg_filename(name):
        raise HTTPException(status_code=400, detail="invalid cfg filename")

    path = cfg_dir / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="cfg file not found")

    text = await asyncio.to_thread(_read_text_file, path)
    return {"name": name, "text": text}


@app.post("/api/mesh-tool/config")
async def mesh_tool_save_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    merged = {**default_mesh_tool_config, **payload}
    mesh_tool_config_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(mesh_tool_config_path), "config": merged}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "printer_connected": printer.connected}


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    return printer.get_cache()


@app.get("/api/temperature")
async def get_temperature() -> dict[str, Any]:
    return printer.cache["temperatures"]


@app.get("/api/position")
async def get_position() -> dict[str, Any]:
    return printer.cache["position"]


@app.get("/api/endstops")
async def get_endstops() -> dict[str, Any]:
    return printer.cache["endstops"]


@app.get("/api/print/status")
async def get_print_status() -> dict[str, Any]:
    return printer.cache["print_status"]


@app.get("/api/bed_mesh")
async def get_bed_mesh() -> dict[str, Any]:
    mesh = printer.cache.get("bed_mesh")
    if mesh is None:
        raise HTTPException(status_code=404, detail="bed mesh not loaded")
    return mesh


@app.post("/api/refresh_mesh")
async def refresh_bed_mesh() -> dict[str, Any]:
    mesh = parse_bed_mesh_from_file(MESH_SOURCE_FILE)
    if mesh is None:
        raise HTTPException(status_code=404, detail=f"mesh not found in {MESH_SOURCE_FILE}")
    printer.cache["bed_mesh"] = mesh
    return {"ok": True, "source": str(MESH_SOURCE_FILE), "stats": mesh.get("stats", {})}


@app.get("/api/temperature/history")
async def temperature_history(limit: int = 200) -> dict[str, Any]:
    lim = max(10, min(limit, 5000))
    return {"rows": _read_tail_csv(TEMP_LOG_CSV, lim)}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    active_websockets.add(websocket)

    try:
        await websocket.send_json(printer.get_cache())
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
            elif msg == "refresh":
                await printer.update_all()
                await websocket.send_json(printer.get_cache())
            elif msg == "refresh_mesh":
                mesh = parse_bed_mesh_from_file(MESH_SOURCE_FILE)
                if mesh is not None:
                    printer.cache["bed_mesh"] = mesh
                    await websocket.send_json({"event": "mesh_refreshed", "stats": mesh.get("stats", {})})
            else:
                await websocket.send_json({"event": "ignored", "message": "unsupported command"})
    except WebSocketDisconnect:
        pass
    finally:
        active_websockets.discard(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=True, log_level="info")
