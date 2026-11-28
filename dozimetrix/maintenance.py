"""Maintenance tasks: recompute derived study/device columns from events and studies, and the
periodic run (``dozimetrix maintain``, a systemd timer): reclassify, refill the stored bands,
refresh derived values, retry failed ones."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time

from sqlalchemy import and_, bindparam, func, or_, select, update
from sqlalchemy.orm import Session

from .db.models import AppSetting, Device, Event, IngestedObject, Patient, Study
from .hybrid import NM_MODALITIES
from .security import pseudonym


def recompute_ct_columns(session: Session, study_ids: list[int] | None = None) -> int:
    """Per-study CT event counts and max CTDIvol (excluding localizers)."""
    ev = Event.__table__

    def count(kind):
        return (select(func.count()).select_from(ev)
                .where(and_(ev.c.study_id == Study.id, ev.c.event_type == kind))
                .scalar_subquery())

    stmt = update(Study).values(
        n_spiral=count("spiral"),
        n_axial=count("axial"),
        n_localizer=(select(func.count()).select_from(ev)
                     .where(and_(ev.c.study_id == Study.id, ev.c.is_localizer.is_(True)))
                     .scalar_subquery()),
        ctdivol_max=(select(func.max(ev.c.ctdivol))
                     .where(and_(ev.c.study_id == Study.id, ev.c.is_localizer.is_(False)))
                     .scalar_subquery()),
    ).where(Study.modality.in_(("CT", *NM_MODALITIES)))
    if study_ids is not None:
        stmt = stmt.where(Study.id.in_(study_ids))
    return session.execute(stmt, execution_options={"synchronize_session": False}).rowcount


PET_SOP = {"1.2.840.10008.5.1.4.1.1.128", "1.2.840.10008.5.1.4.1.1.128.1",
           "1.2.840.10008.5.1.4.1.1.130"}
NM_SOP = "1.2.840.10008.5.1.4.1.1.20"
PET_HALF_LIFE_S = 3 * 3600  # F-18 110 min, Ga-68 68 min, C-11, N-13, Rb-82; Tc-99m is 6 h


def relabel_hybrid(session: Session) -> int:
    """A CT study with a radiopharmaceutical administration is a PET/CT or SPECT/CT whose
    CT dose report came first: store it under PT / NM, as if the images had come first.
    PET or NM from the images received, else from the radionuclide's half-life."""
    ids = list(session.scalars(select(Study.id).where(
        Study.modality == "CT",
        select(Event.id).where(Event.study_id == Study.id,
                               Event.event_type == "radiopharmaceutical").exists())))
    sops: dict[int, set] = {}
    half: dict[int, list[float]] = {}
    for i in range(0, len(ids), 1000):
        part = ids[i:i + 1000]
        for sid, sop in session.execute(select(IngestedObject.study_id, IngestedObject.sop_class_uid)
                                        .where(IngestedObject.study_id.in_(part)).distinct()):
            sops.setdefault(sid, set()).add(sop)
        for sid, extra in session.execute(select(Event.study_id, Event.extra_json).where(
                Event.study_id.in_(part), Event.event_type == "radiopharmaceutical")):
            if (extra or {}).get("half_life_s"):
                half.setdefault(sid, []).append(extra["half_life_s"])
    for sid in ids:
        got = sops.get(sid, set())
        if got & PET_SOP:
            mod = "PT"
        elif NM_SOP in got:
            mod = "NM"
        else:
            hl = half.get(sid)
            mod = "PT" if hl and min(hl) < PET_HALF_LIFE_S else "NM"
        session.execute(update(Study).where(Study.id == sid).values(modality=mod),
                        execution_options={"synchronize_session": False})
    return len(ids)


def settle_administrations(session: Session) -> int:
    """One administration seen in two studies of a patient counts once (ingest/store.py)."""
    from .ingest.store import settle_administrations as settle

    rp = select(Event.study_id).where(Event.event_type == "radiopharmaceutical")
    patients = session.scalars(
        select(Study.patient_id).where(Study.id.in_(rp), Study.patient_id.is_not(None))
        .group_by(Study.patient_id).having(func.count() > 1))
    changed: set[int] = set()
    for pid in list(patients):
        for s in session.scalars(select(Study).where(Study.patient_id == pid,
                                                     Study.id.in_(rp))):
            changed.update(settle(session, s))
    return len(changed)


