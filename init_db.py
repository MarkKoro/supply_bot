from __future__ import annotations
import asyncio
from settings import get_settings
import db

def main() -> None:
    s = get_settings()
    asyncio.run(db.init_db(s.DB_PATH))
    print(f"DB initialized: {s.DB_PATH}")

if __name__ == "__main__":
    main()
