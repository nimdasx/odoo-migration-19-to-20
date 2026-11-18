"""
Odoo External API JSON-2 Client Helper
Kompatibel dengan Odoo 19.0 & Odoo 20.0 (Endpoint /json/2)
"""
import os
import time
import requests
from typing import Any, Callable, Dict, List, Optional, Union


class OdooAPIError(Exception):
    """Exception untuk error dari API Odoo JSON-2."""
    def __init__(self, message: str, status_code: Optional[int] = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class OdooClient:
    """
    Klien HTTP untuk berinteraksi dengan endpoint Odoo /json/2/<model>/<method>
    Menggunakan autentikasi Bearer API Key dan header X-Odoo-Database.
    """
    def __init__(
        self,
        url: str,
        db: str,
        api_key: str,
        endpoint: str = "/json/2",
        timeout: int = 60,
        log_callback: Optional[Callable[[str, str], None]] = None
    ):
        clean_url = url.rstrip("/")
        # Auto-translate localhost/127.0.0.1 saat berjalan di dalam Docker container
        if (os.path.exists("/.dockerenv") or os.environ.get("RUNNING_IN_DOCKER")) and ("localhost" in clean_url or "127.0.0.1" in clean_url):
            clean_url = clean_url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
        self.url = clean_url
        self.db = db
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.log_callback = log_callback

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "X-Odoo-Database": self.db,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _log(self, text: str, level: str = "info"):
        if self.log_callback:
            try:
                self.log_callback(text, level)
                return
            except Exception:
                pass
        print(text)

    def call(self, model: str, method: str, ids: Optional[List[int]] = None, max_retries: int = 6, **kwargs) -> Any:
        """
        Mengeksekusi request POST ke /json/2/<model>/<method>
        Dilengkapi otomatisasi penanganan Rate Limiting (HTTP 429) & gateway timeout dengan exponential backoff.
        """
        url = f"{self.url}{self.endpoint}/{model}/{method}"
        payload = dict(kwargs)
        if ids is not None:
            payload["ids"] = ids

        for attempt in range(max_retries):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise OdooAPIError(f"Koneksi gagal ke {url}: {e}") from e
                wait_sec = 2.0 * (attempt + 1)
                self._log(f"⚠️ [Network Retry] Gagal terhubung ke {model}.{method} ({e}). Mencoba ulang dalam {wait_sec:.1f}s...", "warning")
                time.sleep(wait_sec)
                continue

            # 1. Penanganan HTTP 429: Too Many Requests (Rate Limiting)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_sec = float(retry_after) + 0.5
                    except ValueError:
                        wait_sec = 2.0 * (2 ** attempt)
                else:
                    wait_sec = 2.0 * (2 ** attempt)

                wait_sec = min(wait_sec, 60.0)
                self._log(
                    f"⏳ [Rate Limit 429] Server Odoo Cloud ({self.url}) membatasi laju request. "
                    f"Otomatis menunggu {wait_sec:.1f}s sebelum mencoba kembali (percobaan {attempt + 1}/{max_retries})...",
                    "warning"
                )
                time.sleep(wait_sec)
                continue

            # 2. Penanganan Transient Gateway Errors (502, 503, 504)
            if response.status_code in (502, 503, 504):
                wait_sec = 2.0 * (attempt + 1)
                self._log(
                    f"⏳ [Gateway {response.status_code}] Server Odoo sedang sibuk. "
                    f"Menunggu {wait_sec:.1f}s sebelum retry...",
                    "warning"
                )
                time.sleep(wait_sec)
                continue

            # 3. Parse Respons JSON
            try:
                data = response.json()
            except ValueError:
                data = response.text

            if not response.ok:
                error_msg = f"API Error ({response.status_code}) pada {model}.{method}"
                if isinstance(data, dict):
                    msg = data.get("message") or data.get("name")
                    if msg:
                        error_msg += f": {msg}"
                raise OdooAPIError(error_msg, status_code=response.status_code, details=data)

            return data

        raise OdooAPIError(f"Rate limit / Request gagal setelah {max_retries} percobaan pada {model}.{method}", status_code=429)

    def search(
        self,
        model: str,
        domain: Optional[List] = None,
        offset: int = 0,
        limit: Optional[int] = None,
        order: Optional[str] = None
    ) -> List[int]:
        """Mencari ID record yang cocok dengan domain."""
        kwargs = {"domain": domain or []}
        if offset:
            kwargs["offset"] = offset
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.call(model, "search", **kwargs)

    def read(
        self,
        model: str,
        ids: List[int],
        fields: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Membaca field dari daftar ID record."""
        kwargs = {}
        if fields is not None:
            kwargs["fields"] = fields
        return self.call(model, "read", ids=ids, **kwargs)

    def search_read(
        self,
        model: str,
        domain: Optional[List] = None,
        fields: Optional[List[str]] = None,
        offset: int = 0,
        limit: Optional[int] = None,
        order: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Mencari dan membaca record sekaligus."""
        kwargs = {"domain": domain or []}
        if fields is not None:
            kwargs["fields"] = fields
        if offset:
            kwargs["offset"] = offset
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.call(model, "search_read", **kwargs)

    def create(
        self,
        model: str,
        vals_list: Union[Dict[str, Any], List[Dict[str, Any]]]
    ) -> List[int]:
        """
        Membuat record baru di Odoo.
        vals_list bisa berupa single dict atau list of dicts.
        Mengembalikan daftar ID baru.
        """
        if isinstance(vals_list, dict):
            vals_list = [vals_list]
        return self.call(model, "create", vals_list=vals_list)

    def write(self, model: str, ids: List[int], vals: Dict[str, Any]) -> bool:
        """Memperbarui nilai record."""
        return self.call(model, "write", ids=ids, vals=vals)

    def execute_method(self, model: str, method: str, ids: List[int], **kwargs) -> Any:
        """Menjalankan method bisnis pada record (misal: action_post, reconcile)."""
        return self.call(model, method, ids=ids, **kwargs)
