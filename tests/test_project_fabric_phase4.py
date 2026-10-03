from __future__ import annotations

import os
import unittest

from project_fabric.observability import ObservabilityClient


@unittest.skipUnless(
    os.getenv("PROJECT_FABRIC_DATABASE_URL", "").strip()
    and os.getenv("PROJECT_FABRIC_INTEGRATION_TESTS", "").strip() == "1",
    "Phase 4 PostgreSQL integration requires explicit integration-test opt-in",
)
class Phase4ObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ["PROJECT_FABRIC_DATABASE_URL"]

    def test_run_signal_delivery_health_roundtrip(self):
        service = f"phase4-test-{os.getenv('GITHUB_RUN_ID', 'local')}"
        run_id = f"{service}-run"
        client = ObservabilityClient(
            service,
            database_url=self.url,
            run_id=run_id,
            strict=True,
            auto_migrate=True,
        )

        client.start_run(
            workflow="phase4-test",
            event="test",
            ref="refs/heads/test",
            sha="deadbeef",
            metadata={"token": "must-not-persist", "safe": "yes"},
        )
        signal_id = client.record_signal(
            signal_type="TEST_SIGNAL",
            entity="TEST",
            score=88,
            status="verified",
            payload={"price": 123, "api_key": "must-not-persist"},
        )
        self.assertTrue(signal_id)

        delivery_id = client.record_delivery(
            channel="telegram",
            status="success",
            signal_event_id=signal_id,
            destination="123456789",
            metadata={"sent": 1},
        )
        self.assertTrue(delivery_id)

        health_id = client.record_health(
            component="shared_database",
            status="ok",
            latency_ms=12.5,
            detail={"reachable": True},
        )
        self.assertTrue(health_id)

        client.finish_run("success", summary={"alerts": 1})

        with client._connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, finished_at IS NOT NULL, metadata, summary
                    FROM project_fabric.bot_runs
                    WHERE service=%s AND run_id=%s
                    """,
                    (service, run_id),
                )
                run = cur.fetchone()
                self.assertEqual(run[0], "success")
                self.assertTrue(run[1])
                self.assertEqual(run[2]["token"], "<redacted>")
                self.assertEqual(run[3]["alerts"], 1)

                cur.execute(
                    """
                    SELECT signal_type, entity, score, payload
                    FROM project_fabric.signal_events
                    WHERE event_id=%s
                    """,
                    (signal_id,),
                )
                signal = cur.fetchone()
                self.assertEqual(signal[0], "TEST_SIGNAL")
                self.assertEqual(signal[1], "TEST")
                self.assertEqual(signal[2], 88)
                self.assertEqual(signal[3]["api_key"], "<redacted>")

                cur.execute(
                    """
                    SELECT channel, destination_hash, status
                    FROM project_fabric.delivery_events
                    WHERE event_id=%s
                    """,
                    (delivery_id,),
                )
                delivery = cur.fetchone()
                self.assertEqual(delivery[0], "telegram")
                self.assertNotEqual(delivery[1], "123456789")
                self.assertEqual(len(delivery[1]), 16)
                self.assertEqual(delivery[2], "success")

                cur.execute(
                    """
                    SELECT component, status, latency_ms
                    FROM project_fabric.health_checks
                    WHERE event_id=%s
                    """,
                    (health_id,),
                )
                health = cur.fetchone()
                self.assertEqual(health[0], "shared_database")
                self.assertEqual(health[1], "ok")
                self.assertAlmostEqual(health[2], 12.5)


if __name__ == "__main__":
    unittest.main()
