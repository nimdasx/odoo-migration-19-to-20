"""
Odoo External API JSON-2 Client Helper
Kompatibel dengan Odoo 19.0 & Odoo 20.0 (Endpoint /json/2)
"""
import os
import requests
from typing import Any, Dict, List, Optional, Union


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
        timeout: int = 60
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

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "X-Odoo-Database": self.db,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def call(self, model: str, method: str, ids: Optional[List[int]] = None, **kwargs) -> Any:
        """
        Mengeksekusi request POST ke /json/2/<model>/<method>
        """
        url = f"{self.url}{self.endpoint}/{model}/{method}"
        payload = dict(kwargs)
        if ids is not None:
            payload["ids"] = ids

        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise OdooAPIError(f"Koneksi gagal ke {url}: {e}") from e

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
