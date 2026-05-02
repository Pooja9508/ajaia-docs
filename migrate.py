"""
One-shot schema migration for existing ajaia_docs.db databases.
Run once if you have an existing database from a previous version:

    python migrate.py

Safe to run multiple times — each ALTER is guarded with try/except.
"""
import os
import sqlite3

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ajaia_docs.db")

if not DATABASE_URL.startswith("sqlite"):
    print("PostgreSQL detected — skipping SQLite migration.")
    print("For PostgreSQL, run the ALTER TABLE statements manually or use Alembic.")
    raise SystemExit(0)

db_path = DATABASE_URL.replace("sqlite:///", "").replace("./", "")
print(f"Migrating: {db_path}")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

migrations = [
    # Phase 2: public link sharing
    ("documents", "public_permission", "ALTER TABLE documents ADD COLUMN public_permission VARCHAR(20)"),
    ("documents", "public_token",      "ALTER TABLE documents ADD COLUMN public_token VARCHAR(100)"),
    # Phase 2: file attachment size
    ("file_attachments", "file_size",  "ALTER TABLE file_attachments ADD COLUMN file_size INTEGER"),
]

for table, column, sql in migrations:
    try:
        cur.execute(sql)
        print(f"  + {table}.{column}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print(f"  = {table}.{column} (already exists)")
        else:
            print(f"  ! {table}.{column}: {e}")

# Create unique index for public_token (SQLite can't do ADD COLUMN UNIQUE)
try:
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_documents_public_token "
        "ON documents(public_token) WHERE public_token IS NOT NULL"
    )
    print("  + index ix_documents_public_token")
except sqlite3.OperationalError as e:
    print(f"  = index ix_documents_public_token: {e}")

conn.commit()
conn.close()
print("Migration complete.")
