"""
Odoo Migration Studio - Web Application (FastAPI + Server-Sent Events)
Antarmuka Web interaktif untuk memigrasikan akuntansi Odoo 19 ke 20 (100% Pure API JSON-2).
"""

import asyncio
import base64
from datetime import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Add project root and src to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from odoo_client import OdooClient, OdooAPIError
from id_mapper import IdMapper
import migrate_pure_api


app = FastAPI(title="Odoo Migration Studio", version="1.0.0")

# Setup templates and static
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


# ==============================================================================
# STATE & EVENT BROADCASTER (SSE)
# ==============================================================================
class EventBroadcaster:
    def __init__(self):
        self.subscribers: List[asyncio.Queue] = []
        self.log_history: List[Dict[str, Any]] = []
        self.is_running: bool = False
        self.current_stage: str = "Idle"
        self.current_progress: int = 0
        self.last_status: str = "ready" # ready, running, completed, error
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def register_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def emit(self, event_type: str, data: Dict[str, Any]):
        timestamp = datetime.now().strftime("%H:%M:%S")
        payload = {"type": event_type, "time": timestamp, **data}

        if event_type == "log":
            self.log_history.append(payload)
            # Batasi history dalam memori maksimal 1000 baris
            if len(self.log_history) > 1000:
                self.log_history = self.log_history[-1000:]
        elif event_type == "progress":
            self.current_progress = data.get("percent", self.current_progress)
            self.current_stage = data.get("stage", self.current_stage)
        elif event_type == "status":
            self.is_running = data.get("is_running", self.is_running)
            self.last_status = data.get("state", self.last_status)

        # Broadcast ke semua subscriber yang sedang mendengarkan
        if self.loop and self.loop.is_running():
            for q in list(self.subscribers):
                self.loop.call_soon_threadsafe(q.put_nowait, payload)

    def log(self, text: str, level: str = "info"):
        self.emit("log", {"text": text, "level": level})

    def progress(self, percent: int, stage: str):
        self.emit("progress", {"percent": percent, "stage": stage})

    def set_status(self, is_running: bool, state: str, stage: Optional[str] = None):
        self.is_running = is_running
        self.last_status = state
        if stage:
            self.current_stage = stage
        self.emit("status", {"is_running": is_running, "state": state, "stage": self.current_stage})


broadcaster = EventBroadcaster()


# ==============================================================================
# SCHEMAS
# ==============================================================================
class OdooConnectionConfig(BaseModel):
    url: str
    db: str
    api_key: str
    endpoint: str = "/json/2"


class AppConfig(BaseModel):
    source_odoo_19: OdooConnectionConfig
    target_odoo_20: OdooConnectionConfig
    migration_map_db: Optional[str] = None


class TestConnectionRequest(BaseModel):
    url: str
    db: str
    api_key: str
    endpoint: str = "/json/2"


class MigrateRequest(BaseModel):
    phase: str = "all" # all, master, transactions, reconcile, verify
    limit: Optional[int] = None


class LoginRequest(BaseModel):
    username: str
    password: str


# ==============================================================================
# HELPER FUNCTIONS & AUTHENTICATION
# ==============================================================================
# Secret acak per-proses (atau dari env bila ingin sesi stabil antar-restart).
# Tidak lagi berupa konstanta di source yang bisa dipakai memalsukan cookie.
AUTH_SECRET = os.environ.get("WEB_AUTH_SECRET") or secrets.token_hex(32)

# Sentinel untuk menyamarkan API key saat dibaca UI; nilai ini tidak pernah
# dianggap sebagai key asli ketika menyimpan konfigurasi.
MASKED_KEY = "••••••••••••"

# Pembatasan percobaan login sederhana (anti brute-force) per IP.
_login_failures: Dict[str, List[float]] = {}
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_SEC = 300
_LOGIN_LOCKOUT_SEC = 60


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_locked(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW_SEC]
    _login_failures[ip] = hits
    if len(hits) >= _LOGIN_MAX_FAILS:
        return now - hits[-1] < _LOGIN_LOCKOUT_SEC
    return False


def _record_login_failure(ip: str) -> None:
    _login_failures.setdefault(ip, []).append(time.time())