def recompute_device_stats(session: Session) -> int:
    st = Study.__table__
    stmt = update(Device).values(
        n_studies=select(func.count()).select_from(st)
        .where(st.c.device_id == Device.id).scalar_subquery(),
        first_study_at=select(func.min(st.c.study_datetime))
        .where(st.c.device_id == Device.id).scalar_subquery(),
        last_study_at=select(func.max(st.c.study_datetime))
        .where(st.c.device_id == Device.id).scalar_subquery(),
    )
    return session.execute(stmt, execution_options={"synchronize_session": False}).rowcount


def assign_pseudonyms(session: Session, secret: bytes, batch: int = 5000) -> int:
    """Give every patient without one a keyed pseudonym. Collisions raise (unique index)."""
    rows = session.execute(select(Patient.id, Patient.legacy_sha256)
                           .where(Patient.pseudo_id.is_(None))).all()
    stmt = update(Patient.__table__).where(Patient.__table__.c.id == bindparam("pid"))
    params = [{"pid": pid, "pseudo_id": pseudonym(secret, h or f"internal:{pid}")}
              for pid, h in rows]
    for i in range(0, len(params), batch):
        session.connection().execute(stmt, params[i:i + batch])
    return len(params)


def zero_doses_as_missing(session: Session) -> int:
    """Events stored with DAP, Ka,r or SID exactly 0 (the unit reported 0, not a
    measurement): set them to missing, note it in extra_json, recompute study totals."""
    from .ingest.store import ZERO_IS_MISSING, update_study_totals

    cols = [getattr(Event, k) for k in ZERO_IS_MISSING]
    events = list(session.scalars(select(Event).where(or_(*[c == 0 for c in cols]))))
    for e in events:
        zeros = [k for k in ZERO_IS_MISSING if getattr(e, k) == 0]
        for k in zeros:
            setattr(e, k, None)
        e.extra_json = {**(e.extra_json or {}), "reported_zero": zeros}
    session.flush()
    ids = sorted({e.study_id for e in events})
    for sid in ids:
        update_study_totals(session, session.get(Study, sid))
    return len(ids)


def recompute_all(session: Session, secret: bytes | None = None) -> list[str]:
    out = [f"studies with zero doses set to missing: {zero_doses_as_missing(session)}",
           f"PET/CT and SPECT/CT studies filed as CT, relabelled: {relabel_hybrid(session)}",
           f"CT studies updated: {recompute_ct_columns(session)}",
           f"studies sharing an administration with another: "
           f"{settle_administrations(session)}",
           f"devices updated: {recompute_device_stats(session)}"]
    from . import ssde

    out.append(f"CT series with SSDE calculated from Dw: {ssde.update(session)}")
    from . import esak

    out.append(f"X-ray exposures with ESAK calculated (annex 2): {esak.update(session)}")
    from . import indications

    out.append(f"clinical indications: {dict(indications.update(session))}")
    from . import rxgroup

    out.append(f"radiography exposures with their bands stored: {dict(rxgroup.update(session))}")
    if secret is not None:
        out.append(f"pseudonyms assigned: {assign_pseudonyms(session, secret)}")
    return out


def forget_studies(session: Session, study_ids: list[int]) -> dict:
    """Remove studies so that query/retrieve fetches them again: their events,
    classifications, alerts and import records go too, and their days lose the
    'retrieved' mark. Used to repair studies imported with a parser bug."""
    from sqlalchemy import delete

    from .db.models import Alert, AppSetting, Classification, IngestedObject
    from .net.qr import DONE_KEY

    ids = sorted(set(study_ids))
    days = {d.date().isoformat() for d in session.scalars(
        select(Study.study_datetime).where(Study.id.in_(ids))) if d}
    out = {"studies": len(ids), "days": sorted(days)}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        session.execute(delete(Alert).where(Alert.study_id.in_(chunk)))
        session.execute(delete(Classification).where(Classification.study_id.in_(chunk)))
        out["objects"] = out.get("objects", 0) + session.execute(
            delete(IngestedObject).where(IngestedObject.study_id.in_(chunk))).rowcount
        session.execute(delete(Event).where(Event.study_id.in_(chunk)))
        session.execute(delete(Study).where(Study.id.in_(chunk)))
    row = session.get(AppSetting, DONE_KEY)
    if row is not None and row.value:
        row.value = [d for d in row.value if d not in days]
    session.flush()
    recompute_device_stats(session)
    return out


