import sqlite3

conn = sqlite3.connect('heist_data.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [row[0] for row in cur.fetchall()]

before = {}
for table in tables:
    cur.execute(f'SELECT COUNT(*) FROM {table}')
    before[table] = cur.fetchone()[0]
print('before=', before)

for table in tables:
    cur.execute(f'DELETE FROM {table}')

conn.commit()

after = {}
for table in tables:
    cur.execute(f'SELECT COUNT(*) FROM {table}')
    after[table] = cur.fetchone()[0]
print('after=', after)
print('wal_checkpoint=', cur.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall())
conn.close()
