import json
import logging
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import ValidationError

from api import FixedScheduleBlock, Job, Machine, Operator, ScheduleRequest, _operation_stages, create_schedule


ARRIVAL = datetime(2030, 1, 1, 9, 0)
DUE = datetime(2030, 1, 1, 14, 0)
REQUIRED_SKILLS = ["SMT Machine Operation", "Pick and Place"]


def machine(
    machine_id="M001",
    *,
    availability="Available",
    operations=None,
    materials=None,
    machine_type="SMT Assembly",
):
    return Machine(
        id=machine_id,
        name="PCB Pick and Place Machine",
        machine_type=machine_type,
        availability=availability,
        supported_operations=operations or ["Component Placement", "SMT Assembly"],
        supported_materials=materials or ["FR4 PCB", "SMD Components"],
        operating_cost_per_hour=650,
    )


def operator(skills=None, availability="Available"):
    return Operator(
        id="OP001",
        name="Qualified operator",
        availability=availability,
        skills=skills or REQUIRED_SKILLS,
    )


def job(**overrides):
    values = {
        "id": "JOB-1049",
        "task": "PCB SMD Component Placement",
        "operation": "Component Placement",
        "duration_minutes": 90,
        "arrival": ARRIVAL,
        "due": DUE,
        "priority": "High",
        "material": "FR4 PCB; SMD Components",
        "required_skills": REQUIRED_SKILLS,
        "allow_late": False,
    }
    values.update(overrides)
    return Job(**values)


def request(jobs=None, machines=None, operators=None, **options):
    return ScheduleRequest(
        jobs=jobs or [job()],
        machines=machines or [machine()],
        operators=operators or [operator()],
        time_limit_seconds=1,
        num_workers=1,
        **options,
    )


