"""HTTP API and resource registry for the DynamiQ scheduler."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from src.baseline_schedulers import schedule_baseline
from src.cp_sat_scheduler import solve_schedule
from src.data_validator import DataValidationError
from src.metrics import calculate_metrics
from src.schedule_validator import validate_schedule
from src import sqlite_store

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
logger = logging.getLogger(__name__)


def _parse_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        return [item for part in value for item in _parse_values(part)]
    if not isinstance(value, str):
        raise ValueError("Expected a delimited string or a list of strings.")

    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return [item for part in parsed for item in _parse_values(part)]
    if isinstance(parsed, str) and parsed != text:
        return _parse_values(parsed)
    if not isinstance(parsed, str):
        raise ValueError("JSON capabilities must be a string or an array of strings.")

    return [part.strip() for part in re.split(r"[;,]", text) if part.strip()]


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _operation_stages(operation: str) -> list[str]:
    stages = [
        stage.strip()
        for stage in re.split(r"\s*(?:->|→|;|\r?\n)\s*", operation)
        if stage.strip()
    ]
    if not stages:
        raise ValueError("Each job must include at least one required operation.")
    return stages


class Job(BaseModel):
    id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    duration_minutes: int = Field(gt=0)
    arrival: datetime | None = None
    not_before: datetime | None = None
    due: datetime
    priority: Literal["Critical", "High", "Medium", "Low"] = "Low"
    material: str | list[str] | None = None
    required_skills: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    allow_late: bool = True

    @field_validator("required_skills", mode="before")
    @classmethod
    def parse_required_skills(cls, value: object) -> list[str]:
        if value is not None and not isinstance(value, (str, list, tuple, set)):
            raise ValueError("required_skills must be a string or a list of strings.")
        return _parse_values(value)

    @field_validator("material", mode="before")
    @classmethod
    def parse_material(cls, value: object) -> str | list[str] | None:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, list):
            return _parse_values(value)
        raise ValueError("material must be a string or a list of strings.")


class Operator(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    availability: str = "Available"
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    @field_validator("skills", "certifications", mode="before")
    @classmethod
    def parse_skills(cls, value: object) -> list[str]:
        if value is not None and not isinstance(value, (str, list, tuple, set)):
            raise ValueError("skills and certifications must be strings or lists of strings.")
        return _parse_values(value)


class Machine(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    machine_type: str = Field(min_length=1)
    availability: str = "Available"
    supported_operations: list[str] = Field(default_factory=list)
    supported_materials: list[str] = Field(default_factory=list)
    operating_cost_per_hour: float = Field(default=0, ge=0)

    @field_validator("supported_operations", "supported_materials", mode="before")
    @classmethod
    def parse_capabilities(cls, value: object) -> list[str]:
        if value is not None and not isinstance(value, (str, list, tuple, set)):
            raise ValueError("Machine capabilities must be strings or lists of strings.")
        return _parse_values(value)


class FixedScheduleBlock(BaseModel):
    machine: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)
    start_at: datetime
    end_at: datetime


class ScheduleRequest(BaseModel):
    jobs: list[Job] = Field(min_length=1)
    operators: list[Operator] = Field(default_factory=list)
    machines: list[Machine] = Field(default_factory=list)
    fixed_schedule: list[FixedScheduleBlock] = Field(default_factory=list)
    objective: Literal["balanced", "tardiness", "makespan", "cost", "workload", "fastest", "deadline"] = "balanced"
    scheduler: Literal["cp_sat", "fcfs", "sjf", "priority"] = "cp_sat"
    priority_policy: Literal["preference", "strict"] = "preference"
    allow_unscheduled: bool = False
    unscheduled_penalty: int = Field(default=1_000_000, ge=1)
    time_limit_seconds: float = Field(default=10, gt=0, le=120)
    num_workers: int = Field(default=4, ge=1, le=32)


class ResourceRecord(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    active: bool = True
    data: dict = Field(default_factory=dict)


class TaskCollection(BaseModel):
    items: list[dict] = Field(default_factory=list)


app = FastAPI(title="DynamiQ Scheduling API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=False, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type"])


@app.on_event("startup")
def initialize_database():
    try:
        sqlite_store.initialize()
    except Exception as error:
        raise RuntimeError(f"Could not initialize SQLite database. Check SQLITE_PATH and file permissions. Details: {error}") from error


def _machine_rejection_reasons(job: Job, machine: Machine, active: bool) -> list[str]:
    reasons = []
    if not active:
        reasons.append(f"machine status '{machine.availability}' is not schedulable")
    operation = _normalize(job.operation)
    supported_operations = {_normalize(item) for item in machine.supported_operations}
    if operation not in supported_operations:
        reasons.append("required operation is unsupported")

    required_materials = {_normalize(item) for item in _parse_values(job.material)}
    supported_materials = {_normalize(item) for item in machine.supported_materials}
    missing_materials = required_materials - supported_materials
    if missing_materials:
        reasons.append(
            "required material(s) unsupported: " + ", ".join(sorted(missing_materials))
        )
    return reasons


def _minute(value: datetime, origin_minute: int) -> int:
    actual = value.replace(tzinfo=IST) if value.tzinfo is None else value
    return int(actual.astimezone(UTC).timestamp() // 60) - origin_minute


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=IST).astimezone(UTC) if value.tzinfo is None else value.astimezone(UTC)


def _dependency_map(jobs):
    known = {job.id for job in jobs}
    deps = {job.id: list(dict.fromkeys(job.dependencies)) for job in jobs}
    for task_id, predecessors in deps.items():
        if task_id in predecessors: raise HTTPException(status_code=422, detail=f"Task {task_id} cannot depend on itself.")
        missing = set(predecessors) - known
        if missing: raise HTTPException(status_code=422, detail=f"Task {task_id} references unknown predecessor(s): {', '.join(sorted(missing))}.")
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting: raise HTTPException(status_code=422, detail="Task dependencies contain a cycle.")
        if node in visited: return
        visiting.add(node)
        for prior in deps[node]: visit(prior)
        visiting.remove(node); visited.add(node)
    for node in deps: visit(node)
    return deps


@app.get("/api/health")
def health():
    try:
        sqlite_store.check_connection()
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"SQLite database check failed: {error}") from error
    return {"status": "ok", "solver": "OR-Tools CP-SAT", "database": "SQLite"}


@app.get("/api/resources")
def list_resources():
    rows = sqlite_store.get_resources()
    return {kind: [{"id": row["id"], "name": row["name"], "active": row["active"], "data": row["data"]} for row in rows if row["kind"] == kind] for kind in ("machines", "operators")}


@app.get("/api/resources/{kind}")
def list_resource_kind(kind: Literal["machines", "operators"], include_inactive: bool = False):
    rows = sqlite_store.get_resources(kind)
    return [{"id": row["id"], "name": row["name"], "active": row["active"], "data": row["data"]} for row in rows if include_inactive or row["active"]]


@app.post("/api/resources/{kind}", status_code=201)
def create_resource(kind: Literal["machines", "operators"], resource: ResourceRecord):
    row = resource.model_dump()
    if not sqlite_store.save_resource(kind, row, create_only=True): raise HTTPException(status_code=409, detail=f"{kind[:-1].capitalize()} ID already exists.")
    return row


@app.put("/api/resources/{kind}/{resource_id}")
def update_resource(kind: Literal["machines", "operators"], resource_id: str, resource: ResourceRecord):
    if resource.id != resource_id: raise HTTPException(status_code=422, detail="Resource ID cannot be changed.")
    row = resource.model_dump()
    if not sqlite_store.save_resource(kind, row): raise HTTPException(status_code=404, detail="Resource not found.")
    return row


@app.patch("/api/resources/{kind}/{resource_id}/deactivate")
def deactivate_resource(kind: Literal["machines", "operators"], resource_id: str):
    if not sqlite_store.set_resource_active(kind, resource_id, False): raise HTTPException(status_code=404, detail="Resource not found.")
    return next(row for row in sqlite_store.get_resources(kind) if row["id"] == resource_id)


@app.delete("/api/resources/{kind}/{resource_id}")
def delete_resource(kind: Literal["machines", "operators"], resource_id: str):
    if not sqlite_store.remove_resource(kind, resource_id): raise HTTPException(status_code=404, detail="Resource not found.")
    return {"deleted": resource_id}


@app.get("/api/tasks")
def list_tasks(): return sqlite_store.get_rows("tasks")


@app.put("/api/tasks")
def replace_tasks(collection: TaskCollection):
    try: sqlite_store.replace_rows("tasks", "Job ID", collection.items)
    except ValueError as error: raise HTTPException(status_code=422, detail=str(error)) from error
    return {"saved": len(collection.items)}


@app.get("/api/archive")
def list_archived_tasks(): return sqlite_store.get_rows("archived_tasks")


@app.put("/api/archive")
def replace_archived_tasks(collection: TaskCollection):
    try: sqlite_store.replace_rows("archived_tasks", "Job ID", collection.items)
    except ValueError as error: raise HTTPException(status_code=422, detail=str(error)) from error
    return {"saved": len(collection.items)}


@app.get("/api/schedule/latest")
def latest_schedule(): return sqlite_store.get_latest_schedule()


@app.put("/api/schedule/latest")
def save_schedule_state(data: dict):
    sqlite_store.save_latest_schedule(data)
    return {"saved": True}


@app.post("/api/schedule")
def create_schedule(request: ScheduleRequest):
    ids = [job.id.strip() for job in request.jobs]
    if any(not item for item in ids) or len({item.casefold() for item in ids}) != len(ids): raise HTTPException(status_code=422, detail="Job IDs must be non-empty and unique.")
    dependencies = _dependency_map(request.jobs)
    machines_all, operators_all = request.machines, request.operators
    machine_ids = [item.id.strip() for item in machines_all]; operator_ids = [item.id.strip() for item in operators_all]
    if len({item.casefold() for item in machine_ids}) != len(machine_ids): raise HTTPException(status_code=422, detail="Machine IDs must be unique.")
    if len({item.casefold() for item in operator_ids}) != len(operator_ids): raise HTTPException(status_code=422, detail="Operator IDs must be unique.")
    machines = [item for item in machines_all if _normalize(item.availability) in {"available", "active"}]
    operators = [item for item in operators_all if _normalize(item.availability) in {"available", "active"}]
    now = datetime.now(UTC)
    for block in request.fixed_schedule:
        if _utc(block.end_at) <= _utc(block.start_at):
            raise HTTPException(status_code=422, detail="Fixed schedule blocks must end after they start.")
    due_dates = [_utc(job.due) for job in request.jobs]
    arrivals = [_utc(job.arrival) if job.arrival else now for job in request.jobs]
    fixed_starts = [_utc(block.start_at) for block in request.fixed_schedule]
    origin = min([*arrivals, *fixed_starts]); origin_minute = int(origin.timestamp() // 60)
    task_rows, eligibility_rows, diagnostic = [], [], {}
    stage_metadata: dict[str, dict] = {}
    stage_ids_by_job: dict[str, list[str]] = {}
    eligibility_failures = []
    priority_rank = {"Critical": 1, "High": 2, "Medium": 3, "Low": 4}
    for sequence, (job, arrival, due) in enumerate(zip(request.jobs, arrivals, due_dates)):
        try:
            operations = _operation_stages(job.operation)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"Job {job.id}: {error}") from error

        stage_ids = [
            f"__route_stage_{sequence}_{stage_index}"
            for stage_index in range(len(operations))
        ]
        if set(stage_ids).intersection(ids):
            raise HTTPException(
                status_code=422,
                detail="A Job ID conflicts with an internal multi-stage route ID.",
            )
        stage_ids_by_job[job.id] = stage_ids
        effective_arrival = max(
            arrival,
            now,
            _utc(job.not_before) if job.not_before else arrival,
        )
        for stage_index, (stage_id, operation) in enumerate(zip(stage_ids, operations)):
            is_final_stage = stage_index == len(operations) - 1
            stage_metadata[stage_id] = {
                "job": job,
                "stage_index": stage_index,
                "stage_count": len(operations),
                "operation": operation,
                "final_stage": is_final_stage,
            }
            task_rows.append({
                "task_id": stage_id,
                "duration": job.duration_minutes,
                "arrival": max(0, int(effective_arrival.timestamp() // 60) - origin_minute) if stage_index == 0 else 0,
                "due": max(0, int(due.timestamp() // 60) - origin_minute),
                "priority": priority_rank[job.priority],
                "sequence": sequence * 1000 + stage_index,
                "hard_due": not job.allow_late if is_final_stage else False,
                "tardiness_enabled": is_final_stage,
                "route_group": job.id,
            })

            stage_job = job.model_copy(update={"operation": operation})
            compatible_machines = []
            machine_rejections = {}
            operation_compatible_count = 0
            material_compatible_count = 0
            for machine in machines_all:
                machine_active = _normalize(machine.availability) in {"available", "active"}
                reasons = _machine_rejection_reasons(stage_job, machine, machine_active)
                operation_reason = "required operation is unsupported" in reasons
                material_reason = any(
                    reason.startswith("required material(s) unsupported:")
                    for reason in reasons
                )
                if machine_active and not operation_reason:
                    operation_compatible_count += 1
                if machine_active and not material_reason:
                    material_compatible_count += 1
                if reasons:
                    machine_rejections[machine.id] = reasons
                else:
                    compatible_machines.append(machine)

            operation_compatible_machines = [
                machine for machine in machines
                if _normalize(operation)
                in {_normalize(item) for item in machine.supported_operations}
            ]
            material_compatible_machines = [
                machine for machine in operation_compatible_machines
                if not (
                    {_normalize(item) for item in _parse_values(job.material)}
                    - {_normalize(item) for item in machine.supported_materials}
                )
            ]
            required_skills = {
                _normalize(skill) for skill in _parse_values(job.required_skills)
            }
            eligible_operators = []
            for operator in operators:
                skills = {
                    _normalize(item)
                    for item in [*operator.skills, *operator.certifications]
                }
                if not required_skills or required_skills.intersection(skills):
                    eligible_operators.append(operator)

            for machine in compatible_machines:
                for operator in eligible_operators:
                    eligibility_rows.append({
                        "task_id": stage_id,
                        "operator": operator.id,
                        "resource": machine.id,
                        "process_time": job.duration_minutes,
                        "eligible": True,
                        "cost": int(round(job.duration_minutes * machine.operating_cost_per_hour / 60)),
                        "setup_time": 0,
                        "operation": operation,
                    })

            logger.info(
                "scheduling_eligibility %s",
                json.dumps(
                    {
                        "job_id": job.id,
                        "stage": stage_index + 1,
                        "stage_count": len(operations),
                        "required_operation": operation,
                        "required_materials": _parse_values(job.material),
                        "machines_retrieved": len(machines_all),
                        "active_machines": len(machines),
                        "operation_compatible_machines": operation_compatible_count,
                        "material_compatible_machines": material_compatible_count,
                        "fully_eligible_machines": len(compatible_machines),
                        "machine_rejections": machine_rejections,
                    },
                    sort_keys=True,
                ),
            )
            if not any(row["task_id"] == stage_id for row in eligibility_rows):
                if not machines:
                    reason = "No available machines are active."
                elif not operation_compatible_machines:
                    reason = f"No available machine explicitly supports operation '{operation}'."
                elif not material_compatible_machines:
                    reason = f"No available machine supports every required material for operation '{operation}'."
                elif not eligible_operators:
                    required_labels = _parse_values(job.required_skills)
                    reason = (
                        "No available operator matches any required skill: "
                        + ", ".join(required_labels)
                        if required_labels
                        else "No available operator is eligible."
                    )
                else:
                    reason = "No eligible machine/operator assignment was generated."
                diagnostic[stage_id] = reason
                if request.allow_unscheduled:
                    eligibility_rows.append({
                        "task_id": stage_id,
                        "operator": f"__unavailable_operator_{stage_id}",
                        "resource": f"__unavailable_machine_{stage_id}",
                        "process_time": job.duration_minutes,
                        "eligible": False,
                        "cost": 0,
                        "setup_time": 0,
                        "operation": operation,
                    })
                else:
                    eligibility_failures.append(
                        f"Job {job.id}, stage {stage_index + 1} ({operation}): {reason}"
                    )

    if eligibility_failures:
        raise HTTPException(
            status_code=422,
            detail="Scheduling eligibility failed: " + " ".join(eligibility_failures),
        )

    stage_dependencies = {
        stage_id: [stage_ids[index - 1]]
        for stage_ids in stage_ids_by_job.values()
        for index, stage_id in enumerate(stage_ids)
        if index > 0
    }
    for successor, predecessors in dependencies.items():
        first_stage = stage_ids_by_job[successor][0]
        stage_dependencies.setdefault(first_stage, []).extend(
            stage_ids_by_job[predecessor][-1] for predecessor in predecessors
        )

    unavailable = {"resource": {}, "operator": {}}
    for block in request.fixed_schedule:
        start = int(_utc(block.start_at).timestamp() // 60) - origin_minute
        end = int(_utc(block.end_at).timestamp() // 60) - origin_minute
        unavailable["resource"].setdefault(block.machine, []).append((start, end))
        unavailable["operator"].setdefault(block.operator_id, []).append((start, end))
    tf, ef = pd.DataFrame(task_rows), pd.DataFrame(eligibility_rows)
    objective = {"balanced":"weighted", "tardiness":"total_tardiness", "makespan":"makespan", "fastest":"fastest", "deadline":"deadline", "cost":"cost", "workload":"workload"}[request.objective]
    selected_name = {"cp_sat":"CP-SAT", "fcfs":"FCFS", "sjf":"SJF", "priority":"Priority"}[request.scheduler]
    solver_details={}
    try:
        if request.scheduler == "cp_sat":
            result = solve_schedule(tf, ef, time_limit_seconds=request.time_limit_seconds, num_workers=request.num_workers, objective=objective, unavailable=unavailable, priority_policy=request.priority_policy, allow_unscheduled=request.allow_unscheduled, unscheduled_penalty=request.unscheduled_penalty, dependencies=stage_dependencies)
            status, selected, metrics, timings = result.status, result.schedule, result.metrics, result.timings
            solver_details={key:metrics[key] for key in ("best_bound","optimality_proven","timeout_reached","objective_value") if key in metrics}
            if result.validation_errors: raise HTTPException(status_code=500, detail={"message":"Independent schedule validation rejected solver output.","errors":result.validation_errors})
        else:
            if request.allow_unscheduled: raise HTTPException(status_code=422, detail="Unscheduled-task penalties are supported by CP-SAT only.")
            selected = schedule_baseline(tf, ef, {"fcfs":"fcfs","sjf":"spt","priority":"priority"}[request.scheduler], unavailable=unavailable, dependencies=stage_dependencies); status="FEASIBLE"; timings={}
            errors=validate_schedule(selected,tf,ef,dependencies=stage_dependencies,unavailable=unavailable)
            if errors: raise HTTPException(status_code=500, detail={"message":"Independent schedule validation rejected baseline output.","errors":errors})
            metrics=calculate_metrics(selected,status)
            metrics["optimality_proven"]=False
            metrics["scheduler_kind"]="HEURISTIC"
    except (DataValidationError, ValueError) as error: raise HTTPException(status_code=422, detail=str(error)) from error
    if status == "INFEASIBLE":
        unscheduled_rows = []
        for job in request.jobs:
            failed_stage = next(
                (stage_id for stage_id in stage_ids_by_job[job.id] if stage_id in diagnostic),
                None,
            )
            unscheduled_rows.append({
                "job_id": job.id,
                "reason": diagnostic.get(
                    failed_stage,
                    "No feasible schedule satisfies dependency, due-date, and resource constraints.",
                ),
            })
        return {"status":status,"scheduler":selected_name,"reference_time":origin.isoformat(),"schedule":[],"metrics":{"solver_status":status,"scheduled_task_count":0,"unscheduled_task_count":len(request.jobs)},"unscheduled_tasks":unscheduled_rows,"timings":timings,"validation":{"valid":True,"errors":[]}}
    op_names={item.id:item.name for item in operators}
    records=selected.to_dict(orient="records")
    rows=[]
    for row in records:
        metadata=stage_metadata[row["task_id"]]
        job=metadata["job"]
        rows.append({"schedule_id":row["task_id"],"job_id":job.id,"task":job.task,"stage_index":metadata["stage_index"]+1,"stage_count":metadata["stage_count"],"machine":row["resource"],"operator_id":row["operator"],"operator":op_names.get(row["operator"],row["operator"]),"start_minute":int(row["start"]),"end_minute":int(row["end"]),"duration_minutes":int(row["duration"]),"arrival_minute":_minute(job.arrival or now,origin_minute),"due_minute":_minute(job.due,origin_minute),"tardiness_minutes":int(row["tardiness"]),"status":row["status"],"priority":job.priority,"operation":metadata["operation"]})
    scheduled_stage_ids={row["task_id"] for row in records}
    unscheduled_jobs=[
        job for job in request.jobs
        if any(stage_id not in scheduled_stage_ids for stage_id in stage_ids_by_job[job.id])
    ]
    unresolved = status == "UNKNOWN"
    unscheduled=[]
    for job in unscheduled_jobs:
        failed_stage=next(
            (stage_id for stage_id in stage_ids_by_job[job.id] if stage_id in diagnostic),
            None,
        )
        unscheduled.append({
            "job_id":job.id,
            "reason":diagnostic.get(
                failed_stage,
                "Solver ended before finding a feasible assignment."
                if unresolved
                else "Could not fit all operation stages within dependencies, due dates, and resource constraints.",
            ),
        })
    actual_horizon=max((int(row["end"]) for row in records),default=1)
    actual_available={"resource":{m.id:max(1,actual_horizon) for m in machines},"operator":{o.id:max(1,actual_horizon) for o in operators}}
    metrics=calculate_metrics(selected,status,available_minutes=actual_available,resource_ids=[m.id for m in machines],operator_ids=[o.id for o in operators])
    metrics.update(solver_details)
    final_stage_ids={
        stage_ids[-1] for stage_ids in stage_ids_by_job.values()
    }
    final_stage_rows=selected[selected.task_id.isin(final_stage_ids)]
    late_count=int(final_stage_rows.tardiness.gt(0).sum()) if "tardiness" in final_stage_rows else 0
    scheduled_job_count=len(request.jobs)-len(unscheduled_jobs)
    workload={str(key):int(group.duration.sum()) for key,group in selected.groupby("operator")} if "operator" in selected else {}
    metrics.update({"scheduled_task_count":scheduled_job_count,"scheduled_stage_count":len(rows),"unscheduled_task_count":len(unscheduled),"unscheduled_task_ids":[x["job_id"] for x in unscheduled],"late_task_count":late_count,"total_tardiness":int(final_stage_rows.tardiness.sum()) if "tardiness" in final_stage_rows else 0,"deadline_performance_percent":round(100*(scheduled_job_count-late_count)/max(1,len(request.jobs)),1),"estimated_operating_cost":int(selected.get("estimated_cost",pd.Series(dtype="int64")).sum()),"operator_workload_minutes":workload})
    return {"status":status,"scheduler":selected_name,"reference_time":origin.isoformat(),"schedule":rows,"metrics":metrics,"unscheduled_tasks":unscheduled,"timings":timings,"validation":{"valid":True,"errors":[]}}
