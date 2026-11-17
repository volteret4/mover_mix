"""API de mover_mix (para el móvil o el PC). Requiere MOVER_MIX_TOKEN."""
import hmac
import os
import subprocess
import threading
import time
import traceback
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from mover import Mover, MoverError

TOKEN = os.environ.get("MOVER_MIX_TOKEN", "")
app = FastAPI(title="mover_mix", docs_url=None, redoc_url=None, openapi_url=None)

# Una sola operación a la vez: todas reescriben los .m3u de Mix
LOCK = threading.Lock()
JOBS = {}


def auth(x_token: str = Header(default="")):
    if not TOKEN or not hmac.compare_digest(x_token, TOKEN):
        raise HTTPException(401, "token")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/analyze", dependencies=[Depends(auth)])
def analyze(id: Optional[str] = None, path: Optional[str] = None, length: Optional[float] = None,
            cue: Optional[int] = Query(default=None), artist: Optional[str] = None,
            title: Optional[str] = None, album: Optional[str] = None):
    try:
        return Mover().analyze(id, length=length, cue_choice=cue, path=path, artist=artist, title=title,
                               album=album)
    except MoverError as e:
        raise HTTPException(400, str(e))


class MoveRequest(BaseModel):
    id: Optional[str] = None      # id de Navidrome (móvil)
    path: Optional[str] = None    # o ruta (PC)
    folder: str
    comment: str = ""
    obsession: bool = False
    action: Optional[str] = None  # obsoleto: «move» solo se acepta si la canción ya está en Mix
    length: Optional[float] = None
    cue: Optional[int] = None


def _new_job(**extra):
    for k in [k for k, j in JOBS.items() if j.get('finished') and time.time() - j['finished'] > 86400]:
        JOBS.pop(k, None)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {'state': 'queued', 'messages': [], 'error': None, 'log': [], 'folder': None, **extra}
    return job_id


def _run(job_id, req: MoveRequest):
    job = JOBS[job_id]
    log = job['log'].append
    with LOCK:
        job['state'] = 'running'
        try:
            job['messages'] = Mover(log=log).execute(req.id, req.folder, req.comment, req.obsession,
                                                     req.action, req.length, req.cue, path=req.path)
            job['state'] = 'done'
        except MoverError as e:
            job['state'], job['error'] = 'error', str(e)
        except Exception as e:
            job['state'], job['error'] = 'error', f"{type(e).__name__}: {e}"
            log(traceback.format_exc())
        job['finished'] = time.time()


@app.post("/api/move", dependencies=[Depends(auth)])
def move(req: MoveRequest):
    if not (req.id or req.path):
        raise HTTPException(400, "Falta la canción (id o ruta)")
    job_id = _new_job(folder=req.folder)
    threading.Thread(target=_run, args=(job_id, req), daemon=True).start()
    return {'job': job_id}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
def job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "trabajo desconocido")
    return {k: j[k] for k in ('state', 'messages', 'error', 'folder')} | {'log': j['log'][-20:]}


# ── vvmm (opcional): post de blog del disco que suena con el script vvmm
#    (https://github.com/volteret4/vvmm), montado en VVMM_DIR. Sin vista previa ni Spotify.

VVMM_DIR = os.environ.get("VVMM_DIR", "/vvmm")
VVMM_LOCK = threading.Lock()


class VvmmRequest(BaseModel):
    artist: str
    album: str = ""
    title: str
    tags: str


def _run_vvmm(job_id, req: VvmmRequest):
    job = JOBS[job_id]
    with VVMM_LOCK:
        job['state'] = 'running'
        cmd = ["python3", "main.py", "--no-preview", "--no-spotify", "--artist", req.artist,
               "--album", req.album, "--title", req.title, "--tags", req.tags]
        try:
            r = subprocess.run(cmd, cwd=VVMM_DIR, capture_output=True, text=True, timeout=1200,
                               stdin=subprocess.DEVNULL)
            out = (r.stdout + "\n" + r.stderr).strip().splitlines()
            job['log'] = out[-60:]
            keep = [l.split(" - ", 2)[-1] for l in out
                    if any(k in l for k in ("Post creado", "Archivo:", "Cambios subidos", "Error", "advertencias"))]
            job['messages'] = keep[-6:] or out[-3:]
            if r.returncode:
                job['state'], job['error'] = 'error', (keep[-1] if keep else f"vvmm salió con {r.returncode}")
            else:
                job['state'] = 'done'
        except subprocess.TimeoutExpired:
            job['state'], job['error'] = 'error', "vvmm: tiempo agotado (20 min)"
        except Exception as e:
            job['state'], job['error'] = 'error', f"{type(e).__name__}: {e}"
        job['finished'] = time.time()


@app.post("/api/vvmm", dependencies=[Depends(auth)])
def vvmm(req: VvmmRequest):
    if not os.path.isfile(os.path.join(VVMM_DIR, "main.py")):
        raise HTTPException(501, f"vvmm no está instalado en {VVMM_DIR}")
    if not req.tags.split():
        raise HTTPException(400, "Faltan los tags")
    job_id = _new_job(folder=f"vvmm: {req.artist} - {req.album or req.title}")
    threading.Thread(target=_run_vvmm, args=(job_id, req), daemon=True).start()
    return {'job': job_id}
