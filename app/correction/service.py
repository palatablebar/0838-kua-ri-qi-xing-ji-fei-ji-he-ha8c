import json
from datetime import timedelta

from app.domain.calculate import calculate_rows, day_fare
from app.domain.helpers import TS
from app.domain.prepare import prepare_rows
from app.errors import DomainIssue
from app.evidence.manifest import stable_digest
from app.intake.service import load

# 补证核对结论码：区间无法解析、区间重叠、区间落在行程之外、合计凑不齐原暂停时长、
# 补证指向批次外骑行、同一骑行在一份补证中重复出现。
STRUCTURAL_CODES = {"PAUSE_INTERVAL_BAD", "PAUSE_OUT_OF_TRIP"}


def _computed_items(profile):
    # 同一来源重复执行得到同一批次数值，因此按批次来源重算即可还原签发时的逐日结果。
    intake = load(profile)
    prepared, _ = prepare_rows(intake["rows"])
    return {item["trip_id"]: item for item in calculate_rows(prepared)}


def _pause_seconds_by_day(pauses):
    per_day = {}
    for pause in pauses:
        cursor = pause["start"]
        while cursor < pause["end"]:
            midnight = cursor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            boundary = min(midnight, pause["end"])
            day = cursor.date()
            per_day[day] = per_day.get(day, 0) + int((boundary - cursor).total_seconds())
            cursor = boundary
    return per_day


def _corrected_day_fares(item, pauses):
    # 与签发口径的唯一差别：暂停按实际发生的自然日扣除，而不是从最早分段起合计扣除。
    pause_by_day = _pause_seconds_by_day(pauses)
    start, end = item["start_at"], item["end_at"]
    fares = []
    cursor = start
    while cursor < end:
        midnight = cursor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        boundary = min(midnight, end)
        seconds = int((boundary - cursor).total_seconds())
        consumed = min(pause_by_day.get(cursor.date(), 0), seconds)
        minutes = (seconds - consumed + 59) // 60
        fares.append(day_fare(minutes))
        cursor = boundary
    return fares


def _check_entry(item, entry):
    trip_id = item["trip_id"]
    problems = []
    raw_pauses = entry.get("pauses") if isinstance(entry, dict) else None
    if not isinstance(raw_pauses, list):
        return [], [{"trip_id": trip_id, "code": "PAUSE_INTERVAL_BAD"}]
    pauses = []
    for raw in raw_pauses:
        start = end = None
        if isinstance(raw, dict):
            try:
                start = TS(raw.get("start"), "PAUSE_INTERVAL_BAD")
                end = TS(raw.get("end"), "PAUSE_INTERVAL_BAD")
            except ValueError:
                start = end = None
        if start is None or end is None or end <= start:
            problems.append({"trip_id": trip_id, "code": "PAUSE_INTERVAL_BAD"})
        elif start < item["start_at"] or end > item["end_at"]:
            problems.append({"trip_id": trip_id, "code": "PAUSE_OUT_OF_TRIP"})
        else:
            pauses.append({"start": start, "end": end})
    ordered = sorted(pauses, key=lambda pause: pause["start"])
    if any(later["start"] < earlier["end"] for earlier, later in zip(ordered, ordered[1:])):
        problems.append({"trip_id": trip_id, "code": "PAUSE_OVERLAP"})
    if not any(problem["code"] in STRUCTURAL_CODES for problem in problems):
        total_seconds = sum(int((pause["end"] - pause["start"]).total_seconds()) for pause in pauses)
        if total_seconds != item["paused_minutes"] * 60:
            problems.append({"trip_id": trip_id, "code": "PAUSE_TOTAL_MISMATCH"})
    return ordered, problems


def submit(store, run_id, supplement):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if row["status"] != "APPROVED":
        raise DomainIssue("NOT_APPROVED", 409)
    evidence_key = stable_digest(supplement)
    existing = store.find_correction(run_id, evidence_key)
    if existing is not None:
        # 重跑同一份补证不新增签发，直接返回首次记录。
        return serialize(existing, replayed=True)
    payload = json.loads(row["payload"])
    items = _computed_items(payload["profile"])
    problems, changes = [], []
    seen = set()
    for entry in supplement:
        trip_id = str(entry.get("trip_id") or "") if isinstance(entry, dict) else ""
        if not trip_id or trip_id in seen:
            code = "TRIP_DUPLICATE" if trip_id else "TRIP_UNKNOWN"
            problems.append({"trip_id": trip_id, "code": code})
            continue
        seen.add(trip_id)
        item = items.get(trip_id)
        if item is None:
            problems.append({"trip_id": trip_id, "code": "TRIP_UNKNOWN"})
            continue
        pauses, entry_problems = _check_entry(item, entry)
        problems.extend(entry_problems)
        if entry_problems:
            continue
        before = [int(part) for part in item["day_fares"]]
        after = _corrected_day_fares(item, pauses)
        if after != before:
            # 逐日比较：总车费没变、日费变了同样触发更正，否则对账漏掉跨日差额。
            first_day = item["start_at"].date()
            changes.append({
                "trip_id": trip_id,
                "dates": [(first_day + timedelta(days=index)).isoformat() for index in range(len(before))],
                "before": {"day_fares": before, "fare": sum(before)},
                "after": {"day_fares": after, "fare": sum(after)},
                "wallet_debit": int(item["wallet_debit"]),
            })
    # 核对无误才签发更正；任何一条失败都整单保留旧版。
    status = "FAILED" if problems else ("ISSUED" if changes else "UNCHANGED")
    detail = {
        "schema": "workbench-correction/v1",
        "run_id": run_id,
        "base_revision": row["revision"],
        "status": status,
        "evidence_key": evidence_key,
        "supplement": supplement,
        "changes": changes,
        "problems": problems,
    }
    detail["correction_sha256"] = stable_digest(detail)
    record, created = store.create_correction(run_id, evidence_key, status, detail)
    return serialize(record, replayed=not created)


def serialize(record, replayed=False):
    if record is None:
        return None
    return {
        "correction_id": record["id"],
        "run_id": record["run_id"],
        "status": record["status"],
        "evidence_key": record["evidence_key"],
        "created_at": record["created_at"],
        "replayed": replayed,
        "detail": json.loads(record["detail"]),
    }
