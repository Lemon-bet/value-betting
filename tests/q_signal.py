import sqlite3
c = sqlite3.connect("data/vb.sqlite")
s = c.execute("SELECT fixture_id, market_id, outcome_id, price, ref_price FROM signals WHERE id=6").fetchone()
print("señal 6:", s)
for r in c.execute("""SELECT substr(swept_at,12,8), bookmaker, outcome_id, price, substr(changed_at,12,8)
                      FROM snapshots WHERE fixture_id=? AND market_id=? AND bookmaker IN ('bet365.es','pinnacle+30')
                      ORDER BY swept_at DESC, bookmaker, outcome_id LIMIT 16""", (s[0], s[1])):
    print(r)
