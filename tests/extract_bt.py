"""Extrae el JSON devuelto por el navegador (guardado por el puente) a data/bt/*.json."""
import json
import sys


def extract(path: str) -> list:
    txt = json.load(open(path))[0]["text"]
    cut = txt.rfind('"', 0, len(txt) - 5)
    inner = json.loads(txt[:cut + 1])
    return json.loads(inner)


if __name__ == "__main__":
    out = sys.argv[2]
    data = extract(sys.argv[1])
    json.dump(data, open(out, "w"))
    for f in data:
        n = {bk: sum(len(a) for mm in v.values() for a in mm.values()) for bk, v in f["books"].items()}
        print(f["t"][:16], f["h"], "-", f["a"], n)