def reanalyze_studies(session: Session, study_ids: list[int]) -> dict:
    """Run the whole analysis again on stored studies: exam codes, the columns derived from
    the events, then weights, indications, skin dose, alerts. Nothing is fetched."""
    from . import classify
    from .ingest.store import Helpers, derive

    ids = sorted(set(study_ids))
    h = Helpers(session).get()
    classify.run(session, ids, classifier=h.classifier)
    recompute_ct_columns(session, ids)
    derive(session, ids, h)
    session.flush()
    return {"studies": len(ids)}


DERIVE_TRIES = 5


def repair_failed_derive(session: Session, limit: int = 200, tries: int = DERIVE_TRIES) -> dict:
    """Retry the studies whose exam codes / derived values / alerts failed at import
    (``derive failed (try N)`` on their object). Success clears the mark; after ``tries``
    failures it reads ``gave up`` (still counted by ``derive_status``)."""
    import logging
    import re

    from . import classify
    from .ingest.store import DERIVE_FAILED, Helpers, derive

    pat = re.compile(rf"{DERIVE_FAILED} \(try (\d+)\)")
    rows = session.scalars(select(IngestedObject).where(
        IngestedObject.message.like(f"%{DERIVE_FAILED} (try %")).limit(limit)).all()
    by_study: dict[int, list[IngestedObject]] = {}
    for r in rows:
        if r.study_id is not None:
            by_study.setdefault(r.study_id, []).append(r)
    out = {"retried": 0, "repaired": 0, "failed": 0, "gave_up": 0}
    helpers = Helpers(session)
    for sid, objs in by_study.items():
        out["retried"] += 1
        try:
            with session.begin_nested():
                h = helpers.get()
                classify.run(session, [sid], classifier=h.classifier)
                derive(session, [sid], h)
        except Exception:  # noqa: BLE001 – counted, marked, retried later
            logging.getLogger(__name__).exception("retry of study %s failed", sid)
            for o in objs:
                n = int(pat.search(o.message).group(1)) + 1
                o.message = pat.sub(f"{DERIVE_FAILED} (gave up)" if n >= tries
                                    else f"{DERIVE_FAILED} (try {n})", o.message)
            out["gave_up" if any("gave up" in o.message for o in objs) else "failed"] += 1
            continue
        for o in objs:
            o.message = pat.sub("derived on retry", o.message)
        out["repaired"] += 1
    session.flush()
    return out


def derive_status(session: Session) -> dict:
    """Studies still waiting for a retry, and the ones given up on."""
    from .ingest.store import DERIVE_FAILED

    def n(pattern: str) -> int:
        return session.scalar(select(func.count(func.distinct(IngestedObject.study_id)))
                              .where(IngestedObject.message.like(pattern))) or 0

    return {"derive_pending": n(f"%{DERIVE_FAILED} (try %"),
            "derive_gave_up": n(f"%{DERIVE_FAILED} (gave up)%")}


# ---------------------------------------------------------------------- periodic maintenance

STATUS_KEY = "maintenance.status"
RULES_KEY = "maintenance.rules_hash"
# What decides an exposure's exam code and bands: when any of it changes, every study is
# reclassified and its bands refilled (a rule edit then reaches the old studies by itself).
RULE_FILES = ("classify_rules.toml", "crosswalk.csv", "group_schemes.toml", "group_grid.toml",
              "codes/ro-nom-rx.csv", "codes/ro-nom-ct.csv", "codes/ro-nom-rf.csv")


