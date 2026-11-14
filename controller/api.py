"""Token-authenticated HTTP API plus a shared, dependency-free web UI."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import BusyError, Controller, NotRunningError

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class StopRequest(BaseModel):
    confirm: bool = False


def create_app(controller: Controller | None = None, token: str | None = None) -> FastAPI:
    key = token if token is not None else os.environ.get("PBS_CONTROLLER_TOKEN", "")
    if len(key) < 32:
        raise RuntimeError("Set PBS_CONTROLLER_TOKEN to a random 32+ character secret")
    engine = controller if controller is not None else Controller(
        log_path=os.environ.get("PBS_CONTROLLER_LOG", "/var/log/run-pbs-job.log"),
        db_path=os.environ.get("PBS_CONTROLLER_DB", "/var/lib/pbs-controller/history.sqlite3"),
    )
    app = FastAPI(
        title="PBS Controller",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        prefix = "Bearer "
        provided = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
        if not secrets.compare_digest(provided, key):
            raise HTTPException(status_code=401, detail="Invalid access token", headers={"WWW-Authenticate": "Bearer"})

    guard = [Depends(authorized)]

    @app.get("/healthz")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/status", dependencies=guard)
    def status():
        try:
            return engine.status()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/start", dependencies=guard)
    def start():
        try:
            return engine.start()
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/stop", dependencies=guard)
    def stop(body: StopRequest):
        if not body.confirm:
            raise HTTPException(status_code=400, detail="Explicit stop confirmation required")
        try:
            return engine.stop()
        except NotRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/history", dependencies=guard)
    def history(limit: Annotated[int, Query(ge=1, le=100)] = 30):
        try:
            return {"runs": engine.history(limit)}
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/log", dependencies=guard)
    def log(offset: Annotated[int | None, Query(ge=0)] = None, file_id: str | None = None):
        try:
            return engine.log_chunk(offset, file_id)
        except PermissionError as exc:
            raise HTTPException(status_code=503, detail="Controller cannot read the log file") from exc

    @app.get("/api/log/download", dependencies=guard)
    def download():
        path = engine.log_path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No backup log found")
        start_offset = engine.latest_log_start()
        end_offset = path.stat().st_size

        def chunks():
            with path.open("rb") as stream:
                stream.seek(start_offset)
                remaining = end_offset - start_offset
                while remaining > 0:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block

        return StreamingResponse(
            chunks(), media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="pbs-backup-run.log"'},
        )

    app.mount("/assets", StaticFiles(directory=str(WEB / "assets")), name="assets")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    return app


app = create_app()
