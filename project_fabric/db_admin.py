from __future__ import annotations

import argparse
import json
import os

from .postgres_backend import PostgresTTLCache, configured_database_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["status", "migrate"], default="status")
    parser.add_argument("--require", action="store_true")
    args = parser.parse_args()

    url = configured_database_url()
    namespace = os.getenv("PROJECT_FABRIC_CACHE_NAMESPACE", "shared-v1").strip() or "shared-v1"

    if not url:
        print(json.dumps({
            "backend": "local",
            "configured": False,
            "reachable": False,
            "namespace": namespace,
        }, separators=(",", ":")))
        return 2 if args.require else 0

    cache = PostgresTTLCache(
        url,
        namespace=namespace,
        service="db-admin",
        auto_migrate=True,
    )
    if args.command == "migrate":
        cache.migrate()

    status = cache.health_check()
    print(json.dumps({
        "backend": status.backend,
        "configured": status.configured,
        "reachable": status.reachable,
        "namespace": status.namespace,
    }, separators=(",", ":")))
    return 0 if status.reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