def mask_config(cfg: dict) -> dict:
    """Salinan config dengan API key disamarkan untuk ditampilkan ke browser."""
    masked = json.loads(json.dumps(cfg))
    for section in ("source_odoo_19", "target_odoo_20"):
        if masked.get(section, {}).get("api_key"):
            masked[section]["api_key"] = MASKED_KEY
    return masked


def resolve_api_key(url: str, db: str, provided: str) -> str:
    """Jika key yang dikirim kosong/samaran, pakai key tersimpan yang cocok url+db."""
    if provided and provided != MASKED_KEY:
        return provided
    cfg = read_current_config()
    for section in ("source_odoo_19", "target_odoo_20"):
        s = cfg.get(section, {})
        if s.get("url", "").rstrip("/") == (url or "").rstrip("/") and s.get("db") == db:
            return s.get("api_key", "")
    return provided or ""


def read_current_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "source_odoo_19": {"url": "http://localhost:1969", "db": "odoo19_db", "api_key": "", "endpoint": "/json/2"},
        "target_odoo_20": {"url": "http://localhost:2069", "db": "odoo20_db", "api_key": "", "endpoint": "/json/2"},
        "web_auth": {"enabled": True, "username": "admin", "password": "odoomigrate2026"}
    }


def save_current_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_auth_credentials() -> Tuple[str, str, bool]:
    cfg = read_current_config()
    web_auth = cfg.get("web_auth", {})
    username = os.environ.get("WEB_USERNAME") or web_auth.get("username", "admin")
    password = os.environ.get("WEB_PASSWORD") or web_auth.get("password", "odoomigrate2026")
    enabled = web_auth.get("enabled", True)
    return str(username), str(password), bool(enabled)


def get_auth_token(username: str, password: str) -> str:
    msg = f"{username}:{password}:{AUTH_SECRET}".encode("utf-8")
    return hashlib.sha256(msg).hexdigest()


def is_authenticated(request: Request) -> bool:
    username, password, enabled = get_auth_credentials()
    if not enabled:
        return True

    # 1. Cookie check
    cookie_token = request.cookies.get("odoo_migration_token")
    expected_token = get_auth_token(username, password)
    if cookie_token and hmac.compare_digest(cookie_token, expected_token):
        return True

    # 2. Basic Auth header fallback (useful for API/curl access)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
            u, p = decoded.split(":", 1)
            if hmac.compare_digest(u, username) and hmac.compare_digest(p, password):
                return True
        except Exception:
            pass

    return False


# ==============================================================================
# MIDDLEWARE
# ==============================================================================
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = {"/login", "/api/login"}

    if path in public_paths or path.startswith("/static/"):
        if path == "/login" and is_authenticated(request):
            return RedirectResponse(url="/", status_code=303)
        return await call_next(request)

    if not is_authenticated(request):
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "Silakan login terlebih dahulu."}
            )
        return RedirectResponse(url="/login", status_code=303)

    return await call_next(request)


# ==============================================================================
# ROUTES
# ==============================================================================
@app.on_event("startup")
async def startup_event():
    broadcaster.register_loop(asyncio.get_running_loop())


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/api/login")
async def api_login(payload: LoginRequest, request: Request):
    username, password, enabled = get_auth_credentials()
    if not enabled:
        return JSONResponse({"success": True, "message": "Autentikasi dinonaktifkan."})

    ip = _client_ip(request)
    if _login_locked(ip):
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": "Terlalu banyak percobaan gagal. Coba lagi dalam 60 detik."}
        )

    if hmac.compare_digest(payload.username, username) and hmac.compare_digest(payload.password, password):
        _login_failures.pop(ip, None)
        resp = JSONResponse({"success": True, "message": "Login berhasil!"})
        token = get_auth_token(username, password)
        resp.set_cookie(
            key="odoo_migration_token",
            value=token,
            max_age=86400 * 7,
            httponly=True,
            samesite="lax",
            secure=(request.url.scheme == "https"),
            path="/"
        )
        return resp
    else:
        _record_login_failure(ip)
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "Username atau password salah."}
        )


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"success": True, "message": "Logout berhasil!"})
    resp.delete_cookie(key="odoo_migration_token", path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    cfg = read_current_config()
    db_path = IdMapper.get_db_path_from_config(cfg)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "config": mask_config(cfg),
            "db_path": db_path,
            "db_filename": os.path.basename(db_path)
        }
    )


