"""生成两个示例库：demo 用于成功升级演示，billing 用于失败回滚演示。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main() -> None:
    DATA.mkdir(exist_ok=True)
    demo = DATA / "demo.db"
    billing = DATA / "billing.db"
    for p in (demo, billing):
        p.unlink(missing_ok=True)

    with sqlite3.connect(demo) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO users (id, name) VALUES (1, 'alice'), (2, 'bob; lit')")

    with sqlite3.connect(billing) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE)"
        )
        conn.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, "
            "FOREIGN KEY(customer_id) REFERENCES customers(id))"
        )
        conn.execute("INSERT INTO customers (id, email) VALUES (1, 'a@example.com')")
        conn.execute("INSERT INTO orders (id, customer_id) VALUES (10, 1)")

    print(f"created {demo} and {billing}")


if __name__ == "__main__":
    main()
