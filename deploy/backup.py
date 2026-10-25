"""Copia diaria de la base de datos (válido en Windows y Linux). Conserva 30 días."""
import glob
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = os.path.join(ROOT, "data", "vb.sqlite")
dst_dir = os.path.join(ROOT, "backups")
os.makedirs(dst_dir, exist_ok=True)
dst = os.path.join(dst_dir, time.strftime("vb-%Y-%m-%d.sqlite"))
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
cut = time.time() - 30 * 86400
for f in glob.glob(os.path.join(dst_dir, "vb-*.sqlite")):
    if os.path.getmtime(f) < cut:
        os.remove(f)
print("copia:", dst)
