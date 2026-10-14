#!/usr/bin/env python3
"""Týdenní přehled slev: snímek /prehled/<rok>-<týden> + Web Push odběratelům (+ e-mail, je-li EMAIL_BACKEND).

  python weekly_digest.py            # pošle jen v pátek 17–18 h pražského času (cron běží 15:17 i 16:17 UTC)
  python weekly_digest.py --force    # hned (ruční spuštění workflow)
  python weekly_digest.py --dry-run  # jen vypíše položky, nic neukládá ani neposílá
Idempotentní: jeden přehled na týden, push i e-mail se pošlou nejvýš jednou.
"""
import argparse
import json
import logging
import sys

from lovceny.db import SessionLocal, init_db
from lovceny import digest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_db()
    with SessionLocal() as db:
        if a.dry_run:
            items = digest.build_items(db)
            print(json.dumps({"week": digest.week_of(), "items": items}, ensure_ascii=False, indent=1))
            return 0
        out = digest.run(db, force=a.force)
    print("SOUHRN", json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