@app.get("/api/config")
async def get_config():
    cfg = read_current_config()
    db_path = IdMapper.get_db_path_from_config(cfg)
    return {
        "config": mask_config(cfg),
        "db_path": db_path,
        "db_filename": os.path.basename(db_path)
    }


@app.post("/api/config")
async def update_config(payload: AppConfig):
    current_cfg = read_current_config()
    cfg = payload.dict(exclude_none=True)
    # Pertahankan API key lama bila UI mengirim nilai samaran/kosong (tidak diubah user).
    for section in ("source_odoo_19", "target_odoo_20"):
        incoming = cfg.get(section, {})
        key = incoming.get("api_key", "")
        if not key or key == MASKED_KEY:
            incoming["api_key"] = current_cfg.get(section, {}).get("api_key", "")
    if "web_auth" in current_cfg:
        cfg["web_auth"] = current_cfg["web_auth"]
    save_current_config(cfg)
    db_path = IdMapper.get_db_path_from_config(cfg)
    broadcaster.log(f"💾 Konfigurasi koneksi berhasil disimpan. SQLite Mapping: {os.path.basename(db_path)}")
    return {
        "success": True,
        "message": "Konfigurasi berhasil disimpan!",
        "db_path": db_path,
        "db_filename": os.path.basename(db_path)
    }


@app.post("/api/test-connection")
async def test_connection(payload: TestConnectionRequest):
    try:
        api_key = resolve_api_key(payload.url, payload.db, payload.api_key)
        client = OdooClient(payload.url, payload.db, api_key, endpoint=payload.endpoint, timeout=10)
        # Query res.company
        comps = client.search_read("res.company", [], ["id", "name", "currency_id"], limit=1)
        if not comps:
            raise ValueError("Tidak ada data company yang dapat diakses dengan API Key ini.")
        
        comp = comps[0]
        # Query active user
        users = client.search_read("res.users", [("id", "=", 2)], ["name", "login"], limit=1)
        user_info = f"{users[0]['name']} ({users[0]['login']})" if users else "OK"

        return {
            "success": True,
            "company_id": comp["id"],
            "company_name": comp["name"],
            "currency": comp["currency_id"][1] if comp.get("currency_id") else "-",
            "user": user_info,
            "message": f"Koneksi Berhasil! Terhubung ke '{comp['name']}' sebagai {user_info}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"Koneksi Gagal: {e}"
        }


