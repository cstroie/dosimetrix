"""API behaviour on a small committed SQLite database."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from dozimetrix import classify
from dozimetrix.api.app import create_app
from dozimetrix.config import Settings
from dozimetrix.db.migrate import upgrade
from dozimetrix.db.models import AuditLog, Device, Event, Patient, Study, User
from dozimetrix.db.session import make_engine, make_sessionmaker
from dozimetrix.security import hash_password
from dozimetrix.seeds.loader import load_all

SECRET = b"s" * 32
NOW = dt.datetime(2026, 11, 21, 10, 0)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('api') / 'api.sqlite3'}"
    upgrade(url)
    engine = make_engine(url)
    with make_sessionmaker(engine)() as s:
        s.add_all([
            User(username="viewer", password_hash=hash_password("viewer-pass-1"), roles="viewer"),
            User(username="phys", password_hash=hash_password("phys-pass-12"), roles="viewer,pid"),
            User(username="boss", password_hash=hash_password("boss-pass-123"),
                 roles="viewer,admin"),
            User(username="regist", password_hash=hash_password("regist-pass-12"),
                 roles="viewer,exporter,pid"),
        ])
        dev = Device(display_name="Angio Suite", model_name="Azurion", identity_key="k1",
                     is_local=True, n_studies=1, last_study_at=NOW - dt.timedelta(days=5))
        pat = Patient(patient_id="2041187", name="POPESCU^ION", pseudo_id="P-ABCDE-12345",
                      birth_date=dt.date(1959, 3, 1), sex="M")
        s.add_all([dev, pat])
        s.flush()
        st = Study(study_uid="1.2.3", accession="A1", modality="XA", device_id=dev.id,
                   patient_id=pat.id, study_datetime=NOW - dt.timedelta(days=5),
                   study_description="Coronary angiography", age_days=24500, weight_kg=88,
                   total_dap=12.5, total_rp_dose=0.42, n_events=1, source_kind="test")
        st.events.append(Event(event_uid="e1", event_type="fluoro", rp_dose=0.123,
                               dap=1.5, field_w_mm=200, sid_mm=1050, fluoro_time_s=30,
                               event_datetime=NOW - dt.timedelta(days=5)))
        s.add(st)
        load_all(s)
        rx = Study(study_uid="1.2.4", accession="A2", modality="DX", device_id=dev.id,
                   study_datetime=NOW - dt.timedelta(days=2), study_description="Unknown",
                   n_events=2, source_kind="test")
        rx.events = [Event(protocol="XRAY-UNKNOWN", anatomy="Other", dap=0.2),
                     Event(protocol="W035 Torace a.p. 10kg", view="antero-posterior", dap=0.05)]
        s.add(rx)
        s.add(Study(study_uid="1.2.5", accession="A3", modality="MG", device_id=dev.id,
                    study_datetime=NOW - dt.timedelta(days=1), study_description="Mamografie",
                    source_kind="test"))
        s.flush()
        classify.run(s)
        s.commit()
    app = create_app(Settings(database_url=url), engine=engine, secret=SECRET, now=NOW)
    return TestClient(app)


def login(client, user, pw):
    client.cookies.clear()
    r = client.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


def test_requires_login(client):
    client.cookies.clear()
    assert client.get("/api/studies").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/api/login", json={"username": "viewer", "password": "x"}
                       ).status_code == 401


def test_cookie_flags(client):
    r = login(client, "viewer", "viewer-pass-1")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie  # plain HTTP on the LAN still logs in


def test_cookie_is_secure_over_https(client):
    https = TestClient(client.app, base_url="https://testserver")
    r = https.post("/api/login", json={"username": "viewer", "password": "viewer-pass-1"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()


def test_studies_carry_pseudonyms_only(client):
    login(client, "viewer", "viewer-pass-1")
    body = client.get("/api/studies", params={"q": "modality:XA"}).text
    assert "P-ABCDE-12345" in body
    assert "POPESCU" not in body and "2041187" not in body and "1959" not in body
    res = client.get("/api/studies", params={"q": "pid:P-ABCDE-12345"}).json()
    assert res["total"] == 1 and res["rows"][0]["patient"]["age"] == 67


def test_xa_rows_carry_fluoroscopy_columns(client):
    login(client, "viewer", "viewer-pass-1")
    row = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]
    assert row["group"] == "XA"
    assert {"fluoro_time", "kar_total", "psd", "dap_total", "n_events"} <= row.keys()
    assert row["kar_total"] is not None
    srt = client.get("/api/studies", params={"q": "modality:XA", "sort": "kar_total"})
    assert srt.status_code == 200


def test_workspaces_are_kept_per_user(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/me/prefs/workspace.Audit").json()["value"] is None
    ws = {"name": "Audit", "q": "date:90d", "panels": [{"type": "dots", "group": "DX"}]}
    assert client.put("/api/me/prefs/workspace.Audit", json={"value": ws}).status_code == 200
    assert client.get("/api/me/prefs/workspace.Audit").json()["value"] == ws
    assert client.get("/api/me/prefs/theme").status_code == 400
    login(client, "phys", "phys-pass-12")  # another user does not see it
    assert client.get("/api/me/prefs/workspace.Audit").json()["value"] is None


def test_study_queries_are_kept_per_user(client):
    login(client, "viewer", "viewer-pass-1")
    saved = [{"name": "CT kids", "q": "modality:CT age:<18"}]
    assert client.put("/api/me/prefs/studies.saved", json={"value": saved}).status_code == 200
    assert client.get("/api/me/prefs/studies.saved").json()["value"] == saved
    assert client.get("/api/me/prefs/studies.other").status_code == 400


def test_shared_views_admin_writes_everyone_reads(client):
    login(client, "viewer", "viewer-pass-1")
    body = {"kind": "study_query", "name": "CT kids", "value": {"q": "modality:CT age:<18"}}
    assert client.post("/api/views", json=body).status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/views", json={**body, "kind": "theme"}).status_code == 400
    assert client.post("/api/views", json={**body, "name": " "}).status_code == 400
    big = {**body, "value": {"q": "x" * 300_000}}
    assert client.post("/api/views", json=big).status_code == 413
    a = client.post("/api/views", json=body).json()
    assert client.post("/api/views", json=body).status_code == 409
    b = client.post("/api/views",
                    json={**body, "name": "DX all", "value": {"q": "modality:DX"}}).json()
    ws = client.post("/api/views", json={"kind": "workspace", "name": "Audit CT",
                                         "value": {"q": "date:90d", "panels": []}}).json()
    assert client.put(f"/api/views/{b['id']}", json={"name": "CT kids"}).status_code == 409
    renamed = client.put(f"/api/views/{b['id']}", json={"name": "DX, all"}).json()
    assert renamed["name"] == "DX, all"
    order = {"kind": "study_query", "ids": [b["id"]]}
    assert client.post("/api/views/order", json=order).status_code == 400
    order["ids"].append(a["id"])
    assert client.post("/api/views/order", json=order).status_code == 200
    login(client, "viewer", "viewer-pass-1")
    views = client.get("/api/views", params={"kind": "study_query"}).json()["views"]
    names = [v["name"] for v in views]
    assert names == ["DX, all", "CT kids"]
    assert [v["name"] for v in client.get("/api/views").json()["views"]].count("Audit CT") == 1
    assert client.put(f"/api/views/{a['id']}", json={"name": "x"}).status_code == 403
    assert client.delete(f"/api/views/{a['id']}").status_code == 403
    login(client, "boss", "boss-pass-123")
    for v in (a, b, ws):
        assert client.delete(f"/api/views/{v['id']}").status_code == 200
    assert client.delete(f"/api/views/{a['id']}").status_code == 404
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        actions = {x.action for x in s.scalars(select(AuditLog))}
    assert {"view.create", "view.update", "view.order", "view.delete"} <= actions


def test_page_defaults(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/page-defaults").json() == {"drl": {}, "quality": {}, "alerts": {}}
    assert client.put("/api/page-defaults/drl", json={"q": "date:30d"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.put("/api/page-defaults/nope", json={}).status_code == 404
    assert client.put("/api/page-defaults/drl", json={"theme": "x"}).status_code == 400
    assert client.put("/api/page-defaults/drl", json={"minN": "20"}).status_code == 400
    r = client.put("/api/page-defaults/drl", json={"q": "date:30d", "minN": 10, "by": ""})
    assert r.json()["value"] == {"q": "date:30d", "minN": 10}
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/page-defaults").json()["drl"] == {"q": "date:30d", "minN": 10}


def test_modality_profiles(client):
    login(client, "viewer", "viewer-pass-1")
    profs = {p["code"]: p for p in client.get("/api/modalities").json()}
    assert profs["DX"]["modalities"] == ["DX", "CR"] and "dlp_total" in profs["CT"]["columns"]
    assert "agd_left" in profs["MG"]["columns"] and profs["NM"]["main"] == "activity"


def test_detail_units(client):
    login(client, "viewer", "viewer-pass-1")
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    d = client.get(f"/api/studies/{sid}").json()
    ev = d["events"][0]
    assert ev["kar_mgy"] == 123.0 and ev["field_cm"] == 20.0 and ev["sid_cm"] == 105.0
    assert d["thresholds"]["source"] == "default"
    assert "identified" not in d["patient"] and "POPESCU" not in str(d)


def test_identify_needs_pid_role_and_is_audited(client):
    login(client, "viewer", "viewer-pass-1")
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    assert client.post(f"/api/studies/{sid}/identify").status_code == 403
    login(client, "phys", "phys-pass-12")
    r = client.post(f"/api/studies/{sid}/identify").json()
    assert r["name"] == "POPESCU^ION" and r["id"] == "2041187"


def test_audit_entry_written(client, tmp_path_factory):
    login(client, "phys", "phys-pass-12")
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    client.post(f"/api/studies/{sid}/identify")
    # read the audit log through a fresh engine on the same file
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        actions = [a.action for a in s.scalars(select(AuditLog))]
    assert "identify" in actions and "login" in actions and "login_failed" in actions


def test_overview_and_pages(client):
    login(client, "viewer", "viewer-pass-1")
    ov = client.get("/api/overview").json()
    # tiles follow the modalities the local devices produce: the fixture has no CT, so no CT tile
    assert [m["code"] for m in ov["modalities"]] == ["DX", "XA", "MG"]
    assert ov["feeds"][0]["status"] == "stale"  # last study 5 days ago
    page = client.get("/studies.html")
    assert page.status_code == 200 and 'data-api="http://testserver"' in page.text
    css = client.get("/assets/css/tokens.css")
    assert css.status_code == 200 and css.headers["cache-control"] == "no-cache"  # revalidated
    assert client.get("/api/lookup", params={"q": "A1"}).json() == []  # min 3 chars
    assert client.get("/api/lookup", params={"q": "P-ABC"}).json() == []  # too short
    assert client.get("/api/lookup", params={"q": "P-ABCDE"}).json()[0]["accession"] == "A1"


def _add_device_with_study(client, name, serial, uid, modality="DX"):
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        d = Device(display_name=name, model_name="Ysio", manufacturer="SIEMENS",
                   serial_number=serial, identity_key=f"k-{uid}", is_local=True, n_studies=1)
        s.add(d)
        s.flush()
        s.add(Study(study_uid=uid, accession=f"ACC{uid}", modality=modality, device_id=d.id,
                    study_datetime=NOW, source_kind="test", n_events=0))
        s.commit()
        return d.id


def test_device_writes_need_admin(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.patch("/api/devices/1", json={"origin": "external"}).status_code == 403
    assert client.post("/api/devices/merge", json={"record_ids": [1, 2], "survivor_id": 1}
                       ).status_code == 403


def test_merge_rename_split_dismiss_and_override(client):
    a = _add_device_with_study(client, "Ysio UPU", "25", "7.7.1")
    b = _add_device_with_study(client, "Ysio UPU", "25", "7.7.2")
    login(client, "boss", "boss-pass-123")
    sugg = client.get("/api/devices").json()["merge_suggestions"]
    assert any(set(x["record_ids"]) == {a, b} for x in sugg)

    r = client.post("/api/devices/merge", json={"record_ids": [a, b], "survivor_id": a,
                                                "display_name": "Ysio – Emergency"})
    assert r.status_code == 200 and r.json()["merged"] == [b]
    recs = {x["id"]: x for x in client.get("/api/devices").json()["records"]}
    assert recs[b]["physical_id"] == a
    rows = client.get("/api/studies", params={"q": "acc:ACC7.7.2"}).json()["rows"]
    assert rows[0]["device_name"] == "Ysio – Emergency" and rows[0]["device_id"] == a

    assert client.patch(f"/api/devices/{b}", json={"display_name": "Renamed"}).status_code == 200
    assert {x["id"]: x for x in client.get("/api/devices").json()["records"]}[a][
        "display_name"] == "Renamed"  # alias edits go to the physical device

    assert client.post(f"/api/devices/{b}/split").status_code == 200
    recs = {x["id"]: x for x in client.get("/api/devices").json()["records"]}
    assert recs[b]["physical_id"] == b
    assert client.post(f"/api/devices/{b}/split").status_code == 400  # not merged now

    client.post("/api/devices/merge-suggestions/dismiss", json={"record_ids": [b, a]})
    sugg = client.get("/api/devices").json()["merge_suggestions"]
    assert not any(set(x["record_ids"]) == {a, b} for x in sugg)

    assert client.get("/api/studies", params={"q": "acc:ACC7.7.2 modality:RF"}
                      ).json()["total"] == 0
    client.patch(f"/api/devices/{b}", json={"modality_override": "RF"})
    res = client.get("/api/studies", params={"q": "acc:ACC7.7.2 modality:RF"}).json()
    assert res["total"] == 1 and res["rows"][0]["modality"] == "RF"
    client.patch(f"/api/devices/{b}", json={"modality_override": "auto"})
    assert client.get("/api/studies", params={"q": "acc:ACC7.7.2 modality:DX"}
                      ).json()["total"] == 1

    assert client.patch(f"/api/devices/{b}", json={"origin": "elsewhere"}).status_code == 400
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        actions = {x.action for x in s.scalars(select(AuditLog))}
    assert {"device.merge", "device.update", "device.split",
            "device.merge_dismissed"} <= actions


def test_classification_review(client):
    login(client, "viewer", "viewer-pass-1")
    summary = {r["family"]: r for r in client.get("/api/classification/summary").json()}
    assert summary["rx"]["studies"] >= 1 and summary["rx"]["classified"] >= 1
    todo = client.get("/api/classification/unclassified?family=rx").json()
    spine = [x for x in todo if x["texts"]["protocol"] == "XRAY-UNKNOWN"]
    assert spine and spine[0]["exposures"] == 1
    codes = {c["code"] for c in client.get("/api/codes/ro-nom-rx").json()}
    assert "RG-CVLM" in codes
    body = {"family": "rx", "field": "protocol", "text": "XRAY-UNKNOWN", "code": "RG-CVLM"}
    assert client.put("/api/classification/mappings", json=body).status_code == 403

    login(client, "boss", "boss-pass-123")
    bad = client.put("/api/classification/mappings", json={**body, "code": "CT-CR"})
    assert bad.status_code == 400 and "not a code" in bad.json()["error"]
    r = client.put("/api/classification/mappings", json=body).json()
    assert r["studies_reclassified"] == 1
    left = client.get("/api/classification/unclassified?family=rx").json()
    assert not [x for x in left if x["texts"]["protocol"] == "XRAY-UNKNOWN"]
    m = client.get("/api/classification/mappings").json()
    assert [(x["text"], x["code"]) for x in m] == [("xray unknown", "RG-CVLM")]


def test_stats_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/stats", params={"q": "modality:DX", "metric": "dap",
                                         "by": "exam", "system": "ro-drl-ms838-2025"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "Gy.cm2" and body["level"] == "event"
    g = {x["key"]["exam"]: x for x in body["groups"]}
    assert g["RX-TORACE-PA-AP"]["median"] == 0.05
    bad = client.get("/api/stats", params={"metric": "nope"})
    assert bad.status_code == 400 and "unknown metric" in bad.json()["error"]


def test_drl_endpoints(client):
    login(client, "viewer", "viewer-pass-1")
    sets = {s["key"] for s in client.get("/api/drl/sets").json()}
    assert {"ro-ms838-2025", "ec-rp185-2018"} <= sets
    r = client.get("/api/drl/compare", params={"q": "", "min_n": 1}).json()
    assert r["set"] == "ro-ms838-2025" and r["by"] == "facility"
    assert client.get("/api/drl/compare", params={"set": "x"}).status_code == 400
    one = client.get("/api/drl/sets/ec-rp185-2018").json()
    assert one["key"] == "ec-rp185-2018" and len(one["values"]) > 0
    v = one["values"][0]
    assert {"exam", "exam_meaning", "band_label", "quantity", "value", "unit"} <= v.keys()
    assert client.get("/api/drl/sets/nope").status_code == 404


def test_export_roles_and_formats(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/export").status_code == 403  # needs exporter (or admin)
    login(client, "boss", "boss-pass-123")
    assert client.get("/api/export", params={"pid": "true"}).status_code == 403  # needs pid
    r = client.get("/api/export", params={"q": "modality:XA"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿Study ID,") and "P-ABCDE-12345" in text
    assert "POPESCU" not in text and len(text.strip().splitlines()) == 2
    x = client.get("/api/export", params={"q": "modality:XA", "format": "xlsx",
                                          "level": "event"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    with make_sessionmaker(make_engine(str(client.app.state.settings.database_url)))() as s:
        acts = [a.target for a in s.scalars(select(AuditLog).filter_by(action="export"))]
    assert "study:csv" in acts and "event:xlsx" in acts


def test_drl_dataset_and_summary_downloads(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/drl/dataset").status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/drl/dataset", params={"q": "", "format": "json"})
    assert r.status_code == 200 and r.json()["dataset"] == "dozimetrix-drl"
    s = client.get("/api/drl/summary", params={"q": "", "format": "csv", "min_n": 1})
    assert s.status_code == 200 and s.content.decode("utf-8-sig").startswith("unit,device,")
    assert client.get("/api/drl/summary", params={"set": "x"}).status_code == 400


def test_operators_report_is_admin_only(client):
    login(client, "phys", "phys-pass-12")
    assert client.get("/api/reports/operators").status_code == 403  # staff names
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/reports/operators", params={"year": 2026, "quarter": 3, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["start"] == "2025-10-01" and r.json()["end"] == "2026-09-30"
    x = client.get("/api/reports/operators", params={"year": 2026, "quarter": 3,
                                                      "format": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/api/reports/operators", params={"year": 2026,
                                                        "quarter": 5}).status_code == 400


def test_dose_check_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/dose-check", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["start"] == "2026-10-01" and r.json()["notifications"] == 0
    c = client.get("/api/reports/dose-check", params={"year": 2026, "quarter": 4, "q": "",
                                                       "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("protocol,")
    assert client.get("/api/reports/dose-check", params={"format": "xml"}).status_code == 400


def test_exposure_index_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/exposure-index", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["high"] == 3.0 and r.json()["rows"] == []
    c = client.get("/api/reports/exposure-index", params={"year": 2026, "quarter": 4, "q": "",
                                                           "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("room,")


def test_collimation_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/collimation", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["min_dept"] == 20 and r.json()["rooms"] == []
    c = client.get("/api/reports/collimation", params={"q": "", "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("room,verdict")


def test_scan_length_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/scan-length", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["flagged_total"] == 0
    assert client.get("/api/reports/scan-length", params={"ratio": 9}).status_code == 400
    c = client.get("/api/reports/scan-length", params={"q": "", "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("study_id,")


def test_reports_pages_need_exporter(client):
    """Analyses and registers (Reports, Quality) are read with the exporter or admin role."""
    login(client, "viewer", "viewer-pass-1")
    for url in ("/api/followups", "/api/incidents", "/api/incidents/1", "/api/qa/due",
                "/api/devices/1/qa", "/api/quality", "/api/protocols/compare?region=CT-CAP"):
        assert client.get(url).status_code == 403, url
    assert client.get("/api/drl/sets").status_code == 200  # the Reference levels page needs it
    login(client, "phys", "phys-pass-12")  # pid is not enough
    assert client.get("/api/followups").status_code == 403
    login(client, "regist", "regist-pass-12")  # exporter
    for url in ("/api/followups", "/api/incidents", "/api/qa/due", "/api/devices/1/qa",
                "/api/quality"):
        assert client.get(url).status_code == 200, url
    login(client, "boss", "boss-pass-123")  # admin
    assert client.get("/api/followups").status_code == 200


def test_followup_endpoints_and_roles(client):
    login(client, "regist", "regist-pass-12")
    r = client.get("/api/followups", params={"status": "pending"})
    assert r.status_code == 200 and r.json()["rows"] == []
    assert client.get("/api/followups", params={"status": "x"}).status_code == 400
    assert client.post("/api/followups/1", json={"status": "informed"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/followups/999", json={"status": "informed"}).status_code == 404
    assert client.get("/api/followups/999/letter").status_code == 403  # admin alone: no pid
    login(client, "phys", "phys-pass-12")
    assert client.get("/api/followups/999/letter").status_code == 404
    assert client.get("/api/followups/999/letter", params={"lang": "xx"}).status_code == 400


def test_incident_endpoints(client):
    login(client, "regist", "regist-pass-12")
    assert client.get("/api/incidents").json()["rows"] == []
    assert client.post("/api/incidents", json={"study_id": 1, "title": "x"}).status_code == 403
    assert client.get("/api/incidents", params={"status": "weird"}).status_code == 400
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/incidents", json={}).status_code == 400
    assert client.post("/api/incidents", json={"alert_id": 99999}).status_code == 404
    r = client.post("/api/incidents", json={"study_id": 1, "title": "Dose too high"})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    r = client.post(f"/api/incidents/{iid}", json={"cause": "long case", "note": "called"})
    assert r.status_code == 200 and [e["kind"] for e in r.json()["log"]] == [
        "opened", "change", "note"]
    r = client.post(f"/api/incidents/{iid}", json={"action": "close"})
    assert r.status_code == 400 and "outcome" in r.json()["error"]
    r = client.post(f"/api/incidents/{iid}", json={"outcome": "none", "reportable": False,
                                                    "action": "close"})
    assert r.status_code == 200 and r.json()["status"] == "closed"
    assert client.get(f"/api/incidents/{iid}").json()["reportable"] is False
    assert client.get("/api/incidents/99999").status_code == 404
    r = client.post(f"/api/incidents/{iid}", json={"action": "reopen", "note": "again"})
    assert r.status_code == 200 and r.json()["status"] == "open"


def test_optimisation_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/optimisation", params={"change": "2026-10-01", "q": "",
                                                         "note": "scores unchanged"})
    assert r.status_code == 200, r.text
    assert r.json()["image_quality_note"] == "scores unchanged" and r.json()["days"] == 180
    c = client.get("/api/reports/optimisation", params={"change": "2026-10-01", "q": "",
                                                         "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("change_date,")
    assert client.get("/api/reports/optimisation",
                      params={"change": "2026-10-01", "set": "nope"}).status_code == 400
    assert client.get("/api/reports/optimisation", params={"days": 5}).status_code == 422


def test_digest_endpoints_are_admin_only(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/digests").status_code == 403
    assert client.put("/api/digests", json=[]).status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.get("/api/digests").json() == []
    assert client.put("/api/digests", json=[{"name": "x", "to": ["bad"]}]).status_code == 400
    r = client.put("/api/digests", json=[{"name": "CT dept", "to": ["a@example.org"], "q": ""}])
    assert r.status_code == 200 and r.json()[0]["id"] == "ct-dept"
    p = client.get("/api/digests/ct-dept/preview")
    assert p.status_code == 200 and p.json()["subject"].startswith("[Dozimetrix] CT dept:")
    assert client.get("/api/digests/none/preview").status_code == 404


def test_qa_register_endpoints(client):
    login(client, "regist", "regist-pass-12")
    assert client.get("/api/devices/1/qa").json() == []
    assert client.get("/api/qa/due").json() == []
    body = {"kind": "dap_calibration", "done_on": "2026-05-01", "factor": 1.1,
            "next_due": "2027-05-01"}
    assert client.post("/api/devices/1/qa", json=body).status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/devices/999/qa", json=body).status_code == 404
    assert client.post("/api/devices/1/qa", json={**body, "factor": 9}).status_code == 400
    r = client.post("/api/devices/1/qa", json=body)
    assert r.status_code == 200, r.text
    got = client.get("/api/devices/1/qa").json()
    assert got[0]["factor"] == 1.1 and got[0]["entered_by"] == "boss"
    assert client.get("/api/qa/due", params={"days": 365}).json()[0]["kind"] == "dap_calibration"
    assert client.delete(f"/api/qa/{r.json()['id']}").status_code == 200
    assert client.delete("/api/qa/999").status_code == 404


def test_repeats_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/repeats", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["true_rejects"].startswith("not available") and "operators" not in r.json()
    assert client.get("/api/reports/repeats", params={"operators": True}).status_code == 403
    c = client.get("/api/reports/repeats", params={"q": "", "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("room,exam")
    assert client.get("/api/reports/repeats", params={"gap": 500}).status_code == 400
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/reports/repeats", params={"operators": True, "q": ""})
    assert r.status_code == 200 and r.json()["operators"] == []


def test_effective_dose_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/effective-dose", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["coefficients"] is False and r.json()["groups"] == []
    c = client.get("/api/reports/effective-dose", params={"q": "", "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("exam,")


def test_centering_report_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/reports/centering", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["measured"] == 0 and "unverified" in r.json()["note"]
    c = client.get("/api/reports/centering", params={"q": "", "format": "csv"})
    assert c.status_code == 200 and c.content.decode("utf-8-sig").startswith("room,method")


def test_ct_kfactor_endpoints_and_new_pages(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/ct-kfactors").json()["rows"] == []
    row = {"region": "head", "age_to_years": None, "phantom_cm": 16, "k": 0.002, "source": "test"}
    assert client.put("/api/ct-kfactors", json=[row]).status_code == 403
    for page in ("analysis", "followups", "incidents", "qa", "digests", "kfactors"):
        r = client.get(f"/{page}.html")
        assert r.status_code == 200 and f"pages/{page}.js" in r.text, page
    login(client, "boss", "boss-pass-123")
    assert client.put("/api/ct-kfactors", json=[{**row, "region": "knee"}]).status_code == 400
    assert client.put("/api/ct-kfactors", json=[{**row, "phantom_cm": "x"}]).status_code == 400
    assert client.put("/api/ct-kfactors", json=[row]).json() == {"rows": 1}
    got = client.get("/api/ct-kfactors").json()
    assert got["rows"][0]["k"] == 0.002 and "trunk" in got["regions"]
    assert client.put("/api/ct-kfactors", json=[]).json() == {"rows": 0}


def test_insp1255_report_endpoint(client):
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/reports/insp1255", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["key"] for s in body["sheets"]][:2] == ["radiologie", "ct"]
    x = client.get("/api/reports/insp1255", params={"year": 2026, "quarter": 4,
                                                     "format": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/api/reports/insp1255", params={"year": 2026,
                                                       "quarter": 7}).status_code == 400
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/reports/insp1255", params={"year": 2026,
                                                       "quarter": 1}).status_code == 403


def test_art12_needs_exporter_and_pid_for_the_workbooks(client):
    login(client, "boss", "boss-pass-123")  # admin: may look, may not take patient identifiers
    r = client.get("/api/reports/art12", params={"year": 2026, "q": ""})
    assert r.status_code == 200 and r.json()["per_sheet"] == 20
    assert r.json()["window"] == ["2026-02-01", "2026-03-31"]
    assert client.get("/api/reports/art12", params={"year": 2026, "format": "zip"}
                      ).status_code == 403
    login(client, "regist", "regist-pass-12")
    z = client.get("/api/reports/art12", params={"year": 2026, "q": "", "format": "zip"})
    assert z.status_code == 200 and z.content[:2] == b"PK"
    assert client.get("/api/reports/art12", params={"year": 2026, "format": "xls"}
                      ).status_code == 400
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/reports/art12", params={"year": 2026}).status_code == 403


def test_every_page_in_the_web_folder_is_served(client):
    from dozimetrix.api.app import PAGES, WEB

    login(client, "viewer", "viewer-pass-1")
    on_disk = {p.name for p in WEB.glob("*.html")}
    assert on_disk <= set(PAGES.values()), on_disk - set(PAGES.values())  # a page nobody can open
    r = client.get("/dose-statement.html")
    assert r.status_code == 200 and "dose-statement.js" in r.text


def test_renamed_page_redirects_with_its_query(client):
    r = client.get("/dicom.html?tab=his", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/integration.html?tab=his"


def test_dose_statement_names_the_patient_so_needs_pid_and_is_audited(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/studies/1/dose-statement").status_code == 403
    login(client, "phys", "phys-pass-12")
    r = client.get("/api/studies/1/dose-statement")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient"]["name"] == "POPESCU^ION" and body["exam"] == "Coronary angiography"
    got = {d["key"]: d for d in body["doses"]}
    assert got["dap"]["value"] == 12500 and got["dap"]["unit"] == "mGy·cm²"
    assert got["rp"]["value"] == pytest.approx(420.0) and got["rp"]["unit"] == "mGy"  # Gy -> mGy
    assert "dlp" not in got and "activity" not in got  # only what applies to the examination
    assert client.get("/api/studies/9999/dose-statement").status_code == 404
    login(client, "boss", "boss-pass-123")
    log = client.get("/api/audit", params={"action": "identify"}).json()["rows"]
    assert any(x["target"] == "dose-statement:study:1" and x["user"] == "phys" for x in log)


def test_glandularity_is_typed_in_for_mammography_only(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.put("/api/studies/3/glandularity", json={"percent": 50}).status_code == 403
    login(client, "boss", "boss-pass-123")
    mg = client.get("/api/studies", params={"q": "modality:MG"}).json()
    sid = next(r["id"] for r in mg["rows"] if r["modality"] == "MG")
    assert client.put(f"/api/studies/{sid}/glandularity", json={"percent": 25}).json() == {
        "study_id": sid, "glandularity_pct": 25.0}
    assert client.get(f"/api/studies/{sid}").json()["glandularity_pct"] == 25.0
    assert client.put(f"/api/studies/{sid}/glandularity", json={"percent": 140}).status_code == 400
    assert client.put("/api/studies/1/glandularity", json={"percent": 50}).status_code == 400  # XA
    assert client.put(f"/api/studies/{sid}/glandularity", json={"percent": None}).status_code == 200
    log = client.get("/api/audit", params={"action": "glandularity"}).json()["rows"]
    assert log and log[0]["detail"]["from"] == 25.0


def test_plan_of_measures_crud_and_deadline_marking(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/actions", params={"year": 2026, "quarter": 3}).json()["rows"] == []
    body = {"year": 2026, "quarter": 3, "finding": "T1 above DRL", "action": "Check the AEC",
            "owner": "physicist"}
    assert client.post("/api/actions", json=body).status_code == 403  # writing needs admin
    login(client, "regist", "regist-pass-12")
    assert client.post("/api/actions", json=body).status_code == 403  # exporter too
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/actions", json={**body, "quarter": 9}).status_code == 400
    assert client.post("/api/actions", json={**body, "action": " "}).status_code == 400
    a = client.post("/api/actions", json=body).json()
    assert a["status"] == "open" and a["created_by"] == "boss"
    put = client.put(f"/api/actions/{a['id']}", json={"status": "doing"})
    assert put.json()["status"] == "doing"
    assert client.put(f"/api/actions/{a['id']}", json={"status": "x"}).status_code == 400
    assert client.put(f"/api/actions/{a['id']}", json={"finding": " "}).status_code == 400
    rows = client.get("/api/actions", params={"year": 2026, "quarter": 3}).json()["rows"]
    assert [r["status"] for r in rows] == ["doing"] and rows[0]["finding"] == "T1 above DRL"
    assert client.delete(f"/api/actions/{a['id']}").json() == {"deleted": a["id"]}
    assert client.delete(f"/api/actions/{a['id']}").status_code == 404
    # deadlines: NOW is 2026-11-23: the Q3 report (due 20 Oct) is overdue and listed
    ov = client.get("/api/overview").json()["deadlines"]
    assert any(d["id"] == "insp:2026Q3" and d["level"] == "alert" for d in ov)
    assert client.post("/api/deadlines/insp:2026Q3/done").json() == {"done": "insp:2026Q3"}
    left = client.get("/api/overview").json()["deadlines"]
    assert not any(d["id"] == "insp:2026Q3" for d in left)
    assert client.post("/api/deadlines/art12:2031/done").status_code == 404
    login(client, "viewer", "viewer-pass-1")
    assert client.post("/api/deadlines/insp:2026Q3/done").status_code == 403


def test_exports_list_run_and_audit_through_one_pair_of_routes(client):
    login(client, "viewer", "viewer-pass-1")
    ls = client.get("/api/exports").json()
    cohort = next(x for x in ls["specs"] if x["id"] == "dozimetrix-cohort")
    assert cohort["allowed"] is False and ls["problems"] == [] and ls["plugins"] == []
    assert client.get("/api/exports/dozimetrix-cohort",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).status_code == 403
    login(client, "regist", "regist-pass-12")
    assert next(x for x in client.get("/api/exports").json()["specs"]
                if x["id"] == "dozimetrix-cohort")["allowed"] is True
    r = client.get("/api/exports/dozimetrix-cohort",
                   params={"from": "2026-01-01", "to": "2026-12-31", "q": "modality:XA"})
    assert r.status_code == 200, r.text
    sheet = r.json()["books"][0]["sheets"][0]
    assert sheet["columns"][0] == "Study" and len(sheet["rows"]) == 1  # the one XA study
    x = client.get("/api/exports/dozimetrix-cohort", params={
        "from": "2026-01-01", "to": "2026-12-31", "q": "", "format": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/api/exports/dozimetrix-cohort", params={"from": "x"}).status_code == 400
    assert client.get("/api/exports/dozimetrix-cohort", params={
        "from": "2026-01-01", "to": "2026-12-31", "format": "zip"}).status_code == 400
    assert client.get("/api/exports/nope").status_code == 404
    login(client, "boss", "boss-pass-123")
    log = client.get("/api/audit", params={"action": "report"}).json()["rows"]
    assert any(x["target"] == "spec:dozimetrix-cohort@1:json" and x["user"] == "regist"
               for x in log)


def test_survey_export_endpoint(client):
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/survey/export", params={"month": "2026-11", "q": "", "format": "json"})
    assert r.status_code == 200 and r.json()["dataset"] == "dozimetrix-paediatric-survey"
    assert client.get("/api/survey/export", params={"month": "nov"}).status_code == 400


def test_quality_endpoint_and_study_findings(client):
    login(client, "regist", "regist-pass-12")
    r = client.get("/api/quality", params={"q": ""}).json()
    assert r["studies"] >= 1 and "ct_dlp_mismatch" in r["rules"]
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    assert isinstance(client.get(f"/api/studies/{sid}").json()["quality"], list)


def test_alert_endpoints(client):
    from dozimetrix import alerts

    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        xa = s.scalar(select(Study).filter_by(study_uid="1.2.3"))
        xa.total_rp_dose = 6.0
        alerts.evaluate(s, [xa.id])
        s.commit()
    login(client, "viewer", "viewer-pass-1")
    open_ = client.get("/api/alerts").json()
    a = next(x for x in open_ if x["rule"] == "fluoro-kar-5gy")
    assert a["study_id"] and a["level"] == "alert"
    assert any(x["id"] == a["id"] for x in client.get("/api/overview").json()["alerts"])
    assert client.post(f"/api/alerts/{a['id']}/ack", json={}).status_code == 403  # admin only
    login(client, "boss", "boss-pass-123")
    r = client.post(f"/api/alerts/{a['id']}/ack", json={"note": "seen, patient informed"})
    assert r.json()["status"] == "acknowledged"
    assert all(x["id"] != a["id"] for x in client.get("/api/alerts").json())
    login(client, "viewer", "viewer-pass-1")
    rules = client.get("/api/alert-rules").json()
    rid = next(x["id"] for x in rules if x["key"] == "drl-2x")
    assert client.patch(f"/api/alert-rules/{rid}", json={"threshold": 3}).status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.patch(f"/api/alert-rules/{rid}", json={"threshold": 3, "severity": "alert"})
    assert r.json()["changes"]["threshold"] == {"from": 2.0, "to": 3.0}
    assert client.patch(f"/api/alert-rules/{rid}", json={"severity": "x"}).status_code == 400


def test_dicom_node_endpoints(client):
    login(client, "viewer", "viewer-pass-1")
    st = client.get("/api/dicom/status").json()
    assert st["local"]["aet"] and len(st["coverage"]) == 120
    assert client.post("/api/dicom/nodes", json={"name": "CT 1", "aet": "CT1"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    n = client.post("/api/dicom/nodes", json={"name": "CT 1", "aet": "CT1", "sends": True})
    assert n.status_code == 200, n.text
    nid = n.json()["id"]
    assert client.post("/api/dicom/nodes", json={"name": "X", "aet": "Y" * 20}).status_code == 400
    r = client.patch(f"/api/dicom/nodes/{nid}", json={"notes": "room 3"}).json()
    assert r["changes"]["notes"]["to"] == "room 3"
    assert "CT1" in client.get("/api/dicom/status").json()["local"]["allowed"]
    assert client.post(f"/api/dicom/nodes/{nid}/echo").status_code == 400  # no host
    assert client.delete(f"/api/dicom/nodes/{nid}").json() == {"deleted": "CT 1"}


def test_backup_status_is_admin_only(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/backup/status").status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/backup/status").json()
    assert r["state"] in ("never", "ok", "stale") and r["max_hours"] == 26


def test_alert_rule_create_and_delete(client):
    from dozimetrix.db.models import Alert, AlertRule, Study

    good = {"name": "DAP over 5 Gy.cm2", "kind": "threshold", "metric": "dap_total",
            "threshold": 5.0, "modality": "xa, rf", "severity": "alert", "notify": True}
    login(client, "viewer", "viewer-pass-1")
    assert client.post("/api/alert-rules", json=good).status_code == 403
    assert client.get("/api/alert-rules/metrics").status_code == 200
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/alert-rules", json=good)
    assert r.status_code == 200, r.text
    rule = r.json()
    assert rule["key"] == "dap-over-5-gy-cm2" and rule["unit"] == "Gy.cm2"
    assert rule["modality"] == "XA,RF" and rule["custom"] is True and rule["enabled"] is True
    assert client.post("/api/alert-rules", json=good).json()["key"] == "dap-over-5-gy-cm2-2"
    listed = {x["key"]: x for x in client.get("/api/alert-rules").json()}
    assert listed[rule["key"]]["custom"] is True and listed["fluoro-kar-5gy"]["custom"] is False
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "alert_rule.create", AuditLog.target == f"rule:{rule['key']}")) == 1
    bad = [({"kind": "nope"}, "kind"), ({"metric": "dlp"}, None),  # event metric is fine for threshold
           ({"severity": "x"}, "severity"), ({"modality": "C"}, "modality"),
           ({"threshold": 0}, "threshold"), ({"name": " "}, "name"), ({"metric": "nope"}, "metric"),
           ({"window_days": 5}, "window"), ({"max_age_days": 0}, "age")]
    for patch, word in bad:
        resp = client.post("/api/alert-rules", json={**good, **patch})
        assert (resp.status_code == 200) if word is None else (resp.status_code == 400 and word in resp.text), patch
    cum = {"name": "Σ", "kind": "cumulative", "threshold": 1.0, "window_days": 30}
    assert client.post("/api/alert-rules", json={**cum, "metric": "dlp"}).status_code == 400
    assert client.post("/api/alert-rules", json={**cum, "metric": "dap_total", "window_days": None}).status_code == 400
    assert client.post("/api/alert-rules", json={**cum, "metric": "dap_total"}).status_code == 200
    assert client.post("/api/alert-rules", json={"name": "Preg", "kind": "pregnant"}).json()["threshold"] == 0
    # a group stands for its modalities; a code no group knows is refused (the rule would never fire)
    dx = client.post("/api/alert-rules", json={**good, "name": "DX", "modality": "dx"}).json()
    assert dx["modality"] == "DX,CR"
    r = client.post("/api/alert-rules", json={**good, "name": "XR", "modality": "XR"})
    assert r.status_code == 400 and "XR" in r.text
    assert client.post("/api/alert-rules", json={"name": "N", "kind": "count", "threshold": 3,
                                                 "window_days": 365}).status_code == 200
    # delete: a default rule stays, a custom one goes with its alerts
    assert client.delete(f"/api/alert-rules/{listed['fluoro-kar-5gy']['id']}").status_code == 400
    with make_sessionmaker(make_engine(url))() as s:
        study = s.scalar(select(Study))
        s.add(Alert(rule_id=rule["id"], study_id=study.id, severity="alert", message="x"))
        s.commit()
    login(client, "viewer", "viewer-pass-1")
    assert client.delete(f"/api/alert-rules/{rule['id']}").status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.delete(f"/api/alert-rules/{rule['id']}").json() == {
        "deleted": "DAP over 5 Gy.cm2", "alerts_removed": 1}
    assert client.delete(f"/api/alert-rules/{rule['id']}").status_code == 404
    with make_sessionmaker(make_engine(url))() as s:
        assert s.scalar(select(func.count()).select_from(Alert).where(Alert.rule_id == rule["id"])) == 0
        assert s.scalar(select(AlertRule).filter_by(key="dap-over-5-gy-cm2-2")) is not None


def test_notify_settings_endpoints(client):
    from dozimetrix.db.models import AuditLog

    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/notify").status_code == 403
    assert client.put("/api/notify", json={"ntfy_url": "http://x/t"}).status_code == 403
    assert client.post("/api/notify/test", json={"channel": "ntfy"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    first = client.get("/api/notify").json()
    assert first["channels"] == [] and first["smtp_password_set"] is False
    for bad in ({"smtp_port": 70000}, {"email_to": ["nope"]}, {"ntfy_url": "ftp://x"},
                {"email_from": "a b"}):
        assert client.put("/api/notify", json=bad).status_code == 400, bad
    ok = client.put("/api/notify", json={
        "ntfy_url": "https://ntfy.example/topic", "ntfy_token": "tok-SECRET-1",
        "smtp_host": "mail.example", "email_to": ["a@b.ro", "c@d.ro"], "smtp_password": "pw-SECRET-2"})
    assert ok.status_code == 200 and ok.json()["changes"]["ntfy_token"] == "set"
    got = client.get("/api/notify").json()
    assert got["channels"] == ["email", "ntfy"] and got["ntfy_token_set"] is True
    assert got["fields"]["ntfy_url"] == {"value": "https://ntfy.example/topic", "source": "ui"}
    assert got["fields"]["email_to"]["value"] == ["a@b.ro", "c@d.ro"]
    assert "SECRET" not in client.get("/api/notify").text
    sent = []
    client.app.state.notify_senders = {"ntfy": lambda c, m: sent.append((c.ntfy_token, m.title)),
                                       "email": lambda c, m: (_ for _ in ()).throw(OSError("smtp down"))}
    r = client.post("/api/notify/test", json={"channel": "ntfy"}).json()
    assert r == {"ok": True, "error": None} and sent == [("tok-SECRET-1", "[Dozimetrix] Test notification")]
    r = client.post("/api/notify/test", json={"channel": "email"}).json()
    assert r["ok"] is False and "smtp down" in r["error"]
    assert client.post("/api/notify/test", json={"channel": "sms"}).status_code == 400
    assert client.put("/api/notify", json={"clear_ntfy_token": True, "smtp_host": ""}).json()["changes"]
    got = client.get("/api/notify").json()
    assert got["ntfy_token_set"] is False and got["channels"] == ["ntfy"]
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action.like("notify.%"))))
        assert {r.action for r in rows} == {"notify.update", "notify.test"}
        assert "SECRET" not in " ".join(str(r.detail) for r in rows)


def test_site_settings_endpoints(client):
    from dozimetrix.db.models import AuditLog

    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/settings").status_code == 403
    assert client.put("/api/settings", json={"base_url": "http://x"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    got = client.get("/api/settings").json()
    assert got["base_url"] == {"value": None, "source": None}
    assert got["spool_retention_days"]["source"] == "toml"
    d = got["deployment"]
    assert d["dicom"]["aet"] and d["database"]["driver"].startswith("sqlite") and "password" not in str(d).lower()
    for bad in ({"base_url": "javascript:alert(1)"}, {"base_url": "dose.example"},
                {"spool_retention_days": -1}, {"spool_retention_days": 99999}, {"name": " "}):
        assert client.put("/api/settings", json=bad).status_code == 400, bad
    r = client.put("/api/settings", json={"name": "Spitalul Test", "code": "tst",
                                          "base_url": "https://dose.example.org/",
                                          "spool_retention_days": 30})
    assert r.status_code == 200, r.text
    assert set(r.json()["changes"]) == {"name", "code", "base_url", "spool_retention_days"}
    got = client.get("/api/settings").json()
    assert got["site"] == {"name": "Spitalul Test", "code": "TST"}
    assert got["base_url"] == {"value": "https://dose.example.org", "source": "ui"}  # no trailing slash
    assert got["spool_retention_days"]["value"] == 30 and got["spool_retention_days"]["source"] == "ui"
    # the notification links use it
    from dozimetrix import notify

    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        assert notify.load_settings(s, {"base_url": "https://old"}, b"k" * 32).base_url == "https://dose.example.org"
    # empty values go back to the toml / default
    assert client.put("/api/settings", json={"base_url": "", "spool_retention_days": None}).status_code == 200
    got = client.get("/api/settings").json()
    assert got["base_url"]["value"] is None and got["spool_retention_days"]["source"] == "toml"
    with make_sessionmaker(make_engine(url))() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "settings.update")) >= 2


def test_his_server_endpoints(client):
    from tests import his_stub

    stub = his_stub.Stub()
    try:
        login(client, "viewer", "viewer-pass-1")
        assert client.get("/api/his/status").json()["servers"] == []
        assert client.post("/api/his/servers", json={"name": "HIS"}).status_code == 403
        login(client, "boss", "boss-pass-123")
        assert client.post("/api/his/servers", json={"name": "HIS"}).status_code == 400
        r = client.post("/api/his/servers", json={
            "name": "HIS", "base_url": stub.url, "username": his_stub.USER, "password": "bad"})
        assert r.status_code == 200, r.text
        sid, body = r.json()["id"], r.json()
        assert body["password_set"] and "bad" not in r.text
        assert client.post(f"/api/his/servers/{sid}/check").json()["ok"] is False
        r = client.patch(f"/api/his/servers/{sid}", json={"password": his_stub.PASSWORD})
        assert r.json()["changes"] == {"password": "changed"}
        assert client.post(f"/api/his/servers/{sid}/check").json()["ok"] is True
        audit = client.get("/api/audit").text
        assert his_stub.PASSWORD not in audit and '"bad"' not in audit
        dev = client.get("/api/devices").json()["records"][0]
        assert dev["his_policy"] == "full"
        assert client.patch(f"/api/devices/{dev['id']}",
                            json={"his_policy": "patient"}).status_code == 200
        assert client.patch(f"/api/devices/{dev['id']}",
                            json={"his_policy": "maybe"}).status_code == 400
        sid_study = client.get("/api/studies").json()["rows"][0]["id"]
        r = client.post(f"/api/studies/{sid_study}/his")
        assert r.status_code == 200 and r.json()["status"] == "pending"
        assert client.get(f"/api/studies/{sid_study}").json()["his"]["status"] == "pending"
        assert client.delete(f"/api/his/servers/{sid}").json() == {"deleted": "HIS"}
    finally:
        stub.close()


def test_users_and_audit(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/audit").status_code == 403
    login(client, "boss", "boss-pass-123")
    body = client.get("/api/users").json()
    assert "pid" in body["roles"] and any(u["username"] == "boss" for u in body["users"])
    bad = client.post("/api/users", json={"username": "nurse", "password": "short",
                                         "roles": ["viewer"]})
    assert bad.status_code == 400 and "10 characters" in bad.json()["error"]
    assert client.post("/api/users", json={"username": "nurse", "password": "long-enough-1",
                                           "roles": ["root"]}).status_code == 400
    r = client.post("/api/users", json={"username": "nurse", "name": "Nurse",
                                        "password": "long-enough-1", "roles": ["viewer"]})
    assert r.json()["roles"] == ["viewer"]
    r = client.patch("/api/users/nurse", json={"roles": ["viewer", "exporter"],
                                               "password": "another-pass-2"}).json()
    assert r["changes"]["password"] == "changed" and r["roles"] == ["exporter", "viewer"]
    assert client.patch("/api/users/boss", json={"active": False}).status_code == 400
    assert client.patch("/api/users/boss", json={"roles": ["viewer"]}).status_code == 400
    log = client.get("/api/audit", params={"action": "user."}).json()
    acts = [x["action"] for x in log["rows"]]
    assert acts[:2] == ["user.update", "user.create"]
    assert "another-pass" not in str(log) and "long-enough" not in str(log)
    client.cookies.clear()
    assert client.post("/api/login", json={"username": "nurse",
                                           "password": "another-pass-2"}).status_code == 200


def test_user_rename_and_remove(client):
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/users", json={"username": "tech", "password": "tech-pass-123",
                                           "roles": ["viewer"]}).status_code == 200
    login(client, "tech", "tech-pass-123")
    client.get("/api/studies")
    tech_cookie = dict(client.cookies)
    login(client, "boss", "boss-pass-123")
    # rename: same rules as at creation; the renamed user stays signed in (cookie holds the id)
    assert client.patch("/api/users/tech", json={"username": "viewer"}).status_code == 400
    assert client.patch("/api/users/tech", json={"username": "a b"}).status_code == 400
    r = client.patch("/api/users/tech", json={"username": "radtech"}).json()
    assert r["username"] == "radtech"
    assert r["changes"]["username"] == {"from": "tech", "to": "radtech"}
    boss_cookie = dict(client.cookies)
    client.cookies.clear()
    client.cookies.update(tech_cookie)
    assert client.get("/api/me").json()["username"] == "radtech"
    assert client.delete("/api/users/viewer").status_code == 403  # not an admin
    client.cookies.clear()
    client.cookies.update(boss_cookie)
    assert client.post("/api/login", json={"username": "radtech",
                                           "password": "tech-pass-123"}).status_code == 200
    login(client, "boss", "boss-pass-123")
    # remove: never an admin (nor oneself); audit rows survive with the name they had
    assert client.delete("/api/users/boss").status_code == 400
    assert client.delete("/api/users/nobody").status_code == 400
    assert client.delete("/api/users/radtech").status_code == 200
    assert not any(u["username"] == "radtech" for u in client.get("/api/users").json()["users"])
    client.cookies.clear()
    client.cookies.update(tech_cookie)
    assert client.get("/api/studies").status_code == 401
    login(client, "boss", "boss-pass-123")
    log = client.get("/api/audit", params={"action": "user."}).json()["rows"]
    assert [x["action"] for x in log[:2]] == ["user.delete", "user.update"]
    assert log[0]["target"] == "user:radtech" and log[0]["detail"]["roles"] == "viewer"
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        s.add(User(username="admin2", password_hash="x", roles="viewer,admin"))
        s.commit()
    r = client.delete("/api/users/admin2")
    assert r.status_code == 400 and "admin role" in r.json()["error"]


def test_cli_user_rename_and_delete(client):
    from dozimetrix import cli

    url = str(client.app.state.settings.database_url)
    base = ["user", "--url", url]
    with make_sessionmaker(make_engine(url))() as s:
        s.add(User(username="clerk", password_hash="x", roles="viewer"))
        s.commit()
    assert cli.main(base[:1] + ["rename", "clerk", "--to", "viewer"] + base[1:]) == 2
    assert cli.main(base[:1] + ["rename", "clerk", "--to", "physicist"] + base[1:]) == 0
    assert cli.main(base[:1] + ["delete", "boss"] + base[1:]) == 2
    assert cli.main(base[:1] + ["delete", "physicist"] + base[1:]) == 0
    assert cli.main(base[:1] + ["delete", "physicist"] + base[1:]) == 2
    with make_sessionmaker(make_engine(url))() as s:
        names = set(s.scalars(select(User.username)))
        assert "clerk" not in names and "physicist" not in names and "boss" in names
        acts = [a for (a,) in s.execute(select(AuditLog.action).where(
            AuditLog.username.like("cli:%")).order_by(AuditLog.id))]
        assert acts[-2:] == ["user.update", "user.delete"]


def test_alerts_page_api(client):
    from dozimetrix import alerts

    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        xa = s.scalar(select(Study).filter_by(study_uid="1.2.3"))
        xa.total_rp_dose, xa.total_dap = 7.0, 600.0
        alerts.evaluate(s, [xa.id])
        s.commit()
    login(client, "viewer", "viewer-pass-1")
    summ = client.get("/api/alerts/summary", params={"status": "all"}).json()
    assert summ["total"] >= 2 and summ["by_level"].get("alert", 0) >= 2
    keys = {r["rule"] for r in summ["by_rule"]}
    assert {"fluoro-kar-5gy", "fluoro-dap-500"} <= keys
    # selecting a rule must not shrink the rule dropdown
    sel = client.get("/api/alerts/summary",
                     params={"status": "all", "rule": "fluoro-dap-500"}).json()
    assert {r["rule"] for r in sel["by_rule"]} == keys and sel["total"] == 1
    one = client.get("/api/alerts", params={"status": "all", "rule": "fluoro-dap-500"}).json()
    assert len(one) == 1 and one[0]["modality"] == "XA"
    assert client.get("/api/alerts", params={"since": "bad"}).status_code == 400
    ids = [a["id"] for a in client.get("/api/alerts", params={"modality": "XA"}).json()]
    assert client.post("/api/alerts/ack", json={"ids": ids}).status_code == 403  # admin only
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/alerts/ack", json={"ids": ids, "note": "reviewed"}).json()
    assert r["acknowledged"] == len(ids) > 0
    acked = client.get("/api/alerts", params={"status": "acknowledged", "modality": "XA"}).json()
    assert "reviewed" in {a["ack_note"] for a in acked}
    assert client.post("/api/alerts/ack", json={"ids": []}).status_code == 400


def test_grid_report_endpoint(client):
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/reports/grid", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["grid"].startswith("ICRP 135") and "rows" in r.json()
    x = client.get("/api/reports/grid", params={"year": 2026, "quarter": 4, "format": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/api/reports/grid", params={"year": 2026, "quarter": 5}).status_code == 400
    s = client.get("/api/stats", params={"q": "", "metric": "dlp", "by": "exam,ref_group",
                                         "system": "ro-nom-ct"})
    assert s.status_code == 200 and "ref_group" in s.json()["orders"]
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/reports/grid", params={"year": 2026, "quarter": 4}).status_code == 403


def test_sizes_import_csv(client):
    login(client, "viewer", "viewer-pass-1")
    csv = b"accession,weight_kg,source\nA1,86,measured\n"
    assert client.post("/api/import/sizes", content=csv,
                       headers={"content-type": "text/csv"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/import/sizes", content=csv, headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["counts"]["studies_updated"] == 1
    assert client.get("/api/studies/1").json()["patient"]["weight_kg"] == 88  # dry run
    r = client.post("/api/import/sizes?apply=1", content=csv,
                    headers={"content-type": "text/csv"})
    assert r.json()["applied"] and client.get("/api/studies/1").json()["patient"][
        "weight_kg"] == 86
    assert client.post("/api/import/sizes", content=b"x\n1\n",
                       headers={"content-type": "text/csv"}).status_code == 400


def test_study_rdsr_download(client):
    import io

    import pydicom

    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/studies/1/rdsr").status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/studies/1/rdsr")
    assert r.status_code == 200 and r.headers["content-type"] == "application/dicom"
    ds = pydicom.dcmread(io.BytesIO(r.content))
    assert ds.PatientID == "P-ABCDE-12345" and ds.PatientName == ""
    assert client.get("/api/studies/1/rdsr?pid=1").status_code == 403


def test_indication_endpoints(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.put("/api/studies/1/indication", json={"code": "RSP-PNM"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.put("/api/studies/1/indication", json={"code": "NOPE"}).status_code == 400
    r = client.put("/api/studies/1/indication", json={"code": "RSP-PNM"})
    assert r.status_code == 200 and r.json()["status"] == "manual"
    assert client.get("/api/studies/1").json()["indication"]["code"] == "RSP-PNM"
    assert client.put("/api/studies/1/indication", json={"code": None}).json() is None
    r = client.put("/api/indications/mappings", json={"text": "tuse seaca", "code": "PED-TUS"})
    assert r.status_code == 200 and r.json()["studies"] == 0
    rev = client.get("/api/indications/review").json()
    assert rev["decisions"][0]["code"] == "PED-TUS"
    client.put("/api/indications/mappings", json={"text": "tuse seaca", "code": None})


def test_survey_central_endpoints(client):
    from tests.test_aggregate import _csv

    login(client, "viewer", "viewer-pass-1")
    body = _csv("I-API", "2026-10", [{"dlp": 400}] * 10)
    assert client.post("/api/survey/uploads", content=body,
                       headers={"content-type": "text/csv"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/survey/uploads?filename=a.csv", content=body,
                    headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["rows"] == 10
    assert client.post("/api/survey/uploads", content=body,
                       headers={"content-type": "text/csv"}).status_code == 400  # same file
    assert client.put("/api/survey/institutions/I-API", json={"name": "Site A"}).status_code == 200
    rep = client.get("/api/survey/national", params={"start": "2026-01", "end": "2026-12"}).json()
    assert rep["rows"][0]["institutions"][0]["name"] == "Site A"
    up = client.get("/api/survey/uploads").json()[0]
    assert client.delete(f"/api/survey/uploads/{up['id']}").json()["rows"] == 10


def test_change_own_password(client):
    login(client, "phys", "phys-pass-12")
    put = lambda cur, new: client.put("/api/me/password", json={"current": cur, "new": new})  # noqa: E731
    assert put("nope", "x" * 12).status_code == 400
    assert put("phys-pass-12", "short").status_code == 400
    r = client.put("/api/me/password", json={"current": "phys-pass-12", "new": "phys-pass-new-1"})
    assert r.status_code == 200
    assert login(client, "phys", "phys-pass-new-1").status_code == 200
    client.put("/api/me/password", json={"current": "phys-pass-new-1", "new": "phys-pass-12"})


def test_a_query_that_does_not_parse_is_refused_not_silently_dropped(client):
    """A typo must not widen a report or export to everyone; the list shows the error."""
    login(client, "boss", "boss-pass-123")
    for typo in ("modalty:CT", "date:2025-13-45", "dlp:abc"):
        r = client.get("/api/quality", params={"q": typo})
        assert r.status_code == 400 and "query not understood" in r.json()["error"], typo
        assert client.get("/api/stats", params={"q": typo, "metric": "dlp_total"}
                          ).status_code == 400
        assert client.get("/api/export", params={"q": typo}).status_code == 400
    ok = client.get("/api/quality", params={"q": "modality:XA"})
    assert ok.status_code == 200
    lst = client.get("/api/studies", params={"q": "modalty:CT"})  # the list keeps going
    assert lst.status_code == 200 and lst.json()["errors"]


def test_live_stream_sends_current_state_per_topic(client, monkeypatch):
    from dozimetrix.api import app as app_mod

    monkeypatch.setattr(app_mod, "LIVE_MAX_S", 0)  # one snapshot, then the stream ends
    client.cookies.clear()
    assert client.get("/api/live").status_code == 401
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/live?topics=dicom,his")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith("retry: ") and "event: dicom" in r.text
    assert "event: his" not in r.text  # HIS state is for admins
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/live?topics=his,bogus")
    assert "event: his" in r.text and "bogus" not in r.text
    data = r.text.split("event: his\ndata: ", 1)[1].split("\n", 1)[0]
    import json
    assert set(json.loads(data)) == {"worker", "queue", "waiting", "recent"}
    r = client.get("/api/live")  # default: the pulse
    pulse = json.loads(r.text.split("event: pulse\ndata: ", 1)[1].split("\n", 1)[0])
    assert set(pulse) == {"study", "alert", "alerts_open"} and pulse["study"]
    login(client, "viewer", "viewer-pass-1")  # services: for every signed-in user
    r = client.get("/api/live?topics=services")
    svc = json.loads(r.text.split("event: services\ndata: ", 1)[1].split("\n", 1)[0])
    assert [x["key"] for x in svc] == ["web", "receiver", "qr", "his"]
    assert svc[0]["state"] == "ok" and svc[1]["state"] == "alert"  # receiver never started
    r = client.get("/api/live?topics=studies,alerts")  # the slow topics, first snapshot
    studies_ = json.loads(r.text.split("event: studies\ndata: ", 1)[1].split("\n", 1)[0])
    assert studies_ and all(isinstance(v[0], int) and len(v) == 2 for v in studies_.values())
    assert "event: alerts" in r.text


def test_live_study_topic_stamps_one_study(client, monkeypatch):
    from dozimetrix.api import app as app_mod

    monkeypatch.setattr(app_mod, "LIVE_MAX_S", 0)
    login(client, "viewer", "viewer-pass-1")
    sid = client.get("/api/studies").json()["rows"][0]["id"]
    r = client.get(f"/api/live?topics=study:{sid},study:x1")
    assert f"event: study:{sid}" in r.text and '"events": [' in r.text
    assert "study:x1" not in r.text


def test_his_waiting_breakdown(client):
    login(client, "boss", "boss-pass-123")
    wt = client.get("/api/his/status").json()["waiting"]
    assert set(wt) == {"due", "retry", "next_retry", "history", "backfill_hours"}
    assert wt["due"] >= 0 and wt["retry"] >= 0 and wt["history"] >= 0


def test_protocol_comparison_endpoint(client):
    login(client, "regist", "regist-pass-12")
    r = client.get("/api/protocols/compare", params={"region": "CT-CAP", "min_n": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["region"] == "CT-CAP" and body["parameter"] == "age" and "rows" in body
    assert client.get("/api/protocols/compare", params={"region": "NOPE"}).status_code == 400
    assert client.get("/api/protocols/compare",
                      params={"region": "CT-CAP", "by": "x"}).status_code == 400
    assert client.get("/protocols.html").status_code == 200


def test_size_bands_by_protocol_name(client):
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        d = Device(display_name="Ysio UPU", model_name="Ysio", identity_key="k-sizeband", is_local=True)
        s.add(d)
        s.flush()
        n = 0
        for name, daps in [("W035 Torace a.p. 5-15Kg", [0.01, 0.02, 0.03]), ("T035 Torace a.p. 5-15 Kg", [0.04]),
                           ("W001a Craniu a.p. 1-6 ani", [0.1, 0.2, 0.3]), ("W008 Sinusuri", [0.5, 0.6, 0.7]), ("W080 Abdomen a.p. 5-20 Kg", [0.8, 0.9, 1.0])]:
            for dap in daps:
                n += 1
                s.add(Study(study_uid=f"9.9.{n}", accession=f"SB{n}", modality="DX", device_id=d.id,
                            study_datetime=NOW - dt.timedelta(days=1), source_kind="test", n_events=1,
                            events=[Event(protocol=name, dap=dap)]))
        s.commit()
        dev = d.id
    login(client, "viewer", "viewer-pass-1")
    w = client.get("/api/charts/size-bands", params={"device": dev, "kind": "weight"}).json()
    by = {(s["anatomy"], s["projection"]): s for s in w["series"]}
    assert set(by) == {("Torace", "AP"), ("Sinusuri", "")}  # Craniu states an age, not a weight
    t = by[("Torace", "AP")]
    assert t["n"] == 4 and sorted(t["prefixes"]) == ["T035", "W035"]  # the two program sets merge
    cell = t["cells"][0]
    assert w["bands"][cell["band"]]["label"] == "5-15 kg" and cell["n"] == 4 and cell["median"] == 0.025
    assert w["bands"][-1]["label"] == "no band"  # Sinusuri states none, in both views
    a = client.get("/api/charts/size-bands", params={"device": dev, "kind": "age"}).json()
    by = {(s["anatomy"], s["projection"]): s for s in a["series"]}
    assert ("Craniu", "AP") in by and ("Torace", "AP") not in by
    assert a["bands"][by[("Craniu", "AP")]["cells"][0]["band"]]["label"] == "1-6 y"
    assert w["n_other_kind"] == 3 and a["n_other_kind"] == 10  # an exam never crosses into the other size type
    assert w["n_hidden"] == 3 and ("Abdomen", "AP") not in by  # 5-20 kg is not an ICRP / RP 185 band: hidden by default
    k = client.get("/api/charts/size-bands", params={"device": dev, "kind": "weight", "other": "keep"}).json()
    ab = next(x for x in k["series"] if x["anatomy"] == "Abdomen")
    assert k["bands"][ab["cells"][0]["band"]]["label"] == "5-20 kg" and k["n_hidden"] == 0
    rb = client.get("/api/charts/size-bands", params={"device": dev, "kind": "weight", "other": "reband"}).json()
    ab = next(x for x in rb["series"] if x["anatomy"] == "Abdomen")  # no patient weight here: no band
    assert rb["bands"][ab["cells"][0]["band"]]["label"] == "no band" and rb["n_hidden"] == 0
    assert client.get("/api/charts/size-bands", params={"device": dev, "other": "x"}).status_code == 400
    assert client.get("/api/charts/size-bands", params={"device": dev, "kind": "x"}).status_code == 400
    assert client.get("/api/charts/size-bands", params={"device": 999999}).status_code == 400


def test_dx_rows_carry_the_cross_device_group(client):
    login(client, "viewer", "viewer-pass-1")
    rows = client.get("/api/stats/DX/rows").json()
    t = next(r for r in rows if r["protocol"] == "W035 Torace a.p. 10kg")
    # no band in the name and no patient weight: anatomy and projection only
    assert (t["anatomy"], t["projection"], t["group"]) == ("Toracică", "AP", "Toracică AP")
    assert (t["band_kind"], t["band_lo"], t["band_hi"]) == ("none", None, None)
    # numbers for sorting the bands ("5-15 kg" before "15-30 kg"); an open end is null, not infinity
    banded = [r for r in rows if r.get("band_kind") == "weight" and r["band_hi"] is None]
    assert all(r["band"].startswith(">") for r in banded)


def test_chart_rows_apply_every_filter(client):
    """The browser chart panels follow the whole query, like the statistics panels beside them:
    a filter they used to drop (acc:, dlp:, flag: …) made them chart a wider population."""
    login(client, "viewer", "viewer-pass-1")
    rows = client.get("/api/stats/DX/rows").json()
    studies = client.get("/api/studies", params={"q": "modality:DX,CR", "size": 500}).json()["rows"]
    one = next(x for x in studies if x["accession"] and x["n_events"])
    only = client.get("/api/stats/DX/rows", params={"q": f"acc:{one['accession']}"}).json()
    assert len(only) == one["n_events"]  # one row per exposure of that study
    rest = client.get("/api/stats/DX/rows", params={"q": f"-acc:{one['accession']}"}).json()
    assert len(rest) == len(rows) - len(only)
    assert client.get("/api/stats/DX/rows", params={"q": "acc:NO-SUCH-ACC"}).json() == []
    assert client.get("/api/stats/ct/acquisitions", params={"q": "acc:NO-SUCH-ACC"}).json() == []
    # a filter that does not parse is refused, not dropped
    assert client.get("/api/stats/DX/rows", params={"q": "dlp:lots"}).status_code == 400


def test_size_bands_several_devices(client):
    url = str(client.app.state.settings.database_url)
    ids = []
    with make_sessionmaker(make_engine(url))() as s:
        for i, nm in enumerate(["Ysio A", "Luminos B"]):
            d = Device(display_name=nm, model_name="X", identity_key=f"k-multi-{i}", is_local=True)
            s.add(d)
            s.flush()
            ids.append(d.id)
            for j, dap in enumerate([0.01 * (i + 1), 0.02 * (i + 1), 0.03 * (i + 1)]):
                s.add(Study(study_uid=f"6.6.{i}.{j}", accession=f"M{i}{j}", modality="DX", device_id=d.id,
                            study_datetime=NOW - dt.timedelta(days=1), source_kind="test", n_events=1,
                            events=[Event(protocol="W035 Torace a.p. 5-15Kg" if i == 0 else "T035 Thorax a.p. 5-15Kg", dap=dap)]))
        s.flush()
        classify.run(s, list(s.scalars(select(Study.id).where(Study.device_id.in_(ids)))))  # Torace = Thorax
        s.commit()
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/charts/size-bands", params={"device": f"{ids[0]},{ids[1]}", "kind": "weight"}).json()
    assert [d["name"] for d in r["devices"]] == ["Ysio A", "Luminos B"]
    by = {(s["device"], s["anatomy"], s["projection"]): s for s in r["series"]}
    assert set(by) == {("Ysio A", "Toracică", "AP"), ("Luminos B", "Toracică", "AP")}  # one anatomy, two units
    assert len({s["device_id"] for s in r["series"]}) == 2
    assert r["anatomies"] and r["anatomies"][0]["n"] == 6  # the picker's list: the same anatomy on both units
    one = client.get("/api/charts/size-bands", params={"device": ids[1]}).json()
    assert [d["name"] for d in one["devices"]] == ["Luminos B"] and one["device"]["id"] == ids[1]
    assert client.get("/api/charts/size-bands", params={"device": "x"}).status_code == 400
    assert client.get("/api/charts/size-bands", params={"device": f"{ids[0]},999999"}).status_code == 400


def test_maintenance_endpoints(client, tmp_path, monkeypatch):
    from dozimetrix import jobs

    lock = tmp_path / "maintain.lock"
    monkeypatch.setattr(jobs, "lock_path", lambda _d: lock)
    spawned = []
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/maintenance").status_code == 403
    assert client.post("/api/maintenance/maintain", json={}).status_code == 403
    login(client, "boss", "boss-pass-123")
    got = client.get("/api/maintenance").json()
    assert [j["name"] for j in got["jobs"]] == ["maintain", "classify", "recompute"]
    assert got["current"] is None
    assert client.post("/api/maintenance/nope", json={}).status_code == 404
    assert client.post("/api/maintenance/maintain", json={"days": 0}).status_code == 422
    r = client.post("/api/maintenance/classify", json={"days": 30})
    assert r.status_code == 200 and r.json()["state"] == "running"
    cmd = spawned[-1]
    assert cmd[cmd.index("classify"):cmd.index("classify") + 3] == ["classify", "--days", "30"]
    assert cmd[cmd.index("--by") + 1] == "boss"
    assert client.get("/api/maintenance").json()["current"]["job"] == "classify"
    # recompute takes no day limit even if one is sent
    client.post("/api/maintenance/recompute", json={"days": 9})
    assert "--days" not in spawned[-1]
    with jobs.locked(lock):  # a job holds the lock: refused
        assert client.post("/api/maintenance/maintain", json={}).status_code == 409


def test_host_ips_are_kept_for_a_minute(monkeypatch):
    from dozimetrix.api import app as app_mod

    calls = []
    monkeypatch.setattr(app_mod, "_lookup_host_ips", lambda: calls.append(1) or ["10.0.0.5"])
    monkeypatch.setattr(app_mod, "_host_ips_seen", None)
    assert app_mod._host_ips() == ["10.0.0.5"] and app_mod._host_ips() == ["10.0.0.5"]
    assert len(calls) == 1
    app_mod._host_ips_seen = (app_mod._host_ips_seen[0] - 61, app_mod._host_ips_seen[1])  # a minute later
    app_mod._host_ips()
    assert len(calls) == 2


def test_page_html_is_read_again_only_when_the_file_changes(client, monkeypatch):
    from pathlib import Path

    reads = []
    real = Path.read_text

    def counting(self, *a, **kw):
        if self.name == "studies.html":
            reads.append(1)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", counting)
    login(client, "viewer", "viewer-pass-1")
    for _ in range(3):
        assert client.get("/studies.html").status_code == 200
    assert len(reads) <= 1  # at most the first request reads the file


def test_heavy_endpoints_reuse_their_answer_until_something_changes(client, monkeypatch):
    from dozimetrix import jobs  # noqa: F401 - app import order
    from dozimetrix.api import data

    calls = []
    real = data.group_rows
    monkeypatch.setattr(data, "group_rows", lambda *a, **k: calls.append(1) or real(*a, **k))
    client.app.state.result_cache.clear()
    login(client, "viewer", "viewer-pass-1")
    first = client.get("/api/stats/DX/rows", params={"q": "modality:DX"}).json()
    again = client.get("/api/stats/DX/rows", params={"q": "modality:DX"}).json()
    assert first == again and len(calls) == 1
    client.get("/api/stats/DX/rows", params={"q": "modality:DX date:90d"})  # another query
    assert len(calls) == 2
    login(client, "boss", "boss-pass-123")  # a login is audited: the answers are recomputed
    client.get("/api/stats/DX/rows", params={"q": "modality:DX"})
    assert len(calls) == 3


def test_local_drl_refuses_a_date_in_the_selection(client):
    """Local DRLs take their period from start/end (recorded with the set): a date: in the
    selection would narrow it without the set saying so."""
    login(client, "viewer", "viewer-pass-1")
    p = {"start": "2025-01-01", "end": "2025-12-31"}
    assert client.get("/api/drl/local/propose", params={**p, "q": "origin:local"}).status_code == 200
    r = client.get("/api/drl/local/propose", params={**p, "q": "origin:local date:30d"})
    assert r.status_code == 400 and "From and To" in r.json()["error"]
    r = client.get("/api/drl/local/review", params={**p, "set": "x", "q": "date:30d"})
    assert r.status_code == 400
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/drl/local", json={"name": "t", "start": "2025-01-01", "end": "2025-12-31",
                                            "q": "date:2025-06-01..", "rows": []})
    assert r.status_code == 400


def test_study_reanalyze_is_admin_only(client):
    login(client, "viewer", "viewer-pass-1")
    body = client.get("/api/studies").json()
    sid = (body["rows"] if isinstance(body, dict) else body)[0]["id"]
    assert client.post(f"/api/studies/{sid}/reanalyze").status_code == 403
    assert client.post(f"/api/studies/{sid}/refetch").status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/studies/999999/reanalyze").status_code == 404
    assert client.post("/api/studies/999999/refetch").status_code == 404
    r = client.post(f"/api/studies/{sid}/reanalyze")
    assert r.status_code == 200 and r.json() == {"studies": 1}
