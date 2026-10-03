from __future__ import annotations

import json
import os

from project_fabric.observability import ObservabilityClient


service = os.getenv("PHASE4_PROBE_SERVICE", "").strip()
if not service:
    raise SystemExit("PHASE4_PROBE_SERVICE is required")

client = ObservabilityClient(service, strict=True, auto_migrate=True)
client.start_run(
    workflow="Phase 4 Production Verify",
    event="verification",
    ref=os.getenv("GITHUB_REF"),
    sha=os.getenv("GITHUB_SHA"),
    metadata={"verification": True},
)
client.record_health(
    component="shared_database",
    status="ok",
    detail={"verification": True},
)
signal_id = client.record_signal(
    signal_type="PHASE4_PROBE",
    entity=service,
    score=100,
    status="verified",
    payload={"verification": True, "phase": 4},
)
delivery_id = client.record_delivery(
    channel="control-center-probe",
    status="success",
    signal_event_id=signal_id,
    destination=service,
    metadata={"verification": True},
)
client.finish_run("success", summary={"verification": True})

with client._connect() as con:
    with con.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM project_fabric.bot_runs WHERE service=%s AND run_id=%s",
            (service, client.run_id),
        )
        run_count = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM project_fabric.signal_events WHERE event_id=%s",
            (signal_id,),
        )
        signal_count = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM project_fabric.delivery_events WHERE event_id=%s",
            (delivery_id,),
        )
        delivery_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT count(*) FROM project_fabric.health_checks
            WHERE service=%s AND run_id=%s AND component='shared_database'
            """,
            (service, client.run_id),
        )
        health_count = cur.fetchone()[0]

if (run_count, signal_count, delivery_count, health_count) != (1, 1, 1, 1):
    raise SystemExit(
        f"Phase 4 persistence mismatch: run={run_count} signal={signal_count} "
        f"delivery={delivery_count} health={health_count}"
    )

print(json.dumps({
    "phase4": "PASS",
    "service": service,
    "run_id": client.run_id,
    "run": run_count,
    "signal": signal_count,
    "delivery": delivery_count,
    "health": health_count,
}, sort_keys=True))
