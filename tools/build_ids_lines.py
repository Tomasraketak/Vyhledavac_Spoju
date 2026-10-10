"""Seznam autobusových linek zařazených do integrovaných systémů (z JDF).

Výstup ``vyhledavac/data/ids_lines.json.gz``::

    {"generated": "...", "systems": {"PID": ["100110", ...], ...},
     "zoned": ["320011", ...]}

* ``systems`` – linky s kódem dopravního systému v ``Linext.txt``
  (30001 PID, 30314 IDS JK/IDESKA, 30421 DÚK, 30512 IDOL, 30522 IREDO,
  30621 IDS JMK, 30722 IDZK, 30811 ODIS);
* ``zoned`` – linky, které mají v ``Zaslinky.txt`` tarifní pásma (IDPK, VDV,
  IDSOK a další systémy bez kódu v Linext).

Spuštění (JDF.zip z CIS JŘ, stejný jako pro ``build_stations.py``)::

    python tools/build_ids_lines.py --jdf cesta/k/JDF.zip
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import zipfile
from pathlib import Path

import build_stations as b

CODES = {"30001": "PID", "30314": "IDESKA", "30421": "DÚK", "30512": "IDOL", "30522": "IREDO",
         "30621": "IDS JMK", "30722": "IDZK", "30811": "ODIS"}
OUTPUT = Path(__file__).resolve().parent.parent / "vyhledavac" / "data" / "ids_lines.json.gz"


def build(jdf: Path) -> dict:
    systems: dict[str, set[str]] = {name: set() for name in CODES.values()}
    zoned: set[str] = set()
    with zipfile.ZipFile(jdf) as outer:
        for name in outer.namelist():
            try:
                inner = zipfile.ZipFile(io.BytesIO(outer.read(name)))
            except zipfile.BadZipFile:
                continue
            files = {f.lower(): f for f in inner.namelist()}
            linext = b._find(files, "linext.txt")
            for row in b._jdf_rows(inner.read(linext)) if linext else ():
                if len(row) > 2 and row[2] in CODES:
                    systems[CODES[row[2]]].add(row[0])
            zaslinky = b._find(files, "zaslinky.txt")
            for row in b._jdf_rows(inner.read(zaslinky)) if zaslinky else ():
                if len(row) > 2 and row[2].strip():
                    zoned.add(row[0])
    return {"generated": dt.date.today().isoformat(),
            "source": "CIS JŘ – JDF (Linext.txt, Zaslinky.txt)",
            "systems": {k: sorted(v) for k, v in systems.items()},
            "zoned": sorted(zoned)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdf", type=Path, required=True)
    args = parser.parse_args()
    data = build(args.jdf)
    with gzip.open(OUTPUT, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print({k: len(v) for k, v in data["systems"].items()}, "zoned:", len(data["zoned"]),
          f"-> {OUTPUT} ({OUTPUT.stat().st_size // 1024} kB)")


if __name__ == "__main__":
    main()