class TestJobEligibility(unittest.TestCase):
    def test_operation_field_parses_ordered_stages(self):
        self.assertEqual(
            _operation_stages("SMT Assembly → SMT Soldering"),
            ["SMT Assembly", "SMT Soldering"],
        )
        self.assertEqual(
            _operation_stages("SMT Assembly; SMT Soldering"),
            ["SMT Assembly", "SMT Soldering"],
        )

    def test_exact_smt_machine_and_material_compatibility_schedules_job(self):
        result = create_schedule(request())
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["schedule"][0]["job_id"], "JOB-1049")
        self.assertEqual(result["schedule"][0]["machine"], "M001")
        self.assertEqual(result["schedule"][0]["duration_minutes"], 90)

    def test_schedule_reference_time_uses_earliest_job_arrival(self):
        early_arrival = ARRIVAL - timedelta(hours=2)
        result = create_schedule(
            request(
                jobs=[
                    job(id="JOB-LATER", arrival=ARRIVAL),
                    job(id="JOB-EARLIER", arrival=early_arrival),
                ]
            )
        )
        expected_reference = early_arrival.replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30))
        ).astimezone(timezone.utc)
        self.assertEqual(
            datetime.fromisoformat(result["reference_time"]),
            expected_reference,
        )

    def test_new_job_respects_fixed_schedule_and_keeps_its_arrival_time(self):
        previous = create_schedule(request(jobs=[job()]))
        previous_row = previous["schedule"][0]
        previous_reference = datetime.fromisoformat(previous["reference_time"])
        fixed_start = previous_reference + timedelta(minutes=previous_row["start_minute"])
        fixed_end = previous_reference + timedelta(minutes=previous_row["end_minute"])
        expected_arrival = ARRIVAL.replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30))
        ).astimezone(timezone.utc)

        result = create_schedule(
            request(
                jobs=[job(id="JOB-1050")],
                fixed_schedule=[
                    FixedScheduleBlock(
                        machine=previous_row["machine"],
                        operator_id=previous_row["operator_id"],
                        start_at=fixed_start,
                        end_at=fixed_end,
                    )
                ],
            )
        )

        row = result["schedule"][0]
        reference = datetime.fromisoformat(result["reference_time"])
        self.assertGreaterEqual(
            reference + timedelta(minutes=row["start_minute"]),
            fixed_end,
        )
        self.assertEqual(
            reference + timedelta(minutes=row["arrival_minute"]),
            expected_arrival,
        )

    def test_not_before_delays_a_job_without_changing_its_arrival(self):
        not_before = ARRIVAL.replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30))
        ).astimezone(timezone.utc) + timedelta(hours=1)
        result = create_schedule(
            request(jobs=[job(id="JOB-1051", not_before=not_before)])
        )
        row = result["schedule"][0]
        reference = datetime.fromisoformat(result["reference_time"])
        expected_arrival = ARRIVAL.replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30))
        ).astimezone(timezone.utc)
        self.assertGreaterEqual(
            reference + timedelta(minutes=row["start_minute"]),
            not_before,
        )
        self.assertEqual(
            reference + timedelta(minutes=row["arrival_minute"]),
            expected_arrival,
        )

    def test_explicit_capabilities_are_authoritative_for_unrecognized_operation_and_type(self):
        arbitrary_operation = "Laser Etching"
        flexible_machine = machine(
            machine_type="Prototype Cell 7",
            operations=[arbitrary_operation],
            materials=["Titanium Sheet"],
        )
        qualified_operator = operator(skills=["Laser Etching", "Fixture Setup"])
        result = create_schedule(
            request(
                jobs=[
                    job(
                        operation=arbitrary_operation,
                        material="Titanium Sheet",
                        required_skills=["Fixture Setup"],
                    )
                ],
                machines=[flexible_machine],
                operators=[qualified_operator],
            )
        )
        self.assertEqual(result["schedule"][0]["machine"], "M001")
        self.assertEqual(result["schedule"][0]["operator_id"], "OP001")

    def test_operation_material_and_skills_match_case_and_whitespace_insensitively(self):
        compatible_machine = machine(
            operations=["  COMPONENT   PLACEMENT  "],
            materials=[" fr4 pcb ", "smd COMPONENTS"],
        )
        qualified_operator = operator(
            skills=[" smt machine operation ", "PICK AND PLACE"]
        )
        result = create_schedule(
            request(
                jobs=[job(operation=" component placement ")],
                machines=[compatible_machine],
                operators=[qualified_operator],
            )
        )
        self.assertEqual(result["schedule"][0]["machine"], "M001")

    def test_json_and_delimited_capabilities_are_parsed(self):
        compatible_machine = Machine(
            id="M001",
            name="SMT machine",
            machine_type="SMT Assembly",
            supported_operations=json.dumps(["Component Placement"]),
            supported_materials="FR4 PCB; SMD Components",
        )
        qualified_operator = Operator(
            id="OP001",
            name="Qualified operator",
            skills=json.dumps(REQUIRED_SKILLS),
        )
        result = create_schedule(
            request(
                machines=[compatible_machine],
                operators=[qualified_operator],
            )
        )
        self.assertEqual(result["schedule"][0]["machine"], "M001")

    def test_missing_and_empty_capability_fields_are_safe(self):
        compatible_machine = Machine(
            id="M001",
            name="SMT machine",
            machine_type="SMT Assembly",
            supported_operations=None,
            supported_materials=None,
        )
        self.assertEqual(compatible_machine.supported_operations, [])
        self.assertEqual(compatible_machine.supported_materials, [])
        with self.assertRaises(HTTPException) as error:
            create_schedule(request(machines=[compatible_machine]))
        self.assertIn("explicitly supports operation", error.exception.detail)

    def test_unsupported_operation_is_reported(self):
        with self.assertRaises(HTTPException) as error:
            create_schedule(
                request(machines=[machine(operations=["PCB Drilling"])])
            )
        self.assertIn("explicitly supports operation", error.exception.detail)

    def test_eligibility_errors_report_every_blocked_job(self):
        jobs = [
            job(id="JOB-OP", operation="Solder rework"),
            job(
                id="JOB-SKILL",
                required_skills=["Electrical Testing"],
            ),
        ]
        with self.assertRaises(HTTPException) as error:
            create_schedule(request(jobs=jobs))
        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("JOB-OP", error.exception.detail)
        self.assertIn("Solder rework", error.exception.detail)
        self.assertIn("JOB-SKILL", error.exception.detail)
        self.assertIn("Electrical Testing", error.exception.detail)

    def test_every_required_material_must_be_supported(self):
        with self.assertRaises(HTTPException) as error:
            create_schedule(
                request(machines=[machine(materials=["FR4 PCB"])])
            )
        self.assertIn("supports every required material", error.exception.detail)

    def test_diagnostics_include_machine_counts_and_rejection_reason(self):
        with self.assertLogs("api", level=logging.INFO) as captured:
            with self.assertRaises(HTTPException):
                create_schedule(
                    request(machines=[machine(materials=["FR4 PCB"])])
                )
        diagnostic = next(
            line for line in captured.output if "scheduling_eligibility " in line
        )
        payload = json.loads(diagnostic.split("scheduling_eligibility ", 1)[1])
        self.assertEqual(payload["job_id"], "JOB-1049")
        self.assertEqual(payload["machines_retrieved"], 1)
        self.assertEqual(payload["active_machines"], 1)
        self.assertEqual(payload["operation_compatible_machines"], 1)
        self.assertEqual(payload["material_compatible_machines"], 0)
        self.assertEqual(payload["fully_eligible_machines"], 0)
        self.assertIn("unsupported", payload["machine_rejections"]["M001"][0])

    def test_inactive_offline_and_maintenance_machines_are_not_candidates(self):
        for status in ("Inactive", " offline ", "MAINTENANCE"):
            with self.subTest(status=status):
                with self.assertRaises(HTTPException) as error:
                    create_schedule(
                        request(machines=[machine(availability=status)])
                    )
                self.assertIn("No available machines are active", error.exception.detail)

    def test_any_one_explicit_operator_skill_is_sufficient(self):
        result = create_schedule(
            request(operators=[operator(skills=["SMT Machine Operation"])])
        )
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["schedule"][0]["operator_id"], "OP001")

    def test_no_matching_explicit_operator_skills_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            create_schedule(
                request(operators=[operator(skills=["Unrelated skill"])])
            )
        self.assertIn("matches any required skill", error.exception.detail)

    def test_job_without_explicit_required_skills_can_use_any_available_operator(self):
        unqualified_by_skill_name = operator(skills=["A different skill"])
        result = create_schedule(
            request(
                jobs=[job(required_skills=[])],
                operators=[unqualified_by_skill_name],
            )
        )
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(result["schedule"][0]["operator_id"], "OP001")

    def test_job_without_required_skills_can_use_operator_with_no_skills_listed(self):
        operator_without_skills = operator(skills=[])
        result = create_schedule(
            request(
                jobs=[job(required_skills=[])],
                operators=[operator_without_skills],
            )
        )
        self.assertEqual(result["status"], "OPTIMAL")

    def test_available_alternative_machine_is_selected_when_primary_is_offline(self):
        alternate = machine(
            machine_id="M002",
            machine_type="General",
        )
        result = create_schedule(
            request(
                machines=[
                    machine(availability="Offline"),
                    alternate,
                ]
            )
        )
        self.assertEqual(result["schedule"][0]["machine"], "M002")

    def test_single_capacity_machine_does_not_overlap_jobs(self):
        jobs = [job(id="JOB-A"), job(id="JOB-B")]
        result = create_schedule(request(jobs=jobs))
        scheduled = sorted(result["schedule"], key=lambda row: row["start_minute"])
        self.assertEqual(len(scheduled), 2)
        self.assertGreaterEqual(scheduled[1]["start_minute"], scheduled[0]["end_minute"])

    def test_ordered_operations_route_across_machines_and_qualified_operators(self):
        jobs = [
            job(
                operation="SMT Assembly → SMT Soldering",
                required_skills=["SMT Operation", "Solder Operation"],
                duration_minutes=45,
            )
        ]
        machines = [
            machine(
                machine_id="PICK-PLACE",
                machine_type="General",
                operations=["SMT Assembly"],
                materials=[],
            ),
            machine(
                machine_id="REFLOW",
                machine_type="General",
                operations=["SMT Soldering"],
                materials=[],
            ),
        ]
        operators = [
            Operator(id="ASSEMBLY-OP", name="Assembly operator", skills=["SMT Operation"]),
            Operator(id="SOLDER-OP", name="Solder operator", skills=["Solder Operation"]),
        ]
        result = create_schedule(
            request(jobs=jobs, machines=machines, operators=operators)
        )

        self.assertEqual(result["status"], "OPTIMAL")
        self.assertEqual(len(result["schedule"]), 2)
        first, second = sorted(result["schedule"], key=lambda row: row["stage_index"])
        self.assertEqual(first["operation"], "SMT Assembly")
        self.assertEqual(first["machine"], "PICK-PLACE")
        self.assertEqual(second["operation"], "SMT Soldering")
        self.assertEqual(second["machine"], "REFLOW")
        operator_skills = {
            "ASSEMBLY-OP": {"smt operation"},
            "SOLDER-OP": {"solder operation"},
        }
        for stage in (first, second):
            self.assertIn(stage["operator_id"], operator_skills)
            self.assertTrue(
                operator_skills[stage["operator_id"]]
                & {
                    skill.casefold()
                    for skill in jobs[0].required_skills
                }
            )
        self.assertEqual(first["duration_minutes"], 45)
        self.assertEqual(second["duration_minutes"], 45)
        self.assertGreaterEqual(second["start_minute"], first["end_minute"])
        self.assertEqual(result["metrics"]["scheduled_task_count"], 1)
        self.assertEqual(result["metrics"]["scheduled_stage_count"], 2)

    def test_unavailable_later_route_stage_reports_operation_and_stage(self):
        with self.assertRaises(HTTPException) as error:
            create_schedule(
                request(
                    jobs=[job(operation="SMT Assembly → SMT Soldering")],
                    machines=[
                        machine(
                            machine_type="General",
                            operations=["SMT Assembly"],
                            materials=[],
                        )
                    ],
                )
            )
        self.assertIn("stage 2 (SMT Soldering)", error.exception.detail)

    def test_job_dependencies_connect_the_predecessor_final_stage(self):
        jobs = [
            job(
                id="JOB-A",
                operation="A1 → A2",
                duration_minutes=30,
                required_skills=[],
                material=None,
            ),
            job(
                id="JOB-B",
                operation="B1 → B2",
                duration_minutes=30,
                required_skills=[],
                material=None,
                dependencies=["JOB-A"],
            ),
        ]
        machines = [
            machine(machine_id=f"M-{operation}", operations=[operation])
            for operation in ("A1", "A2", "B1", "B2")
        ]
        result = create_schedule(
            request(jobs=jobs, machines=machines, operators=[operator([])])
        )
        rows = result["schedule"]
        predecessor_end = next(
            row["end_minute"]
            for row in rows
            if row["job_id"] == "JOB-A" and row["stage_index"] == 2
        )
        successor_start = next(
            row["start_minute"]
            for row in rows
            if row["job_id"] == "JOB-B" and row["stage_index"] == 1
        )
        self.assertGreaterEqual(successor_start, predecessor_end)

    def test_allow_unscheduled_does_not_return_partial_routes(self):
        result = create_schedule(
            request(
                jobs=[job(operation="SMT Assembly → SMT Soldering")],
                machines=[
                    machine(
                        machine_type="General",
                        operations=["SMT Assembly"],
                        materials=[],
                    )
                ],
                allow_unscheduled=True,
            )
        )
        self.assertEqual(result["schedule"], [])
        self.assertEqual(result["metrics"]["scheduled_task_count"], 0)
        self.assertEqual(result["metrics"]["unscheduled_task_count"], 1)
        self.assertEqual(result["unscheduled_tasks"][0]["job_id"], "JOB-1049")

    def test_baseline_schedules_each_stage_in_route_order(self):
        result = create_schedule(
            request(
                jobs=[job(operation="SMT Assembly → SMT Soldering")],
                machines=[
                    machine(machine_id="PICK-PLACE", operations=["SMT Assembly"]),
                    machine(machine_id="REFLOW", operations=["SMT Soldering"]),
                ],
                scheduler="fcfs",
            )
        )
        first, second = sorted(
            result["schedule"], key=lambda row: row["stage_index"]
        )
        self.assertEqual(first["operation"], "SMT Assembly")
        self.assertEqual(second["operation"], "SMT Soldering")
        self.assertGreaterEqual(second["start_minute"], first["end_minute"])

    def test_route_tardiness_is_counted_only_at_final_stage(self):
        result = create_schedule(
            request(
                jobs=[
                    job(
                        operation="SMT Assembly → SMT Soldering",
                        duration_minutes=45,
                        due=datetime(2030, 1, 1, 9, 30),
                        allow_late=True,
                    )
                ],
                machines=[
                    machine(machine_id="PICK-PLACE", operations=["SMT Assembly"]),
                    machine(machine_id="REFLOW", operations=["SMT Soldering"]),
                ],
            )
        )
        first, final = sorted(
            result["schedule"], key=lambda row: row["stage_index"]
        )
        self.assertEqual(first["tardiness_minutes"], 0)
        self.assertEqual(final["tardiness_minutes"], 60)
        self.assertEqual(result["metrics"]["late_task_count"], 1)
        self.assertEqual(result["metrics"]["total_tardiness"], 60)

    def test_hard_deadline_infeasibility_is_not_a_machine_error(self):
        result = create_schedule(
            request(jobs=[job(due=datetime(2030, 1, 1, 9, 30))])
        )
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertNotIn("machine", result["unscheduled_tasks"][0]["reason"].casefold())

    def test_naive_api_datetimes_are_interpreted_as_ist(self):
        task = job()
        result = create_schedule(request(jobs=[task]))
        reference = datetime.fromisoformat(result["reference_time"])
        arrival_utc = ARRIVAL.replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30))
        ).astimezone(reference.tzinfo)
        expected_arrival = int(arrival_utc.timestamp() // 60) - int(
            reference.timestamp() // 60
        )
        self.assertEqual(result["schedule"][0]["arrival_minute"], expected_arrival)
        self.assertEqual(
            result["schedule"][0]["end_minute"] - result["schedule"][0]["start_minute"],
            90,
        )

    def test_invalid_capability_field_types_are_rejected(self):
        with self.assertRaises(ValidationError):
            Machine(
                id="M001",
                name="SMT machine",
                machine_type="SMT Assembly",
                supported_operations=123,
            )


if __name__ == "__main__":
    unittest.main()