# ==============================================================================
# MIGRATION WORKER THREAD
# ==============================================================================
def run_migration_worker(phase: str, limit: Optional[int] = None):
    cfg = read_current_config()
    db_path = IdMapper.get_db_path_from_config(cfg)
    broadcaster.set_status(True, "running", "Memulai...")
    broadcaster.log(f"🚀 Memulai Eksekusi Migrasi [{phase.upper()}]...")
    broadcaster.log(f"📁 Target Database Mapping: {os.path.basename(db_path)}")

    try:
        c19 = OdooClient(cfg["source_odoo_19"]["url"], cfg["source_odoo_19"]["db"], cfg["source_odoo_19"]["api_key"], log_callback=broadcaster.log)
        c20 = OdooClient(cfg["target_odoo_20"]["url"], cfg["target_odoo_20"]["db"], cfg["target_odoo_20"]["api_key"], log_callback=broadcaster.log)
        mapper = IdMapper.from_config(cfg)

        # 1. Master Data
        if phase in ("all", "master"):
            broadcaster.progress(10, "Fase 2: Master Data")
            broadcaster.log("🏢 [Fase 2] Sinkronisasi Master Data (Company, Mata Uang, Pajak, CoA, Jurnal, Partner)...")
            migrate_pure_api.migrate_master_data(c19, c20, mapper)
            broadcaster.log("✅ [Fase 2] Selesai sinkronisasi seluruh Master Data.")
            broadcaster.progress(25, "Master Data Selesai")

        # 2. Transaksi — delegasikan ke engine yang sama dengan CLI (satu sumber logika).
        if phase in ("all", "transactions"):
            broadcaster.progress(30, "Fase 3: Mengambil Data Transaksi")
            broadcaster.log("📥 [Fase 3] Memulai Migrasi Transaksi Dual-Route (100% Pure API)...")
            result = migrate_pure_api.migrate_transactions_pure_api(
                c19, c20, mapper, limit=limit,
                log=broadcaster.log, progress=broadcaster.progress
            )
            if result.get("failed"):
                broadcaster.log(f"⚠️ {result['failed']} move gagal dan tercatat di SQLite (tabel migration_errors).", level="warning")
            broadcaster.progress(75, "Transaksi Selesai")

        # 3. Rekonsiliasi
        if phase in ("all", "reconcile"):
            broadcaster.progress(80, "Fase 5: Rekonsiliasi Native")
            broadcaster.log("🔗 [Fase 5] Memproses Rekonsiliasi Pembayaran via Odoo API...")
            migrate_pure_api.migrate_reconciliations_pure_api(c19, c20, mapper, log=broadcaster.log)
            broadcaster.progress(90, "Rekonsiliasi Selesai")

        # 4. Verifikasi
        if phase in ("all", "verify"):
            broadcaster.progress(95, "Audit & Verifikasi")
            broadcaster.log("⚖️ [Verifikasi] Menghitung Trial Balance & Keseimbangan Buku Besar...")
            migrate_pure_api.verify_pure_api(c19, c20, log=broadcaster.log)

        mapper.close()
        broadcaster.progress(100, "Selesai")
        broadcaster.set_status(False, "completed", "Selesai Sempurna")
        broadcaster.log("🎉 SELURUH TAHAP MIGRASI BERHASIL DILAKSANAKAN DENGAN SUKSES!", level="success")

    except Exception as e:
        broadcaster.progress(100, "Error")
        broadcaster.set_status(False, "error", f"Error: {e}")
        broadcaster.log(f"❌ Terjadi kesalahan saat migrasi: {e}", level="error")


@app.post("/api/migrate")
async def trigger_migration(payload: MigrateRequest):
    if broadcaster.is_running:
        raise HTTPException(status_code=400, detail="Migrasi sedang berjalan! Mohon tunggu hingga selesai.")
    
    # Jalankan worker di thread terpisah agar tidak memblokir event loop
    thread = threading.Thread(target=run_migration_worker, args=(payload.phase, payload.limit), daemon=True)
    thread.start()
    return {"success": True, "message": f"Migrasi fase '{payload.phase}' telah dimulai!"}


@app.get("/api/status")
async def get_status():
    return {
        "is_running": broadcaster.is_running,
        "state": broadcaster.last_status,
        "stage": broadcaster.current_stage,
        "progress": broadcaster.current_progress,
        "log_count": len(broadcaster.log_history)
    }


