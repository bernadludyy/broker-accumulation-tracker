from __future__ import annotations

import argparse

from project_fabric.insider_intelligence import collect_global, collect_ticker


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect official IDX insider disclosures into Neon")
    parser.add_argument("--ticker", help="Collect one issuer using company-announcements")
    parser.add_argument("--pages", type=int, default=1, help="Global announcement pages, 1-5")
    args = parser.parse_args()

    result = collect_ticker(args.ticker) if args.ticker else collect_global(args.pages)
    print(
        f"scope={result.scope} matched={result.matched} stored={result.stored} "
        f"existing={result.skipped_existing} zapi_calls={result.zapi_calls} "
        f"pdf_errors={result.pdf_errors}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
