import sqlite3

from pahra.db import init_sqlite_wal
from pahra.setup_guides import setup_guide


def test_old_database_gets_lan_ip_column(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE devices (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, client_id TEXT NOT NULL UNIQUE, "
              "name TEXT NOT NULL, type TEXT NOT NULL, brand TEXT, setup_method TEXT NOT NULL, linked_ip TEXT, "
              "last_seen TEXT, created_at TEXT NOT NULL)")
    c.commit()
    c.close()
    conn = init_sqlite_wal(str(db))
    assert "lan_ip" in {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
    init_sqlite_wal(str(db))  # idempotent


def test_forwarder_setup_guide(settings):
    dev = {"client_id": "living-room-lg", "setup_method": "forwarder", "brand": "LG", "name": "LG"}
    g = setup_guide(settings, dev, "192.168.0.23")
    assert g["values"]["forwarder_ip"] == "192.168.0.23"
    assert any("192.168.0.23" in x for x in g["steps"])
    assert any("laptop" in x.lower() for x in g["steps"])
    assert "this laptop's WiFi IP" in " ".join(setup_guide(settings, dev)["steps"])