@app.get("/api/stream")
async def stream_logs():
    async def event_generator():
        q = broadcaster.subscribe()
        try:
            # Kirim state saat ini
            initial = {
                "type": "init",
                "is_running": broadcaster.is_running,
                "state": broadcaster.last_status,
                "stage": broadcaster.current_stage,
                "progress": broadcaster.current_progress,
                "logs": broadcaster.log_history[-150:] # 150 log terakhir
            }
            yield f"data: {json.dumps(initial)}\n\n"

            while True:
                data = await q.get()
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            broadcaster.unsubscribe(q)
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/api/verify")
async def get_verification_data():
    cfg = read_current_config()
    try:
        c19 = OdooClient(cfg["source_odoo_19"]["url"], cfg["source_odoo_19"]["db"], cfg["source_odoo_19"]["api_key"], timeout=15)
        c20 = OdooClient(cfg["target_odoo_20"]["url"], cfg["target_odoo_20"]["db"], cfg["target_odoo_20"]["api_key"], timeout=15)

        # 1. Trial balance: hanya move 'posted', diagregasi per kode akun agar
        #    selisih antar-akun tidak saling meniadakan (bukan sekadar total tenant).
        domain = [("parent_state", "=", "posted")]
        ml19 = c19.search_read("account.move.line", domain, ["account_id", "debit", "credit"])
        ml20 = c20.search_read("account.move.line", domain, ["account_id", "debit", "credit"])

        def by_code(client, lines):
            # display_name akun berbeda antar versi (Odoo 20 tanpa kode),
            # jadi ambil kode asli dari account.account, bukan parsing nama.
            acc_ids = list({l["account_id"][0] for l in lines if l.get("account_id")})
            code_map = {a["id"]: a["code"] for a in client.search_read(
                "account.account", [("id", "in", acc_ids)], ["code"])} if acc_ids else {}
            agg = {}
            for l in lines:
                if not l.get("account_id"):
                    continue
                code = code_map.get(l["account_id"][0])
                if not code:
                    continue
                a = agg.setdefault(code, [0.0, 0.0])
                a[0] += l["debit"]
                a[1] += l["credit"]
            return agg

        tb19, tb20 = by_code(c19, ml19), by_code(c20, ml20)
        deb19 = sum(v[0] for v in tb19.values()); deb20 = sum(v[0] for v in tb20.values())
        cred19 = sum(v[1] for v in tb19.values()); cred20 = sum(v[1] for v in tb20.values())
        account_mismatches = [
            code for code in sorted(set(tb19) | set(tb20))
            if abs(tb19.get(code, [0, 0])[0] - tb20.get(code, [0, 0])[0]) > 0.01
            or abs(tb19.get(code, [0, 0])[1] - tb20.get(code, [0, 0])[1]) > 0.01
        ]

        # 2. Journal Balances
        j19_data = c19.search_read("account.journal", [("type", "in", ["bank", "cash"])], ["name", "kanban_dashboard"])
        j20_data = c20.search_read("account.journal", [("type", "in", ["bank", "cash"])], ["name", "kanban_dashboard"])

        j19_map = {j["name"]: json.loads(j["kanban_dashboard"]).get("account_balance", "Rp 0.00") for j in j19_data}
        j20_map = {j["name"]: json.loads(j["kanban_dashboard"]).get("account_balance", "Rp 0.00") for j in j20_data}

        journals_summary = []
        def _balance_number(text: str) -> float:
            # Ambil angka dari string terformat (mis. 'Rp 1,234.56' / '$ 1,234.56')
            # agar beda simbol mata uang tidak dianggap selisih saldo.
            m = re.search(r"-?[\d.,]+", text or "")
            if not m:
                return 0.0
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return 0.0

        for name, b19 in j19_map.items():
            b20 = j20_map.get(name) or j20_map.get(name.title()) or j20_map.get(name.upper()) or "Rp 0.00"
            b19_clean = b19.replace("\xa0", " ") if b19 else "Rp 0.00"
            b20_clean = b20.replace("\xa0", " ") if b20 else "Rp 0.00"
            match = abs(_balance_number(b19_clean) - _balance_number(b20_clean)) < 0.01
            journals_summary.append({
                "name": name,
                "balance_19": b19_clean,
                "balance_20": b20_clean,
                "match": match
            })

        # 3. Counts
        m19_count = c19.call("account.move", "search_count", domain=[])
        m20_count = c20.call("account.move", "search_count", domain=[])
        st19_count = c19.call("account.bank.statement.line", "search_count", domain=[])
        st20_count = c20.call("account.bank.statement.line", "search_count", domain=[])

        return {
            "success": True,
            "trial_balance": {
                "debit_19": f"Rp {deb19:,.2f}",
                "debit_20": f"Rp {deb20:,.2f}",
                "debit_diff": f"Rp {deb20 - deb19:,.2f}",
                "credit_19": f"Rp {cred19:,.2f}",
                "credit_20": f"Rp {cred20:,.2f}",
                "credit_diff": f"Rp {cred20 - cred19:,.2f}",
                "balanced": abs(deb20 - deb19) < 0.01 and abs(cred20 - cred19) < 0.01 and not account_mismatches,
                "account_mismatches": account_mismatches,
            },
            "counts": {
                "moves_19": m19_count,
                "moves_20": m20_count,
                "statement_lines_19": st19_count,
                "statement_lines_20": st20_count,
            },
            "journals": journals_summary
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
