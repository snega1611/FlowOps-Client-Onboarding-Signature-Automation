
import sqlite3

db = sqlite3.connect("data/flowops.db")

columns = [
    row[1]
    for row in db.execute("PRAGMA table_info(signature_requests)")
]

if "signing_url" not in columns:
    db.execute(
        "ALTER TABLE signature_requests ADD COLUMN signing_url TEXT"
    )
    db.commit()
    print("Done! Signing URL column added.")
else:
    print("Already done! The column exists.")

db.close()