def rules_fingerprint() -> str:
    from .seeds.loader import SEEDS

    h = hashlib.sha256()
    for name in RULE_FILES:
        f = SEEDS / name
        h.update(name.encode())
        h.update(f.read_bytes() if f.exists() else b"-")
    return h.hexdigest()[:16]


def _recent_ids(session: Session, days: int) -> list[int]:
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    return list(session.scalars(select(Study.id).where(Study.study_datetime >= cutoff)
                                .order_by(Study.id)))


def run_periodic(maker, days: int = 90, full: bool = False, progress=None) -> dict:
    """The tasks, each in its own transaction (one failing does not stop the others):

    1. **reclassify** exam codes: the studies of the last ``days`` days, or all of them when
       asked (``full``) or when the rules, crosswalk, vocabularies or group schemes changed
       since the last run;
    2. **bands**: refill the stored anatomy / projection / size bands (``exposure_band``)
       for the same studies, after the codes;
    3. **indications** of those studies (the HIS may have added requests);
    4. **retry** the studies whose derive step failed at import;
    5. **device counters** (studies, first and last);
    6. **review counts**: the coverage figures and unclassified texts of Admin → Protocol
       mappings, which the page reads from storage (counting them takes 10 s).

    Returns ``{"mode", "tasks": {name: {"ok", "detail", "seconds"}}, ...}`` and records it in
    ``app_setting`` ``maintenance.status`` (Admin pages can show it)."""
    from . import classify, indications, rxgroup

    say = progress or (lambda _m: None)
    fingerprint = rules_fingerprint()
    with maker() as session:
        row = session.get(AppSetting, RULES_KEY)
        changed = row is None or (row.value or {}).get("hash") != fingerprint
    mode = "full" if (full or changed) else f"last {days} days"
    started = time.monotonic()
    result: dict = {"mode": mode, "rules_changed": changed, "tasks": {}}
    ids: list[int] | None = None

    def task(name, fn):
        t = time.monotonic()
        try:
            with maker() as session:
                detail = fn(session)
                session.commit()
            result["tasks"][name] = {"ok": True, "detail": detail, "seconds": round(time.monotonic() - t, 1)}
        except Exception as e:  # noqa: BLE001 – recorded; the other tasks still run
            logging.getLogger(__name__).exception("maintenance task %s failed", name)
            result["tasks"][name] = {"ok": False, "detail": f"{type(e).__name__}: {e}",
                                     "seconds": round(time.monotonic() - t, 1)}
        say(f"{name}: {result['tasks'][name]}")

    def scope(session):
        return None if mode == "full" else _recent_ids(session, days)

    def reclassify(session):
        nonlocal ids
        ids = scope(session)
        return dict(classify.run(session, ids))

    def bands(session):
        return dict(rxgroup.update(session, ids, progress=say))

    def indic(session):
        return dict(indications.update(session, ids))

    def retry(session):
        return repair_failed_derive(session, limit=500)

    def devices(session):
        return {"devices": recompute_device_stats(session)}

    def review_counts(session):
        from .api import review

        return review.refresh(session)

    task("reclassify", reclassify)
    if result["tasks"]["reclassify"]["ok"]:
        task("bands", bands)
    else:
        result["tasks"]["bands"] = {"ok": False, "detail": "skipped: reclassify failed", "seconds": 0}
    task("indications", indic)
    task("retry-derive", retry)
    task("device-counters", devices)
    task("review-counts", review_counts)
    result["ok"] = all(t["ok"] for t in result["tasks"].values())
    result["seconds"] = round(time.monotonic() - started, 1)
    with maker() as session:
        if result["ok"] and changed:  # remembered only when everything it covers ran
            r = session.get(AppSetting, RULES_KEY) or AppSetting(key=RULES_KEY)
            r.value = {"hash": fingerprint}
            session.add(r)
        st = session.get(AppSetting, STATUS_KEY) or AppSetting(key=STATUS_KEY)
        st.value = {**result, "at": dt.datetime.now().isoformat(timespec="seconds")}
        session.add(st)
        session.commit()
    return result
