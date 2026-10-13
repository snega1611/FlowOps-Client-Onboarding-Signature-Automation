import sys

sys.path.insert(0, ".")
from app.config import Settings  # noqa: E402
from app.db import init_db  # noqa: E402

s = Settings.from_env()
init_db(s.db_path)
print(f"initialised {s.db_path}")
