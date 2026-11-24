def approved_late_run(client):
    run = client.post("/api/runs", json={"profile": "late"}).get_json()
    response = client.post(f"/api/runs/{run['run_id']}/review",
                           json={"decision": "APPROVED", "comment": "复核完成", "expected_revision": 0})
    assert response.status_code == 200
    return run


def submit(client, run_id, supplement):
    return client.post(f"/api/runs/{run_id}/corrections", json={"supplement": supplement})


# L1：2026-11-20 23:30 → 2026-11-21 00:30，暂停 10 分钟。
# 签发口径（合计暂停从最早分段扣除）：日费 [150, 250]，合计 400。
DAY2_PAUSE = [{"trip_id": "L1", "pauses": [{"start": "2026-11-21 00:10", "end": "2026-11-21 00:20"}]}]


def test_correction_requires_approved_run(client):
    run = client.post("/api/runs", json={"profile": "late"}).get_json()
    response = submit(client, run["run_id"], DAY2_PAUSE)
    assert response.status_code == 409
    assert response.get_json()["code"] == "NOT_APPROVED"


def test_correction_on_missing_run(client):
    response = submit(client, "missing", DAY2_PAUSE)
    assert response.status_code == 404
    assert response.get_json()["code"] == "RUN_NOT_FOUND"


def test_empty_supplement_is_rejected(client):
    run = approved_late_run(client)
    for body in ({}, {"supplement": {}}, {"supplement": []}):
        response = client.post(f"/api/runs/{run['run_id']}/corrections", json=body)
        assert response.status_code == 400
        assert response.get_json()["code"] == "BAD_REQUEST"


def test_day_shift_without_total_change_is_issued(client):
    run = approved_late_run(client)
    response = submit(client, run["run_id"], DAY2_PAUSE)
    assert response.status_code == 200
    correction = response.get_json()
    assert correction["status"] == "ISSUED"
    assert correction["replayed"] is False
    change = correction["detail"]["changes"][0]
    assert change["trip_id"] == "L1"
    assert change["before"] == {"day_fares": [150, 250], "fare": 400}
    assert change["after"] == {"day_fares": [250, 150], "fare": 400}
    assert change["before"]["fare"] == change["after"]["fare"]
    assert change["dates"] == ["2026-11-20", "2026-11-21"]
    assert len(correction["detail"]["correction_sha256"]) == 64


def test_pause_crossing_midnight_is_split_by_day(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L1", "pauses": [{"start": "2026-11-20 23:55", "end": "2026-11-21 00:05"}]}]
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "ISSUED"
    change = correction["detail"]["changes"][0]
    assert change["after"] == {"day_fares": [200, 200], "fare": 400}


def test_unchanged_supplement_is_not_issued(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L1", "pauses": [{"start": "2026-11-20 23:40", "end": "2026-11-20 23:50"}]}]
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "UNCHANGED"
    assert correction["detail"]["changes"] == []


def test_overlapping_pauses_fail_and_keep_old_version(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L1", "pauses": [
        {"start": "2026-11-21 00:05", "end": "2026-11-21 00:15"},
        {"start": "2026-11-21 00:10", "end": "2026-11-21 00:20"}]}]
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "FAILED"
    codes = {problem["code"] for problem in correction["detail"]["problems"]}
    assert "PAUSE_OVERLAP" in codes
    assert correction["detail"]["changes"] == []
    kept = client.get(f"/api/runs/{run['run_id']}").get_json()
    assert kept["status"] == "APPROVED"
    assert kept["revision"] == 1


def test_pause_total_mismatch_fails(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L1", "pauses": [{"start": "2026-11-21 00:05", "end": "2026-11-21 00:10"}]}]
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "FAILED"
    codes = {problem["code"] for problem in correction["detail"]["problems"]}
    assert codes == {"PAUSE_TOTAL_MISMATCH"}


def test_pause_out_of_trip_fails(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L1", "pauses": [{"start": "2026-11-21 01:00", "end": "2026-11-21 01:10"}]}]
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "FAILED"
    codes = {problem["code"] for problem in correction["detail"]["problems"]}
    assert codes == {"PAUSE_OUT_OF_TRIP"}


def test_bad_pause_interval_fails(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L1", "pauses": [{"start": "2026-11-21 00:20", "end": "2026-11-21 00:10"}]}]
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "FAILED"
    codes = {problem["code"] for problem in correction["detail"]["problems"]}
    assert codes == {"PAUSE_INTERVAL_BAD"}


def test_unknown_trip_fails(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L9", "pauses": []}]
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "FAILED"
    assert correction["detail"]["problems"] == [{"trip_id": "L9", "code": "TRIP_UNKNOWN"}]


def test_duplicate_trip_entry_fails(client):
    run = approved_late_run(client)
    supplement = DAY2_PAUSE + DAY2_PAUSE
    correction = submit(client, run["run_id"], supplement).get_json()
    assert correction["status"] == "FAILED"
    codes = {problem["code"] for problem in correction["detail"]["problems"]}
    assert "TRIP_DUPLICATE" in codes


def test_same_supplement_does_not_issue_twice(client):
    run = approved_late_run(client)
    first = submit(client, run["run_id"], DAY2_PAUSE).get_json()
    second = submit(client, run["run_id"], DAY2_PAUSE).get_json()
    assert second["replayed"] is True
    assert second["correction_id"] == first["correction_id"]
    listed = client.get(f"/api/runs/{run['run_id']}/corrections").get_json()["corrections"]
    assert len(listed) == 1


def test_failed_supplement_replay_does_not_add_record(client):
    run = approved_late_run(client)
    supplement = [{"trip_id": "L1", "pauses": [{"start": "2026-11-21 00:05", "end": "2026-11-21 00:10"}]}]
    first = submit(client, run["run_id"], supplement).get_json()
    second = submit(client, run["run_id"], supplement).get_json()
    assert first["status"] == second["status"] == "FAILED"
    assert second["replayed"] is True
    assert second["correction_id"] == first["correction_id"]
    listed = client.get(f"/api/runs/{run['run_id']}/corrections").get_json()["corrections"]
    assert len(listed) == 1


def test_distinct_supplements_each_issue_once(client):
    run = approved_late_run(client)
    submit(client, run["run_id"], DAY2_PAUSE)
    other = [{"trip_id": "L1", "pauses": [{"start": "2026-11-20 23:55", "end": "2026-11-21 00:05"}]}]
    submit(client, run["run_id"], other)
    listed = client.get(f"/api/runs/{run['run_id']}/corrections").get_json()["corrections"]
    assert len(listed) == 2
    assert {item["status"] for item in listed} == {"ISSUED"}


def test_issued_evidence_stays_as_is(client):
    run = approved_late_run(client)
    before = client.get(f"/api/runs/{run['run_id']}/evidence").get_json()
    submit(client, run["run_id"], DAY2_PAUSE)
    after = client.get(f"/api/runs/{run['run_id']}/evidence").get_json()
    assert before == after
    assert before["artifact_sha256"] == after["artifact_sha256"]


def test_corrections_of_missing_run_are_404(client):
    response = client.get("/api/runs/missing/corrections")
    assert response.status_code == 404
    assert response.get_json()["code"] == "RUN_NOT_FOUND"
