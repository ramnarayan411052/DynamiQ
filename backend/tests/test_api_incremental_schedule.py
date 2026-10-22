import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app


UTC = timezone.utc
ARRIVAL = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
DUE = datetime(2030, 1, 1, 14, 0, tzinfo=UTC)


def request_body(**job_overrides):
    job = {
        "id": "JOB-1",
        "task": "Mill part",
        "operation": "Mill",
        "duration_minutes": 30,
        "arrival": ARRIVAL.isoformat(),
        "due": DUE.isoformat(),
        "allow_late": True,
    }
    job.update(job_overrides)
    return {
        "jobs": [job],
        "operators": [{"id": "OP-1", "name": "Operator", "skills": ["Mill"]}],
        "machines": [{
            "id": "M-1",
            "name": "Mill",
            "machine_type": "General",
            "supported_operations": ["Mill"],
        }],
        "time_limit_seconds": 1,
        "num_workers": 1,
    }


class TestIncrementalScheduleAPI(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.environment_patch = patch.dict(
            os.environ,
            {"SQLITE_PATH": str(Path(self.temporary_directory.name) / "api.sqlite3")},
        )
        self.environment_patch.start()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment_patch.stop()
        self.temporary_directory.cleanup()

    def test_fixed_reservation_and_not_before_preserve_job_arrival(self):
        for scheduler in ("cp_sat", "fcfs", "sjf", "priority"):
            with self.subTest(scheduler=scheduler):
                body = request_body(not_before=(ARRIVAL + timedelta(hours=2)).isoformat())
                body["scheduler"] = scheduler
                body["fixed_schedule"] = [{
                    "machine": "M-1",
                    "operator_id": "OP-1",
                    "start_at": ARRIVAL.isoformat(),
                    "end_at": (ARRIVAL + timedelta(hours=1)).isoformat(),
                }]

                response = self.client.post("/api/schedule", json=body)

                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                row = result["schedule"][0]
                reference = datetime.fromisoformat(result["reference_time"])
                actual_start = reference + timedelta(minutes=row["start_minute"])
                actual_arrival = reference + timedelta(minutes=row["arrival_minute"])
                self.assertGreaterEqual(actual_start, ARRIVAL + timedelta(hours=2))
                self.assertEqual(actual_arrival, ARRIVAL)

    def test_priority_schedule_orders_hard_deadlines_before_soft_priority_jobs(self):
        body = request_body(duration_minutes=5, priority="Critical")
        body["jobs"][0]["allow_late"] = True
        body["jobs"].append({
            **body["jobs"][0],
            "id": "JOB-HARD-DEADLINE",
            "duration_minutes": 4,
            "priority": "Low",
            "due": (ARRIVAL + timedelta(minutes=6)).isoformat(),
            "allow_late": False,
        })
        body["scheduler"] = "priority"

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        rows = {row["job_id"]: row for row in result["schedule"]}
        self.assertEqual(rows["JOB-HARD-DEADLINE"]["start_minute"], 0)
        self.assertLessEqual(rows["JOB-HARD-DEADLINE"]["end_minute"], 6)
        self.assertEqual(result["validation"]["valid"], True)

    def test_empty_job_list_is_rejected_by_request_validation(self):
        body = request_body()
        body["jobs"] = []

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 422)

    def test_fixed_reservation_with_nonpositive_duration_is_rejected(self):
        body = request_body()
        body["fixed_schedule"] = [{
            "machine": "M-1",
            "operator_id": "OP-1",
            "start_at": ARRIVAL.isoformat(),
            "end_at": ARRIVAL.isoformat(),
        }]

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 422)
        self.assertIn("must end after they start", response.json()["detail"])

    def test_unknown_job_dependency_is_rejected(self):
        response = self.client.post(
            "/api/schedule",
            json=request_body(dependencies=["MISSING"]),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("unknown predecessor", response.json()["detail"])

    def test_dependency_cycle_is_rejected(self):
        body = request_body()
        body["jobs"] = [
            {**body["jobs"][0], "id": "JOB-1", "dependencies": ["JOB-2"]},
            {**body["jobs"][0], "id": "JOB-2", "dependencies": ["JOB-1"]},
        ]

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 422)
        self.assertIn("cycle", response.json()["detail"])

    def test_case_insensitive_duplicate_job_ids_are_rejected(self):
        body = request_body()
        body["jobs"] = [
            body["jobs"][0],
            {**body["jobs"][0], "id": "job-1"},
        ]

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 422)
        self.assertIn("unique", response.json()["detail"])

    def test_hard_due_date_infeasibility_is_reported_without_fake_schedule(self):
        body = request_body(
            duration_minutes=90,
            due=(ARRIVAL + timedelta(minutes=30)).isoformat(),
            allow_late=False,
        )

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertEqual(result["schedule"], [])
        self.assertEqual(result["metrics"]["unscheduled_task_count"], 1)

    def test_shared_operator_on_different_machines_serializes_jobs(self):
        body = request_body()
        body["jobs"] = [
            {
                "id": "JOB-CNC",
                "task": "Mill part",
                "operation": "Mill",
                "duration_minutes": 40,
                "arrival": ARRIVAL.isoformat(),
                "due": DUE.isoformat(),
            },
            {
                "id": "JOB-QC",
                "task": "Inspect part",
                "operation": "Inspect",
                "duration_minutes": 25,
                "arrival": ARRIVAL.isoformat(),
                "due": DUE.isoformat(),
            },
        ]
        body["operators"] = [{
            "id": "OP-SHARED",
            "name": "Multi-skilled operator",
            "skills": ["Mill", "Inspect"],
        }]
        body["machines"] = [
            {
                "id": "M-MILL",
                "name": "Mill",
                "machine_type": "Mill",
                "supported_operations": ["Mill"],
            },
            {
                "id": "M-QC",
                "name": "Inspection station",
                "machine_type": "Inspection",
                "supported_operations": ["Inspect"],
            },
        ]

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()["schedule"]
        self.assertEqual({row["job_id"] for row in rows}, {"JOB-CNC", "JOB-QC"})
        ordered = sorted(rows, key=lambda row: row["start_minute"])
        self.assertGreaterEqual(ordered[1]["start_minute"], ordered[0]["end_minute"])
        self.assertEqual({row["operator_id"] for row in rows}, {"OP-SHARED"})

    def test_shared_machine_with_different_operators_serializes_jobs(self):
        body = request_body()
        body["jobs"] = [
            {
                "id": "JOB-MILL",
                "task": "Mill part",
                "operation": "Mill",
                "duration_minutes": 35,
                "arrival": ARRIVAL.isoformat(),
                "due": DUE.isoformat(),
                "required_skills": ["Mill"],
            },
            {
                "id": "JOB-WELD",
                "task": "Weld bracket",
                "operation": "Weld",
                "duration_minutes": 20,
                "arrival": ARRIVAL.isoformat(),
                "due": DUE.isoformat(),
                "required_skills": ["Weld"],
            },
        ]
        body["operators"] = [
            {"id": "OP-MILL", "name": "Mill operator", "skills": ["Mill"]},
            {"id": "OP-WELD", "name": "Weld operator", "skills": ["Weld"]},
        ]
        body["machines"] = [{
            "id": "M-FLEX",
            "name": "Flexible machine",
            "machine_type": "Flexible",
            "supported_operations": ["Mill", "Weld"],
        }]

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()["schedule"]
        self.assertEqual({row["job_id"] for row in rows}, {"JOB-MILL", "JOB-WELD"})
        ordered = sorted(rows, key=lambda row: row["start_minute"])
        self.assertGreaterEqual(ordered[1]["start_minute"], ordered[0]["end_minute"])
        self.assertEqual({row["machine"] for row in rows}, {"M-FLEX"})

    def test_linear_dependency_chain_sharing_resources_finishes_without_deadlock(self):
        body = request_body()
        body["jobs"] = [
            {
                "id": "JOB-A",
                "task": "First stage",
                "operation": "Mill",
                "duration_minutes": 15,
                "arrival": ARRIVAL.isoformat(),
                "due": DUE.isoformat(),
            },
            {
                "id": "JOB-B",
                "task": "Second stage",
                "operation": "Mill",
                "duration_minutes": 20,
                "arrival": ARRIVAL.isoformat(),
                "due": DUE.isoformat(),
                "dependencies": ["JOB-A"],
            },
            {
                "id": "JOB-C",
                "task": "Third stage",
                "operation": "Mill",
                "duration_minutes": 10,
                "arrival": ARRIVAL.isoformat(),
                "due": DUE.isoformat(),
                "dependencies": ["JOB-B"],
            },
        ]

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIn(result["status"], {"OPTIMAL", "FEASIBLE"})
        rows = {row["job_id"]: row for row in result["schedule"]}
        self.assertEqual(set(rows), {"JOB-A", "JOB-B", "JOB-C"})
        self.assertGreaterEqual(rows["JOB-B"]["start_minute"], rows["JOB-A"]["end_minute"])
        self.assertGreaterEqual(rows["JOB-C"]["start_minute"], rows["JOB-B"]["end_minute"])

    def test_fixed_resource_blocks_can_make_hard_deadline_unschedulable(self):
        body = request_body(
            duration_minutes=45,
            due=(ARRIVAL + timedelta(minutes=60)).isoformat(),
            allow_late=False,
        )
        body["fixed_schedule"] = [
            {
                "machine": "M-1",
                "operator_id": "OP-1",
                "start_at": ARRIVAL.isoformat(),
                "end_at": (ARRIVAL + timedelta(minutes=20)).isoformat(),
            },
            {
                "machine": "M-1",
                "operator_id": "OP-1",
                "start_at": (ARRIVAL + timedelta(minutes=40)).isoformat(),
                "end_at": (ARRIVAL + timedelta(minutes=60)).isoformat(),
            },
        ]

        response = self.client.post("/api/schedule", json=body)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertEqual(result["schedule"], [])
        self.assertEqual(result["metrics"]["unscheduled_task_count"], 1)


if __name__ == "__main__":
    unittest.main()
