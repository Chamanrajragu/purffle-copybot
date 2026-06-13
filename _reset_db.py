import sqlite3
c = sqlite3.connect(r"D:\purfflebot\purfflebot.db")
c.executescript("""
DELETE FROM trades;
DELETE FROM positions;
DELETE FROM snapshots;
UPDATE state SET value='100.0' WHERE key IN ('cash','starting_capital');
""")
c.commit()
print("Purffle DB reset: $100 cash, no trades/positions/snapshots")
