"""Consulta rápida: una señal, sus snapshots y todas las líneas de totales de la casa en el último barrido.
Uso: python tests/q_signal.py [ID_SEÑAL]
"""
import sqlite3
import sys

sid = int(sys.argv[1]) if len(sys.argv) > 1 else 6
c = sqlite3.connect("data/vb.sqlite")
s = c.execute("SELECT fixture_id, market_id, outcome_id, price, ref_price, bookmaker FROM signals WHERE id=?", (sid,)).fetchone()
print("señal", sid, ":", s)
fid, mid, _, _, _, book = s
for r in c.execute("""SELECT substr(swept_at,12,8), bookmaker, outcome_id, price, substr(changed_at,12,8)
                      FROM snapshots WHERE fixture_id=? AND market_id=? AND bookmaker IN (?, 'pinnacle+30')
                      ORDER BY swept_at DESC, bookmaker, outcome_id LIMIT 8""", (fid, mid, book)):
    print(r)
last = c.execute("SELECT MAX(swept_at) FROM snapshots WHERE fixture_id=? AND bookmaker=?", (fid, book)).fetchone()[0]
print(f"\n{book} · todas las líneas de totales en el barrido {last[11:19]} (línea, resultado, cuota, cambiada a):")
for r in c.execute("""SELECT handicap, outcome_id, price, substr(changed_at,12,8)
                      FROM snapshots WHERE fixture_id=? AND bookmaker=? AND swept_at=? AND market_type='totals'
                      ORDER BY handicap, outcome_id""", (fid, book, last)):
    print("  ", r)
