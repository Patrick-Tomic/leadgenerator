"""Command-line maintenance. Run with the app's virtualenv, e.g.:

  .venv/bin/python -m app.cli sunbiz-import data/sunbiz            # import new daily files
  .venv/bin/python -m app.cli sunbiz-import 20261015c.txt --force   # re-import one file
  .venv/bin/python -m app.cli sunbiz-match                          # re-run matching to businesses
"""
import argparse
import json
import logging
from pathlib import Path

from . import db, registrations


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("sunbiz-import", help="import Sunbiz daily/quarterly data files")
    imp.add_argument("path", nargs="?", help="a data file or directory (default: SUNBIZ_IMPORT_DIR)")
    imp.add_argument("--since", help="skip registrations filed before YYYY-MM-DD")
    imp.add_argument("--kind", choices=["sunbiz_cor", "sunbiz_fic"], help="file type if the name isn't yyyymmddc/f.txt")
    imp.add_argument("--force", action="store_true", help="re-import files already imported")
    sub.add_parser("sunbiz-match", help="match all registrations against known businesses again")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db.conn()
    if args.cmd == "sunbiz-import":
        p = Path(args.path) if args.path else None
        results = [registrations.import_file(p, args.kind, args.since, args.force)] if p and p.is_file() else \
            registrations.import_dir(p, args.since, args.force)
        print(json.dumps(results, indent=2))
    elif args.cmd == "sunbiz-match":
        print(f"linked {registrations.match_registrations(only_unmatched=False)} registrations")


if __name__ == "__main__":
    main()
