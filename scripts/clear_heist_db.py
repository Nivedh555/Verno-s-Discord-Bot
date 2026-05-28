import sqlite3
import shutil
import os
import sys

DB = 'heist_data.db'
BACKUP = 'heist_data.db.bak'

if not os.path.exists(DB):
    print(f"Database file not found: {DB}")
    sys.exit(0)

# Backup
shutil.copyfile(DB, BACKUP)
print(f"Backed up {DB} -> {BACKUP}")

conn = sqlite3.connect(DB)
cur = conn.cursor()

# List tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]

counts_before = {}
for t in tables:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        counts_before[t] = cur.fetchone()[0]
    except Exception as e:
        counts_before[t] = f"ERROR: {e}"

print('before=', counts_before)

# Delete rows from each table
for t in tables:
    try:
        cur.execute(f"DELETE FROM {t}")
    except Exception as e:
        print(f"Error deleting from {t}: {e}")

conn.commit()

counts_after = {}
for t in tables:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        counts_after[t] = cur.fetchone()[0]
    except Exception as e:
        counts_after[t] = f"ERROR: {e}"

print('after=', counts_after)

try:
    cp = cur.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
    print('wal_checkpoint=', cp)
except Exception as e:
    print('wal_checkpoint_error=', e)

conn.close()
print('Done')
