from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .postgres_backend import PostgresTTLCache, configured_database_url


def _report_path() -> Path:
    root = Path(os.getenv("FABRIC_RUNTIME_DIR", ".runtime/project_fabric"))
    return root / "api_usage.json"


def _markdown(data: dict) -> str:
    totals = data.get("totals", {})
    lines = [
        "### Project Fabric API Usage",
        "",
        f"- Service: `{data.get('service', '-')}`",
        f"- Run: `{data.get('run_id', '-')}`",
        f"- Operations: **{totals.get('operations', 0)}**",
        f"- Upstream operations: **{totals.get('upstream_operations', 0)}**",
        f"- HTTP attempts: **{totals.get('upstream_attempts', 0)}**",
        f"- Cache hits: **{totals.get('cache_hits', 0)}**",
        f"- Stale fallbacks: **{totals.get('stale_hits', 0)}**",
        f"- Errors: **{totals.get('errors', 0)}**",
        "",
        "| Endpoint | Ops | Upstream | Attempts | Cache | Stale | Errors |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in sorted(data.get("endpoints", {}).items()):
        lines.append(
            f"| {name} | {item.get('operations', 0)} | "
            f"{item.get('upstream_operations', 0)} | {item.get('upstream_attempts', 0)} | "
            f"{item.get('cache_hits', 0)} | {item.get('stale_hits', 0)} | "
            f"{item.get('errors', 0)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-summary", action="store_true")
    args = parser.parse_args()

    path = _report_path()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {
            "schema_version": 1,
            "service": "no-data",
            "run_id": os.getenv("GITHUB_RUN_ID", "local"),
            "totals": {},
            "endpoints": {},
        }

    print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))

    database_url = configured_database_url()
    if database_url:
        required = os.getenv("PROJECT_FABRIC_DB_REQUIRED", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        try:
            namespace = (
                os.getenv("PROJECT_FABRIC_CACHE_NAMESPACE", "shared-v1").strip()
                or "shared-v1"
            )
            PostgresTTLCache(
                database_url,
                namespace=namespace,
                service=str(data.get("service") or "report"),
                auto_migrate=True,
            ).persist_usage(data)
        except Exception as exc:
            print(
                f"Project Fabric usage database persistence failed: {type(exc).__name__}",
            )
            if required:
                raise

    if args.github_summary:
        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
