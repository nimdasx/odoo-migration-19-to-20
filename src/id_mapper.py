"""
Kamus Relasi ID (ID Mapper) Berbasis SQLite
Menyimpan dan mengelola relasi (model, old_id) -> new_id
"""
import os
import sqlite3
from typing import Dict, List, Optional, Tuple


class IdMapper:
    """
    Menyimpan kamus mapping ID record dari Odoo 19 ke Odoo 20.
    Menggunakan SQLite agar persisten, cepat, dan idempoten.
    """
    def __init__(self, db_path: str = "data/migration_map.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._init_db()

    @classmethod
    def get_db_path_from_config(cls, config: dict, base_dir: str = "data") -> str:
        """
        Menghasilkan nama file database unik berbasis host, port, dan db source & target.
        Format: <host_src>_<port_src>_<db_src>__<host_tgt>_<port_tgt>_<db_tgt>.db
        """
        if config.get("migration_map_db"):
            custom_path = config["migration_map_db"]
            return os.path.join(base_dir, custom_path) if not os.path.isabs(custom_path) else custom_path

        from urllib.parse import urlparse

        s_url = urlparse(config.get("source_odoo_19", {}).get("url", "http://localhost:1969"))
        t_url = urlparse(config.get("target_odoo_20", {}).get("url", "http://localhost:2069"))

        s_host = s_url.hostname or "localhost"
        s_port = s_url.port or (443 if s_url.scheme == "https" else 80)
        s_db = config.get("source_odoo_19", {}).get("db", "src")

        t_host = t_url.hostname or "localhost"
        t_port = t_url.port or (443 if t_url.scheme == "https" else 80)
        t_db = config.get("target_odoo_20", {}).get("db", "tgt")

        clean = lambda s: "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(s))

        filename = f"{clean(s_host)}_{clean(s_port)}_{clean(s_db)}__{clean(t_host)}_{clean(t_port)}_{clean(t_db)}.db"
        return os.path.join(base_dir, filename)

    @classmethod
    def from_config(cls, config: dict, base_dir: str = "data"):
        """Instansiasi IdMapper otomatis dengan nama database unik dari konfigurasi."""
        path = cls.get_db_path_from_config(config, base_dir)
        return cls(path)

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS id_mapping (
                    model TEXT NOT NULL,
                    old_id INTEGER NOT NULL,
                    new_id INTEGER NOT NULL,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (model, old_id)
                );
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_id_mapping_model ON id_mapping(model);
            """)

    def set_mapping(self, model: str, old_id: int, new_id: int, notes: Optional[str] = None) -> None:
        """Menyimpan atau memperbarui mapping 1 record."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO id_mapping (model, old_id, new_id, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model, old_id) DO UPDATE SET
                    new_id = excluded.new_id,
                    notes = excluded.notes,
                    created_at = CURRENT_TIMESTAMP
            """, (model, old_id, new_id, notes))

    def set_mappings_batch(self, model: str, pairs: List[Tuple[int, int, Optional[str]]]) -> None:
        """Menyimpan banyak mapping sekaligus secara efisien."""
        with self.conn:
            self.conn.executemany("""
                INSERT INTO id_mapping (model, old_id, new_id, notes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model, old_id) DO UPDATE SET
                    new_id = excluded.new_id,
                    notes = excluded.notes,
                    created_at = CURRENT_TIMESTAMP
            """, [(model, old_id, new_id, note) for old_id, new_id, note in pairs])

    def get_new_id(self, model: str, old_id: int) -> Optional[int]:
        """Mengambil new_id di Odoo 20 berdasarkan old_id di Odoo 19."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT new_id FROM id_mapping WHERE model = ? AND old_id = ?", (model, old_id))
        row = cursor.fetchone()
        return row[0] if row else None

    def has_mapping(self, model: str, old_id: int) -> bool:
        """Mengecek apakah record lama sudah pernah dimigrasikan."""
        return self.get_new_id(model, old_id) is not None

    def get_all_mappings(self, model: str) -> Dict[int, int]:
        """Mengambil seluruh kamus {old_id: new_id} untuk model tertentu."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT old_id, new_id FROM id_mapping WHERE model = ?", (model,))
        return {row[0]: row[1] for row in cursor.fetchall()}

    def count(self, model: Optional[str] = None) -> int:
        """Menghitung total record yang sudah terpetakan."""
        cursor = self.conn.cursor()
        if model:
            cursor.execute("SELECT COUNT(*) FROM id_mapping WHERE model = ?", (model,))
        else:
            cursor.execute("SELECT COUNT(*) FROM id_mapping")
        return cursor.fetchone()[0]

    def close(self):
        """Menutup koneksi database SQLite."""
        self.conn.close()
