"""Ottimizza le immagini personaggio: master full-res in static/img/originals/,
versioni servite ridimensionate e compresse.

Idempotente: i master vengono spostati in originals/ alla prima esecuzione
(se ancora a root), poi le versioni servite sono SEMPRE rigenerate dai master.
Gli originali non vengono mai cancellati.

Uso:
    python scripts/optimize_images.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

IMG = Path("static/img")
ORIG = IMG / "originals"

# Master da spostare a root -> originals/ (one-time, guarded).
# (sorgente a root, destinazione in originals/)
MOVES = [
    ("doc.png", "doc.png"),
    ("pilota.png", "pilota.png"),
    ("stella.png", "stella.png"),
    ("virgilio.png", "virgilio-old.png"),  # vecchio "pensionato": conservato
    ("virgilio2.png", "virgilio.png"),      # nuovo approvato: master canonico
]

# Versioni servite: (master in originals/, percorso servito, lato max px)
# Lato max in px: ~2x della dimensione di display (hero 184px, avatar 120px).
SERVED = [
    ("virgilio.png", IMG / "virgilio.png", 384),          # hero splash login (display 184px)
    ("virgilio.png", IMG / "agents" / "virgilio.png", 240),
    ("doc.png", IMG / "agents" / "doc.png", 240),
    ("pilota.png", IMG / "agents" / "pilota.png", 240),
    ("stella.png", IMG / "agents" / "stella.png", 240),
]


def _kb(p: Path) -> str:
    return f"{p.stat().st_size / 1024:.1f} KB" if p.exists() else "—"


def main() -> None:
    ORIG.mkdir(parents=True, exist_ok=True)

    # 1. Sposta i master a root dentro originals/ (solo se ancora presenti a root).
    for src_name, dst_name in MOVES:
        src = IMG / src_name
        dst = ORIG / dst_name
        if src.exists():
            if dst.exists():
                print(f"skip move {src_name}: {dst} già presente")
            else:
                shutil.move(str(src), str(dst))
                print(f"moved {src_name} -> {dst}")

    # 2. Rigenera le versioni servite dai master in originals/.
    #    Ogni master è decodificato una sola volta anche se alimenta più varianti.
    cache: dict[str, Image.Image] = {}
    for master_name, served_path, size in SERVED:
        master = ORIG / master_name
        if not master.exists():
            print(f"WARN: master mancante {master} — salto {served_path}")
            continue
        src = cache.get(master_name)
        if src is None:
            src = Image.open(master)
            if src.mode not in ("RGB", "RGBA"):  # solo se necessario: preserva RGB->PNG più piccoli
                src = src.convert("RGBA")
            cache[master_name] = src
        served_path.parent.mkdir(parents=True, exist_ok=True)
        im = src.copy()  # thumbnail muta in-place: copia per non intaccare la cache
        im.thumbnail((size, size), Image.LANCZOS)
        im.save(served_path, optimize=True)
        print(f"served {served_path}  {im.size}  {_kb(served_path)}")


if __name__ == "__main__":
    main()
