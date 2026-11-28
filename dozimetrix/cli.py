"""`dozimetrix` command-line interface (stdlib argparse)."""

from __future__ import annotations

import argparse
import contextlib
import sys

from . import __version__
from .config import load_settings, set_config_path


def _check_recent_backup(settings, url: str, args: argparse.Namespace) -> None:
    """DDL is not transactional: warn (or refuse, with --require-backup) when a schema change is
    pending and the last good backup (written by deploy/backup/dozimetrix-backup) is old."""
    import time
    from pathlib import Path

    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    from .db.migrate import alembic_config

    if args.no_backup_check or url.startswith("sqlite"):
        return
    try:
        script = ScriptDirectory.from_config(alembic_config(url))
        with create_engine(url).connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        if current is None or current == script.get_current_head():
            return  # a fresh database, or nothing pending
    except Exception:
        return  # cannot tell: let the upgrade itself report
    marker = Path(settings.data_dir) / "backup-last-ok"
    try:
        age_h = (time.time() - int(marker.read_text().strip())) / 3600
    except (OSError, ValueError):
        age_h = None
    if age_h is not None and age_h <= args.backup_max_hours:
        return
    what = "no backup recorded" if age_h is None else f"last backup {age_h:.0f} h ago"
    msg = f"schema changes are pending and {what} ({marker})"
    if args.require_backup:
        raise SystemExit(f"refusing to migrate: {msg}. Run dozimetrix-backup first.")
    print(f"warning: {msg}. Run dozimetrix-backup first; --require-backup makes this an error.",
          file=sys.stderr)


def _cmd_migrate(args: argparse.Namespace) -> int:
    from .db.migrate import upgrade

    settings = load_settings()
    url = args.url or settings.database_url
    _check_recent_backup(settings, url, args)
    upgrade(url, args.revision)
    print(f"database at {args.revision}")
    return 0


def _cmd_seed(args: argparse.Namespace) -> int:
    from .db.session import make_engine, make_sessionmaker
    from .seeds.loader import load_all

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        report = load_all(session)
        session.commit()
    for line in report:
        print(line)
    return 0


def _cmd_openrem_dump(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .legacy.openrem.dump import Source, dump_all

    src = Source(db=args.db, ssh=args.ssh, sudo_postgres=not args.no_sudo)
    dump_all(src, Path(args.out), only=args.only)
    return 0


def _cmd_openrem_import(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .db.session import make_engine, make_sessionmaker
    from .legacy.openrem.importer import OpenREMImporter
    from .legacy.openrem.reconcile import reconcile, reconcile_by_year

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        secret = _secret()
        stats = OpenREMImporter(session, Path(args.dump), args.institution, secret).run()
        session.commit()
        for line in stats.lines():
            print(line)
        from . import growth

        print("weights:", growth.estimate_weights(session), growth.apply_exclusions(session))
        session.commit()
        detail, _ = reconcile(session, Path(args.dump))
        lines, ok = reconcile_by_year(session, Path(args.dump))
    print("\nPer modality/year (informational; OpenREM CR/DX copies are relabelled):")
    print("\n".join(detail))
    print("\nPer year (authoritative):")
    print("\n".join(lines))
    print("\nRECONCILIATION", "OK" if ok else "FAILED")
    return 0 if ok else 2


def _cmd_openrem_reconcile(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .db.session import make_engine, make_sessionmaker
    from .legacy.openrem.reconcile import reconcile_by_year

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        lines, ok = reconcile_by_year(session, Path(args.dump))
    print("\n".join(lines))
    return 0 if ok else 2


def _cmd_export(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import checked_conditions, overrides
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .export import lists

    fmt = args.format or ("xlsx" if args.output.lower().endswith(".xlsx") else "csv")
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        conds = checked_conditions(args.query, dt.datetime.now(), overrides(session))
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="export",
                             target=f"{args.level}:{fmt}",
                             detail={"q": args.query, "pid": args.pid, "file": args.output}))
        session.commit()
        data = lists.rows(session, conds, args.level, args.pid)
        if fmt == "xlsx":
            n = lists.write_xlsx(args.output, args.level, args.pid, data, meta=[
                ("Exported", dt.datetime.now().isoformat(sep=" ", timespec="seconds")),
                ("Query", args.query or "(all)"), ("Level", args.level)])
        else:
            with open(args.output, "w", encoding="utf-8-sig", newline="") as fh:
                n = lists.write_csv(fh, args.level, args.pid, data)
    print(f"{n} rows -> {args.output}")
    return 0


def _cmd_drl_dataset(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import checked_conditions, overrides
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .export import drl_dataset as ds

    fmt = args.output.rsplit(".", 1)[-1].lower()
    if fmt not in ("csv", "xlsx", "json"):
        print("the output must end in .csv, .xlsx or .json")
        return 2
    now = dt.datetime.now()
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session, open(args.output, "wb") as fh:
        conds = checked_conditions(args.query, now, overrides(session))
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="export",
                             target=f"drl-dataset:{fmt}",
                             detail={"q": args.query, "file": args.output}))
        session.commit()
        n = ds.write(fmt, fh, ds.dataset_rows(session, conds, _secret()),
                     ds.metadata(args.query, now))
    print(f"{n} rows -> {args.output}")
    return 0


def _cmd_survey_export(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import checked_conditions, overrides
    from .db.models import AuditLog, Study
    from .db.session import make_engine, make_sessionmaker
    from .export import drl_dataset as ds
    from .export import survey

    fmt = args.output.rsplit(".", 1)[-1].lower()
    if fmt not in ("csv", "xlsx", "json"):
        print("the output must end in .csv, .xlsx or .json")
        return 2
    start, end = survey.month_range(args.month)
    now = dt.datetime.now()
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session, open(args.output, "wb") as fh:
        conds = [*checked_conditions(args.query, now, overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="export",
                             target=f"survey:{args.month}:{fmt}",
                             detail={"q": args.query, "file": args.output}))
        session.commit()
        meta = {**ds.metadata(args.query, now), "dataset": "dozimetrix-paediatric-survey",
                "version": survey.SURVEY_VERSION, "month": args.month}
        n = ds.write(fmt, fh, survey.rows(session, conds, _secret()), meta, survey.DICTIONARY)
    print(f"{n} rows -> {args.output}")
    return 0


def _cmd_specs(args: argparse.Namespace) -> int:
    """List the report specs, their fields, or run one (any spec dropped in the specs folder)."""
    import datetime as dt
    import getpass

    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .specs import loader, plugins, registry, run, writers

    settings = load_settings()
    plugins.load(settings.plugins_dir)
    cat = loader.discover(settings.specs_dir)
    if args.action == "list":
        for s in cat.specs.values():
            tag = "bundled" if s.bundled else "site"
            print(f"{s.id:34} {s.kind:8} v{s.version:10} {s.country or '-':3} {tag:8} {s.label()}")
        for p in cat.problems:
            print(f"PROBLEM {p['file']}: {p['error']}", file=sys.stderr)
        return 0
    if args.action == "fields":
        from .specs import fields  # noqa: F401

        for f in sorted(registry.FIELDS.values(), key=lambda f: f.name):
            print(f"{f.name:28} {'PID ' if f.identifying else '    '}{f.unit:8} "
                  f"{' '.join(f.doc.split())}")
        print("\ntransforms: " + ", ".join(sorted(registry.TRANSFORMS)))
        return 0
    spec = cat.specs.get(args.spec)
    if spec is None:
        print(f"no spec {args.spec!r}; `dozimetrix specs list`", file=sys.stderr)
        return 2
    raw = dict(p.split("=", 1) for p in args.param or [] if "=" in p)
    fmt = args.format or (args.output.rsplit(".", 1)[-1] if args.output and "." in args.output
                          else spec.formats[0])
    engine = make_engine(args.url or settings.database_url)
    now = dt.datetime.now()
    try:
        with make_sessionmaker(engine)() as session:
            ctx = run.build(session, spec, raw, secret=_secret(), now=now,
                            username=f"cli:{getpass.getuser()}")
            result = run.run(ctx)
            session.add(AuditLog(username=ctx.username, action="report",
                                 target=f"spec:{spec.id}@{spec.version}:{fmt}",
                                 detail={"params": raw, "pid": run.needs_pid(spec),
                                         "file": args.output}))
            session.commit()
            if not args.output:
                import json

                print(json.dumps(writers.as_json(result, ctx), ensure_ascii=False, default=str,
                                 indent=1))
                return 0
            with open(args.output, "wb") as fh:
                run.write(result, fmt, fh, ctx)
    except loader.SpecError as e:
        print(f"{spec.id}: {e}", file=sys.stderr)
        return 2
    n = sum(len(s.rows) for b in result.books for s in b.sheets)
    print(f"{spec.id}: {len(result.books)} workbook(s), {n} rows -> {args.output}")
    return 0


def _cmd_esak_curve(args: argparse.Namespace) -> int:
    """The tube output curve of a device (Order 1255/2016 annex 2): set / show / clear."""
    import csv
    import getpass

    from sqlalchemy import delete, select

    from . import esak
    from .db.models import AuditLog, Device, DeviceOutput
    from .db.session import make_engine, make_sessionmaker

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        if args.action == "show":
            names = {d.id: d.display_name or d.station_name or f"Device {d.id}"
                     for d in session.scalars(select(Device))}
            for dev, (pts, fdd) in esak.curves(session).items():
                print(f"{dev} {names.get(dev, '?')}: FDD {fdd:g} cm, "
                      + ", ".join(f"{k:g} kV {y:g}" for k, y in pts) + " mGy/mAs")
            return 0
        if args.device is None or session.get(Device, args.device) is None:
            print("--device is the id of an existing device", file=sys.stderr)
            return 2
        dev = session.get(Device, args.device)
        if dev.merged_into_id:
            print(f"device {dev.id} is merged into {dev.merged_into_id}: use that one",
                  file=sys.stderr)
            return 2
        points: list[tuple[float, float]] = []
        if args.action == "set":
            if not args.file:
                print("set needs --file kvp,mgy_per_mas (one point per line)", file=sys.stderr)
                return 2
            with open(args.file, newline="") as fh:
                seen = 0
                for n, row in enumerate(csv.reader(fh), 1):
                    if not any(c.strip() for c in row):
                        continue  # a blank line
                    seen += 1
                    try:
                        points.append((float(row[0]), float(row[1])))
                    except (ValueError, IndexError):
                        if seen > 1:  # only the first line may be a header
                            print(f"{args.file}, line {n}: not 'kvp,mgy_per_mas': {row}",
                                  file=sys.stderr)
                            return 2
            if len(points) < 2 or any(y <= 0 for _, y in points):
                print("at least two points, with a positive output", file=sys.stderr)
                return 2
        session.execute(delete(DeviceOutput).where(DeviceOutput.device_id == dev.id))
        session.add_all(DeviceOutput(device_id=dev.id, kvp=k, mgy_per_mas=y, fdd_cm=args.fdd)
                        for k, y in points)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="config",
                             target=f"esak-curve:{args.action}:{dev.id}",
                             detail={"points": len(points), "fdd_cm": args.fdd}))
        session.commit()
        print(f"device {dev.id}: {len(points)} points"
              + (" (run `recompute` to calculate ESAK)" if points else " removed"))
    return 0


def _cmd_effective_dose(args: argparse.Namespace) -> int:
    """Effective dose coefficients (mSv/MBq) for nuclear medicine: set / show / clear."""
    import csv
    import getpass

    from sqlalchemy import delete, select

    from .db.models import AuditLog
    from .db.models import EffectiveDoseCoefficient as C
    from .db.session import make_engine, make_sessionmaker

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        if args.action == "show":
            for c in session.scalars(select(C).order_by(C.nuclide, C.form, C.age_to_years)):
                age = "adult" if c.age_to_years is None else f"to {c.age_to_years:g} y"
                print(f"{c.nuclide} / {c.form} ({age}): {c.msv_per_mbq:g} mSv/MBq  [{c.source}]")
            return 0
        rows: list[C] = []
        if args.action == "set":
            if not args.file:
                print("set needs --file: nuclide,form,age_to_years,msv_per_mbq,source",
                      file=sys.stderr)
                return 2
            with open(args.file, newline="", encoding="utf-8") as fh:
                seen = 0
                for n, r in enumerate(csv.reader(fh), 1):
                    if not any(c.strip() for c in r):
                        continue  # a blank line
                    seen += 1
                    try:
                        age = float(r[2]) if r[2].strip() else None
                        rows.append(C(nuclide=r[0].strip(), form=r[1].strip(), age_to_years=age,
                                      msv_per_mbq=float(r[3]), source=r[4].strip()))
                    except (ValueError, IndexError):
                        if seen > 1:  # only the first line may be a header
                            print(f"{args.file}, line {n}: not "
                                  f"'nuclide,form,age_to_years,msv_per_mbq,source': {r}",
                                  file=sys.stderr)
                            return 2
            if not rows or any(c.msv_per_mbq <= 0 or not c.source for c in rows):
                print("each row needs a positive coefficient and its source", file=sys.stderr)
                return 2
        session.execute(delete(C))
        session.add_all(rows)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="config",
                             target=f"effective-dose:{args.action}", detail={"rows": len(rows)}))
        session.commit()
        print(f"{len(rows)} coefficients" if rows else "coefficients removed")
    return 0


def _cmd_ct_kfactor(args: argparse.Namespace) -> int:
    import csv
    import getpass

    from . import ctdose
    from .db.models import AuditLog, CtKFactor
    from .db.session import make_engine, make_sessionmaker

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        if args.action == "show":
            from sqlalchemy import select

            for c in session.scalars(select(CtKFactor).order_by(
                    CtKFactor.region, CtKFactor.phantom_cm, CtKFactor.age_to_years)):
                age = "adult" if c.age_to_years is None else f"to {c.age_to_years:g} y"
                print(f"{c.region} ({age}, {c.phantom_cm} cm): {c.k:g} mSv/(mGy·cm)  [{c.source}]")
            return 0
        rows = []
        if args.action == "set":
            if not args.file:
                print("set needs --file: region,age_to_years,phantom_cm,k,source", file=sys.stderr)
                return 2
            with open(args.file, newline="", encoding="utf-8") as fh:
                for n, r in enumerate(csv.reader(fh), 1):
                    if not any(c.strip() for c in r):
                        continue
                    try:
                        rows.append((r[0].strip(), float(r[1]) if r[1].strip() else None,
                                     int(r[2]), float(r[3]), r[4].strip()))
                    except (ValueError, IndexError):
                        if n > 1:  # only the first line may be a header
                            print(f"{args.file}, line {n}: not "
                                  f"'region,age_to_years,phantom_cm,k,source': {r}",
                                  file=sys.stderr)
                            return 2
        try:
            n = ctdose.set_rows(session, rows)
        except ctdose.CtDoseError as e:
            print(e, file=sys.stderr)
            return 2
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="config",
                             target=f"ct-kfactor:{args.action}", detail={"rows": n}))
        session.commit()
        print(f"{n} coefficients" if n else "coefficients removed")
    return 0


def _cmd_centering(args: argparse.Namespace) -> int:
    """Measure the centering of CT image files (no database): to check the method on a known
    case, e.g. a phantom placed off the isocentre, before turning the measurement on."""
    import pydicom

    from . import centering

    for path in args.files:
        ds = pydicom.dcmread(path, force=True)
        m = centering.measure(ds)
        if m["measured"]:
            print(f"{path}: lateral {m['offset_x_mm']:+.1f} mm (+ left), vertical "
                  f"{m['offset_y_mm']:+.1f} mm (+ lower), body {m['body_w_mm']:.0f} × "
                  f"{m['body_h_mm']:.0f} mm, isocentre from {m['method']}")
        else:
            print(f"{path}: not measured: {m['reason']}")
    return 0


def _cmd_art12(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import checked_conditions, overrides
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .reports import art12

    now = dt.datetime.now()
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        conds = checked_conditions(args.query, now, overrides(session))
        books = art12.build(session, args.year, conds)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="report",
                             target=f"art12:{args.year}:zip",
                             detail={"q": args.query, "file": args.output, "pid": True}))
        session.commit()
    with open(args.output, "wb") as fh:
        art12.write_zip(books, fh, {"Generat": now.isoformat(sep=" ", timespec="seconds")})
    for b in art12.summary(books):
        short = [f"{s['name']} {s['patients']}/{art12.PATIENTS_PER_SHEET}" for s in b["sheets"]]
        print(f"{b['file']}: {', '.join(short) or 'no examinations'}")
    print(f"{len(books)} workbooks -> {args.output}")
    return 0


def _cmd_report_1255(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import checked_conditions, overrides
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .reports import insp1255

    now = dt.datetime.now()
    start, end = insp1255.quarter_range(args.year, args.quarter)
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        conds = checked_conditions(args.query, now, overrides(session))
        report = insp1255.build(session, conds, start, end)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="report",
                             target=f"insp1255:{args.year}Q{args.quarter}:xlsx",
                             detail={"q": args.query, "file": args.output}))
        session.commit()
    with open(args.output, "wb") as fh:
        insp1255.write_xlsx(report, fh, {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                                         "Selecție": args.query})
    for s in report["sheets"]:
        print(f"{s.title}: {sum(r['total'] for r in s.rows)} examinations in "
              f"{sum(1 for r in s.rows if r['total'])} codes")
    print(f"unclassified studies: {report['notes']['unclassified']} -> {args.output}")
    return 0


def _cmd_growth(args: argparse.Namespace) -> int:
    from . import growth

    s = load_settings()
    directory = s.data_dir / "growth"
    if args.action == "install":
        for line in growth.install(directory):
            print(line)
        print(f"installed in {directory} (see SOURCE.txt for origin and terms)")
        return 0
    if args.action == "status":
        g = growth.tables(directory)
        print(f"{directory}: " + ("installed" if g else "NOT installed: dozimetrix growth install"))
        return 0 if g else 1
    from .db.session import make_engine, make_sessionmaker

    with make_sessionmaker(make_engine(args.url or s.database_url))() as session:
        est = growth.estimate_weights(session)  # missing / implausible -> estimate
        stats = growth.apply_exclusions(session)
        session.commit()
    print("estimates:", est)
    print("DRL exclusions:", stats)
    return 0 if "error" not in stats else 1


def _cmd_cnp(args: argparse.Namespace) -> int:
    """Sex, birth date and exact ages from national IDs (offline; CNP > HIS > DICOM)."""
    import getpass

    from . import demographics, growth
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker

    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        counts, studies = demographics.backfill(session)
        if not args.apply:
            session.rollback()
            print("dry run (--apply writes):", counts)
            return 0
        for i in range(0, len(studies), 5000):
            chunk = studies[i:i + 5000]
            growth.estimate_weights(session, chunk)
            growth.apply_exclusions(session, chunk)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="cnp_backfill",
                             target="patients", detail=counts))
        session.commit()
    print(counts)
    return 0


def _cmd_his(args: argparse.Namespace) -> int:
    """HIS servers: check the login; run the lookup worker; queue the history."""
    from .db.models import HisServer
    from .db.session import make_engine, make_sessionmaker
    from .his import servers

    s = load_settings()
    secret = _secret()
    with make_sessionmaker(make_engine(args.url or s.database_url))() as session:
        if args.action == "check":
            rows = list(session.query(HisServer).order_by(HisServer.id))
            if not rows:
                print("no HIS server configured (Admin → Integration)")
                return 1
            bad = 0
            for srv in rows:
                res = servers.check(session, secret, srv.id)
                bad += not res["ok"]
                print(f"{srv.name}: {'OK' if res['ok'] else 'FAILED'} {res['ms']} ms"
                      + (f" – {res['error']}" if res["error"] else ""))
            session.commit()
            return 1 if bad else 0
        if args.action == "run":
            return _his_run(session, secret, args)
        if args.action == "backfill":
            return _his_backfill(session, args)
    return 2


def _his_backfill(session, args) -> int:
    """Queue history for the worker (it runs history only in the server's backfill hours)."""
    import datetime as dt
    import getpass

    from .db.models import AuditLog
    from .his import enrich, servers

    def day(v):
        return dt.datetime.fromisoformat(v) if v else None

    ids, counts = enrich.backfill_candidates(session, day(args.since), day(args.until),
                                             [args.device] if args.device else None)
    srv = (servers.active(session) or [None])[0]
    pause = srv.min_interval_s if srv else 2.0
    hours = len(ids) * 3 * (pause + 2) / 3600  # ~3 calls per study, ~2 s per HIS page
    print(dict(counts))
    print(f"~{hours:.0f} h of lookups at {pause:g} s between calls"
          + (f", run only in backfill hours {srv.backfill_hours}" if srv and srv.backfill_hours
             else ""))
    if not args.apply:
        print("dry run: --apply queues them")
        return 0
    n = enrich.enqueue(session, ids)
    session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="his.backfill",
                         target=f"{args.since or '…'}..{args.until or '…'}",
                         detail={**counts, "queued": n}))
    session.commit()
    print(f"queued {n} studies; the dozimetrix-his service works through them")
    return 0


def _his_run(session, secret, args) -> int:
    """The lookup worker: a batch every few seconds while there is work; heartbeat."""
    import time

    from .his import enrich, servers
    from .his.hippobridge import HisError

    totals: dict = {}

    def report(row, stats):
        """One line per study tried, always (the service log follows the worker)."""
        import datetime as dt

        d = row.detail or {}
        bits = [f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} study {row.study_id}: {row.status}"]
        if row.request_id:
            bits.append(f"request {row.request_id} ({d.get('type', '?')}, "
                        f"score {row.match_score})")
        for k in ("accession_saved", "accession_other_patient", "rejected_request",
                  "rejected_patient", "why"):
            if d.get(k):
                bits.append(f"{k.replace('_', ' ')} {d[k]}")
        if d.get("written"):
            bits.append("wrote " + ", ".join(d["written"]))
        if row.status == "pending" and row.next_try_at:
            bits.append(f"next try {row.next_try_at:%Y-%m-%d %H:%M}")
        if args.verbose and d:
            bits.append(str(d))
        print(" · ".join(bits), flush=True)

    while True:
        try:
            stats = enrich.run_batch(session, secret, limit=args.limit, dry_run=args.dry_run,
                                     report=report)
            msg = ", ".join(f"{k} {v}" for k, v in stats.items()) or "idle"
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v
            error = None
        except HisError as e:
            session.rollback()
            msg = f"{'paused' if e.kind == 'retry' else 'stopped'}: {e} (next try in 5 min)"
            error, stats = str(e), {}
        if not args.dry_run:
            servers.write_status(session, {"message": msg, "totals": totals, "error": error})
            session.commit()
        if stats and not stats.get("no_server"):
            print(f"batch: {msg}", flush=True)
        if args.once or args.dry_run:
            print(msg)
            return 1 if error else 0
        if error:
            time.sleep(300)  # credentials or configuration: wait for an admin
        elif not stats or stats.get("no_server"):
            time.sleep(60)


def _cmd_refetch(args: argparse.Namespace) -> int:
    import datetime as dt

    from sqlalchemy import select

    from .db.models import AuditLog, Study
    from .db.session import make_engine, make_sessionmaker
    from .maintenance import forget_studies

    if not args.missing and not args.device:
        print("give --missing dap|dlp or --device ID")
        return 2
    kind, col = (("ct_rdsr", Study.total_dlp) if args.missing == "dlp"
                 else ("proj_rdsr", Study.total_dap))
    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        q = select(Study.id).where(Study.source_kind == kind)
        if args.missing:
            q = q.where(col.is_(None))
        if args.device:
            from .db.models import Device

            ids_ = [args.device, *session.scalars(select(Device.id).where(
                Device.merged_into_id == args.device))]
            q = q.where(Study.device_id.in_(ids_))
        if args.since:
            q = q.where(Study.study_datetime >= dt.datetime.fromisoformat(args.since))
        ids = list(session.scalars(q))
        if not args.apply:
            what = f"without {args.missing}" if args.missing else f"of device {args.device}"
            print(f"{len(ids)} {kind} studies {what} (dry run; --apply deletes them so that "
                  "qr fetches them again)")
            return 0
        out = forget_studies(session, ids)
        import getpass

        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="refetch",
                             target=f"{kind}:{args.missing or f'device {args.device}'}",
                             detail=out))
        session.commit()
    print(f"forgot {out['studies']} studies ({out.get('objects', 0)} objects) on "
          f"{len(out['days'])} days: {', '.join(out['days'])}")
    print("run 'dozimetrix qr' over these days to fetch them again")
    return 0


def _cmd_study_refetch(args: argparse.Namespace) -> int:
    """Forget one study and retrieve it from the PACS again (started by the study page)."""
    import datetime as dt
    import getpass
    import logging

    from .db.models import AuditLog, Study
    from .db.session import make_engine, make_sessionmaker
    from .maintenance import forget_studies
    from .net import nodes, qr
    from .net.spool import Spool, SpoolWorker

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    s = load_settings()
    maker = make_sessionmaker(make_engine(args.url or s.database_url))
    with maker() as session:
        st = session.get(Study, args.study)
        if st is None:
            print("study not found")
            return 2
        uid, when = st.study_uid, st.study_datetime
        node, _ = nodes.qr_node(session, s)
        if node is None:
            print("no PACS configured: add a DICOM node (Admin > DICOM nodes) or [pacs]")
            return 2
        out = forget_studies(session, [args.study])
        session.add(AuditLog(username=args.by or f"cli:{getpass.getuser()}", action="refetch",
                             target=f"study:{args.study}", detail=out))
        session.commit()
    worker = SpoolWorker(Spool(s.spool_dir), maker, _secret(), keep=s.keep_received)

    def drain() -> int:
        total = 0
        while (n := worker.drain()):
            total += n
        return total

    stats = qr.QRStats()
    hit = qr.StudyHit(uid, (when or dt.datetime.now()).strftime("%Y%m%d"), set())
    qr.retrieve_study(node, hit, worker.spool, drain, maker, stats)
    drain()
    print(f"study {args.study} fetched again: {dict(stats.counts)}")
    return 0


def _cmd_skin(args: argparse.Namespace) -> int:
    from . import skindose
    from .db.session import make_engine, make_sessionmaker

    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        if args.study:
            r = skindose.for_study(session, args.study)
            if r is None:
                print("study not found")
                return 1
            print(f"peak skin dose {r['psd_mgy']} mGy at {r['psd_at']} (estimate), "
                  f"Ka,r total {r['kar_total_mgy']} mGy, {r['events_used']} events, "
                  f"{r['geometries']} geometries")
            for k, v in r["assumptions"].items():
                print(f"  {k}: {v}")
            return 0
        print(skindose.update_psd(session))
        session.commit()
    return 0


def _cmd_alerts(args: argparse.Namespace) -> int:
    import datetime as dt

    from . import alerts, notify
    from .db.session import make_engine, make_sessionmaker

    s = load_settings()
    engine = make_engine(args.url or s.database_url)
    with make_sessionmaker(engine)() as session:
        cfg = notify.load_settings(session, s.notify, _secret())
        if args.action == "evaluate":
            start = dt.datetime.fromisoformat(args.since) if args.since else None
            end = dt.datetime.fromisoformat(args.until) if args.until else None
            stats = alerts.evaluate_range(session, start, end, progress=print)
            print(", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
            # device rules (silent device, dose drift) as of each day of the period, or now
            dev = alerts.evaluate_devices_range(session, start or dt.datetime.now(), end,
                                                progress=print)
            session.commit()
            print("devices:", ", ".join(f"{k}: {v}" for k, v in sorted(dev.items())) or "—")
            from . import followup

            fu = followup.sync(session)  # high-dose fluoroscopy follow-ups of the history
            session.commit()
            print("follow-ups:", ", ".join(f"{k}: {v}" for k, v in sorted(fu.items())) or "—")
        elif args.action == "notify":
            stats = notify.dispatch(session, cfg)
            session.commit()
            print(dict(stats) or "nothing to send", "channels:", cfg.channels or "none")
        elif args.action == "test-notify":
            if not cfg.channels:
                print("no channel configured: set one in Admin → Alerts, or [notify] smtp_host "
                      "+ email_to / ntfy_url in the toml")
                return 1
            for ch in cfg.channels:
                try:
                    notify.send_test(cfg, ch)
                    print(f"{ch}: sent")
                except Exception as e:  # noqa: BLE001
                    print(f"{ch}: FAILED {e}")
        else:
            for a in alerts.listing(session, "open", 50):
                print(f"{a['time']}  {a['level']:5} {a['kind']:7} {a['message']}  "
                      f"[{a['title']}] study {a['study_id']}")
    return 0


def _cmd_digests(args: argparse.Namespace) -> int:
    import datetime as dt

    from . import digests, notify
    from .db.session import make_engine, make_sessionmaker

    s = load_settings()
    with make_sessionmaker(make_engine(args.url or s.database_url))() as session:
        items = digests.load(session)
        if args.action == "list":
            for d in items:
                print(f"{d['id']:20} {'on ' if d['enabled'] else 'off'} {d['name']} → "
                      f"{', '.join(d['to'])} [{d['q'] or 'all'}] "
                      f"last: {d.get('last_period') or '—'}")
            return 0
        now = dt.datetime.now()
        if args.action == "preview":
            for d in items:
                if args.only and d["id"] != args.only:
                    continue
                start, end = digests.previous_month(now)
                _, text, _ = digests.render(d["name"], digests.build(session, d["q"], start, end,
                                                                     now))
                print(text, "\n")
            return 0
        cfg = notify.load_settings(session, s.notify, _secret())
        out = digests.run_due(session, cfg, now, force=args.force, only=args.only)
        session.commit()
        print(out or "nothing due")
        return 1 if any(v.startswith("failed") for v in out.values()) else 0


def _run_job(args: argparse.Namespace, name: str, days: int | None, run) -> int:
    """Run a maintenance job under the shared lock and record its state (Admin → Maintenance
    shows it); exit 75 when another job is running."""
    import getpass

    from . import jobs
    from .db.session import make_engine, make_sessionmaker

    settings = load_settings()
    maker = make_sessionmaker(make_engine(args.url or settings.database_url))
    by = getattr(args, "by", None) or f"cli:{getpass.getuser()}"
    try:
        res = jobs.execute(maker, name, lambda say: run(maker, say), {"days": days}, by,
                           jobs.lock_path(settings.data_dir))
    except jobs.Busy as e:
        print(f"{name}: {e}")
        return 75
    return 0 if not isinstance(res, dict) or res.get("ok", True) else 1


def _cmd_classify(args: argparse.Namespace) -> int:
    from . import classify, maintenance

    def run(maker, say):
        with maker() as session:
            ids = args.study or (maintenance._recent_ids(session, args.days) if args.days else None)
            stats = classify.run(session, ids, progress=say)
            session.commit()
        say(", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
        return {"ok": True, **dict(stats)}

    return _run_job(args, "classify", args.days, run)


def _cmd_openrem_fix(args: argparse.Namespace) -> int:
    import getpass

    from .db.session import make_engine, make_sessionmaker
    from .legacy.openrem.fixes import apply_known_fixes, pending

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        if args.dump:
            from .legacy.openrem.fixes import backfill_ct_phantom

            res = backfill_ct_phantom(session, args.dump, f"cli:{getpass.getuser()}",
                                      apply=not args.dry_run)
            print(f"CT phantom from the dump{' (dry run)' if args.dry_run else ''}: {res}")
            if not args.dry_run:
                session.commit()
        if args.dry_run:
            todo = pending(session)
            for name, n in todo.items():
                print(f"{name}: {n} event(s) to correct")
            if not todo:
                print("nothing to correct")
            return 0
        for line in apply_known_fixes(session, username=f"cli:{getpass.getuser()}"):
            print(line)
        session.commit()
    return 0


def _cmd_recompute(args: argparse.Namespace) -> int:
    from .maintenance import recompute_all

    def run(maker, say):
        lines = []
        with maker() as session:
            for line in recompute_all(session, _secret()):
                say(line)
                lines.append(line)
            session.commit()
        return {"ok": True, "lines": lines}

    return _run_job(args, "recompute", None, run)


def _cmd_maintain(args: argparse.Namespace) -> int:
    from .maintenance import run_periodic

    def run(maker, say):
        res = run_periodic(maker, days=args.days, full=args.full, progress=say)
        say(f"maintenance ({res['mode']}{', rules changed' if res['rules_changed'] else ''}): "
            f"{'ok' if res['ok'] else 'FAILED'} in {res['seconds']} s")
        return res

    return _run_job(args, "maintain", None if args.full else args.days, run)


def _secret() -> bytes:
    from .security import load_or_create_secret

    return load_or_create_secret(load_settings().secret_file)


ROLES = ("viewer", "exporter", "pid", "importer", "admin")


def _cmd_user(args: argparse.Namespace) -> int:
    import getpass

    from sqlalchemy import select

    from .db.models import User
    from .db.session import make_engine, make_sessionmaker
    from .security import hash_password

    roles = [r.strip() for r in (args.roles or "viewer").split(",") if r.strip()]
    bad = [r for r in roles if r not in ROLES]
    if bad:
        print(f"unknown role(s): {', '.join(bad)}; valid: {', '.join(ROLES)}")
        return 2
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        user = session.scalar(select(User).filter_by(username=args.username))
        if args.action == "list":
            for u in session.scalars(select(User).order_by(User.username)):
                print(f"{u.username:20} {u.roles:30} {'active' if u.is_active else 'disabled'}")
            return 0
        if args.action == "disable":
            if user:
                user.is_active = False
                session.commit()
            return 0
        if args.action in ("rename", "delete"):
            from .api import users as users_mod
            from .db.models import AuditLog

            if user is None:
                print(f"no user {args.username!r}")
                return 2
            try:
                if args.action == "rename":
                    new = users_mod.valid_username(session, args.to)
                    detail = {"username": {"from": user.username, "to": new}}
                    user.username = new
                    action, target = "user.update", f"user:{new}"
                else:
                    if "admin" in user.role_set():
                        raise users_mod.UserError(
                            "an administrator cannot be removed; take away the admin role first")
                    detail = {"name": user.display_name, "roles": user.roles}
                    session.delete(user)
                    action, target = "user.delete", f"user:{args.username}"
            except users_mod.UserError as e:
                print(e)
                return 2
            session.add(AuditLog(username=f"cli:{getpass.getuser()}", action=action,
                                 target=target, detail=detail))
            session.commit()
            print(f"user {args.username}: " + (f"renamed to {args.to.strip()}"
                                               if args.action == "rename" else "removed"))
            return 0
        password = args.password or getpass.getpass(f"password for {args.username}: ")
        if len(password) < 10:
            print("password must have at least 10 characters")
            return 2
        if user is None:
            user = User(username=args.username)
            session.add(user)
        user.password_hash = hash_password(password)
        user.roles = ",".join(roles)
        user.display_name = args.name or user.display_name or args.username
        user.is_active = True
        session.commit()
        print(f"user {args.username}: roles {user.roles}")
    return 0


def _cmd_sizes(args: argparse.Namespace) -> int:
    """Patient weight / height from a CSV file (dry run unless --apply)."""
    import getpass
    import json

    from . import sizes_import
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker

    with open(args.file, encoding=args.encoding, newline="") as fh:
        text = fh.read()
    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        try:
            res = sizes_import.run(session, text, apply=args.apply)
        except sizes_import.RowError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for k, v in sorted(res["counts"].items()):
            print(f"{k}: {v}")
        for i in res["issues"][: args.show]:
            print(f"  row {i['row']}: {i['outcome']} {i['detail']}")
        if args.apply:
            session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="sizes.import",
                                 target=args.file.rsplit("/", 1)[-1],
                                 detail=json.loads(json.dumps(res["counts"]))))
            session.commit()
        else:
            print("dry run: --apply writes")
    return 0


def _cmd_rdsr(args: argparse.Namespace) -> int:
    """Write dose SRs (IHE REM) of stored studies as files. Never sends them anywhere."""
    import datetime as dt
    import getpass

    from sqlalchemy import select

    from .api.data import checked_conditions, overrides
    from .db.models import AuditLog, Event, Study
    from .db.session import make_engine, make_sessionmaker
    from .export import rdsr

    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        ids = list(args.study or [])
        if args.query is not None:
            conds = checked_conditions(args.query, dt.datetime.now(), overrides(session))
            has_events = select(Event.id).where(Event.study_id == Study.id).exists()
            ids += list(session.scalars(select(Study.id).where(*conds, has_events)
                                        .order_by(Study.study_datetime).limit(args.limit)))
        if not ids:
            print("no studies: give --study ID or --query", file=sys.stderr)
            return 2
        paths = rdsr.write_studies(session, ids, args.out, _secret(),
                                   identified=args.identified)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="export",
                             target="rdsr", detail={"q": args.query, "studies": len(ids),
                                                    "files": len(paths),
                                                    "identified": args.identified}))
        session.commit()
    print(f"{len(paths)} dose SR files -> {args.out}"
          + ("" if args.identified else " (pseudonymised)"))
    return 0


def _cmd_survey_import(args: argparse.Namespace) -> int:
    """Central collection: load survey exports (CSV / JSON) from the sites."""
    import getpass
    from pathlib import Path

    from . import aggregate
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker

    bad = 0
    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        for name in args.files:
            p = Path(name)
            try:
                up = aggregate.upload(session, p.read_bytes(), filename=p.name,
                                      user=f"cli:{getpass.getuser()}")
            except aggregate.SurveyError as e:
                session.rollback()
                print(f"{p.name}: REJECTED – {e}")
                bad += 1
                continue
            session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="survey.upload",
                                 target=f"{up.institution}:{up.period}",
                                 detail={"rows": up.n_rows, "rejected": up.n_rejected,
                                         "file": p.name}))
            session.commit()
            print(f"{p.name}: {up.institution} {up.period}: {up.n_rows} rows, "
                  f"{up.n_rejected} rejected"
                  + (f", replaced upload {up.report['replaced']}" if up.report["replaced"]
                     else ""))
    return 1 if bad else 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app

    s = load_settings()
    host, port = args.host or s.web_host, args.port or s.web_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {host}; put Dozimetrix behind an HTTPS reverse proxy")
        if not s.secure_cookies:
            print("warning: [security] secure_cookies is off; set it to true behind HTTPS")
    # access_log off: query strings may contain search terms with identifiers.
    # X-Forwarded-For/-Proto are honoured only from the configured reverse proxies.
    uvicorn.run(create_app(), host=host, port=port, access_log=False,
                proxy_headers=True, forwarded_allow_ips=",".join(s.web_trusted_proxies),
                # open live streams (/api/live) would hold a restart for minutes; browsers
                # reconnect on their own
                timeout_graceful_shutdown=5)
    return 0


def _cmd_devices_origin(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from .db.models import Device
    from .db.session import make_engine, make_sessionmaker

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        devs = list(session.scalars(select(Device).order_by(Device.n_studies.desc())))
        local = [d for d in devs if (d.n_studies or 0) >= args.min_studies]
        for d in local:
            print(f"local    {d.id:5} {d.n_studies:8} {d.manufacturer} | {d.model_name} | "
                  f"{d.station_name} | {d.display_name}")
        print(f"{len(local)} local, {len(devs) - len(local)} external "
              f"(threshold {args.min_studies} studies)")
        if args.apply:
            for d in devs:
                d.is_local = d in local
            session.commit()
            print("applied")
        else:
            print("dry run; add --apply to save")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from .db.session import make_engine, make_sessionmaker
    from .ingest.files import import_paths

    def progress(i, path, result):
        if args.verbose or result.status == "failed":
            print(f"{i:6} {result.status:10} {path} {result.message}")
        elif i % 500 == 0:
            print(f"{i} files…")

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        store = import_paths(session, args.paths, _secret(), progress=progress)
    for k, v in sorted(store.stats.items()):
        print(f"{k}: {v}")
    return 1 if store.stats.get("failed") else 0


def _cmd_receive(args: argparse.Namespace) -> int:
    import logging
    import signal
    import threading

    from .db.session import make_engine, make_sessionmaker
    from .net.scp import Receiver
    from .net.spool import Spool, SpoolWorker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    s = load_settings()
    spool = Spool(s.spool_dir)
    maker = make_sessionmaker(make_engine(args.url or s.database_url))
    worker = SpoolWorker(spool, maker, _secret(), keep=s.keep_received,
                         notify=s.notify, device_alerts=True,
                         retention_days=s.spool_retention_days)
    from .net import nodes

    with maker() as session:
        nodes.import_config(session, s)
        allowed = nodes.allowed_senders(session, s.dicom_allowed_aets)
        session.commit()
    rx = Receiver(spool, args.aet or s.dicom_aet, args.port or s.dicom_port,
                  args.bind or s.dicom_bind, allowed, max_object_mb=s.dicom_max_object_mb,
                  min_free_mb=s.spool_min_free_mb)
    worker.start()
    rx.start()
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())

    def status_loop():
        """Every 30 s: heartbeat for the UI, and the accepted senders from the database."""
        while not done.wait(30 if status_loop.ran else 0):
            status_loop.ran = True
            try:
                with maker() as session:
                    rx.set_allowed(nodes.allowed_senders(session, s.dicom_allowed_aets))
                    nodes.write_status(session, nodes.RECEIVER_KEY, {
                        "aet": rx.aet, "port": rx.port, "bind": rx.bind,
                        "received": rx.received, "by_aet": rx.by_aet,
                        "allowed": list(rx.ae.require_calling_aet), "spool": worker.stats})
                    session.commit()
            except Exception:  # noqa: BLE001 – status must never stop the receiver
                logging.getLogger("dozimetrix.receive").exception("status update failed")

    status_loop.ran = False
    threading.Thread(target=status_loop, name="dozimetrix-status", daemon=True).start()
    with contextlib.suppress(KeyboardInterrupt):
        done.wait()
    rx.stop()
    worker.stop()
    worker.join(timeout=10)
    print(f"received {rx.received}; imported {worker.stats}")
    return 0


def _cmd_qr(args: argparse.Namespace) -> int:
    import datetime as dt
    import logging

    from .db.session import make_engine, make_sessionmaker
    from .net import qr
    from .net.spool import Spool, SpoolWorker

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    s = load_settings()
    maker = make_sessionmaker(make_engine(args.url or s.database_url))
    if args.host:  # explicit command-line node
        if not args.aec:
            print("--host needs --aec (the PACS AE title)")
            return 2
        node = qr.Node(args.host, args.port or s.pacs_port, args.aec, args.aet or s.dicom_aet,
                       retrieve=args.retrieve or s.pacs_retrieve,
                       move_destination=s.pacs_move_destination,
                       max_results=s.pacs_max_results)
        node_mods = list(s.pacs_modalities)
    else:
        from .net import nodes

        with maker() as session:
            nodes.import_config(session, s)
            node, node_mods = nodes.qr_node(session, s, args.node)
            session.commit()
        if node is None:
            print(f"no query/retrieve node {args.node!r}" if args.node else
                  "no PACS configured: add a DICOM node (Admin > DICOM nodes) or [pacs]")
            return 2
        if args.aet:
            node.calling_aet = args.aet
        if args.retrieve:
            node.retrieve = args.retrieve
    mode = node.retrieve
    if mode not in ("get", "move"):
        print("retrieve mode must be 'get' or 'move'")
        return 2
    today = dt.date.today()
    if args.date_from:
        start = dt.date.fromisoformat(args.date_from)
        end = dt.date.fromisoformat(args.date_to) if args.date_to else today
    else:
        start, end = today - dt.timedelta(days=(args.days or s.qr_days) - 1), today
    worker = SpoolWorker(Spool(s.spool_dir), maker, _secret(), keep=s.keep_received)

    def drain() -> int:
        n = total = 0
        while (n := worker.drain()):
            total += n
        return total

    mods = {m.strip().upper() for m in args.modalities.split(",")} if args.modalities else None
    configured = {m.upper() for m in node_mods}
    from .net.nodes import QR_KEY, write_status

    last = [0.0]

    def report(message: str, force: bool = False) -> None:
        """Print, and every 15 s store the progress for the UI (DICOM nodes page)."""
        import time

        print(message, flush=True)
        if force or time.monotonic() - last[0] > 15:
            last[0] = time.monotonic()
            try:
                with maker() as session:
                    write_status(session, QR_KEY, {
                        "node": f"{node.aet}@{node.host}", "from": str(start), "to": str(end),
                        "message": message, "dry_run": bool(args.dry_run)})
                    session.commit()
            except Exception as e:  # noqa: BLE001 – status only; the run goes on
                import logging

                logging.getLogger("dozimetrix.qr").debug("progress not stored: %s", e)
    receiver = None
    if mode == "move" and not args.dry_run and not _listening(s.dicom_port):
        from .net.scp import Receiver

        receiver = Receiver(worker.spool, s.dicom_aet, s.dicom_port, s.dicom_bind,
                            s.dicom_allowed_aets, max_object_mb=s.dicom_max_object_mb,
                            min_free_mb=s.spool_min_free_mb)
        receiver.start()
        print(f"C-MOVE: started a receiver on port {s.dicom_port} for this run")
    with maker() as session:
        done = set() if args.redo else qr.done_days(session)
    day = end
    incomplete: list[dt.date] = []  # days with failed studies or undelivered objects
    while day >= start:  # newest first
        stats = None
        if (day.isoformat() in done and day < today - dt.timedelta(days=1)
                and not (args.staff or args.ct_dose or args.dose_pages or args.ct_headers)):
            report(f"{day}: already done")
        elif args.ct_dose:  # known NM / PET studies: their CT dose report (SR series only)
            stats = qr.QRStats()
            if not args.dry_run:
                qr.ct_dose_backfill(node, day, worker.spool, drain, maker, stats, args.pause,
                                    progress=report)
            if stats.counts:
                report(f"{day}: {dict(stats.counts)}; imported {worker.stats}", force=True)
        elif args.ct_headers:  # known CT studies with old-style header estimates
            stats = qr.QRStats()
            if not args.dry_run:
                qr.ct_header_backfill(node, day, worker.spool, drain, maker, stats, args.pause,
                                      progress=report)
            if stats.counts:
                report(f"{day}: {dict(stats.counts)}; imported {worker.stats}", force=True)
        elif args.dose_pages:  # known Toshiba CT studies: the console's dose pages
            stats = qr.QRStats()
            if not args.dry_run:
                qr.dose_page_backfill(node, day, worker.spool, drain, maker, stats, args.pause,
                                      progress=report)
            if stats.counts:
                report(f"{day}: {dict(stats.counts)}; imported {worker.stats}", force=True)
        elif args.staff:  # known CT studies: one image header each for the staff names
            stats = qr.QRStats()
            if not args.dry_run:
                qr.staff_backfill(node, day, worker.spool, drain, maker, stats, args.pause,
                                  progress=report)
            report(f"{day}: {dict(stats.counts)}; imported {worker.stats}", force=True)
        else:
            stats = qr.QRStats()
            qr.run_day(node, day, worker.spool, drain, maker, stats, mods or configured,
                       args.dry_run,
                       args.pause, progress=report,
                       workers=args.workers)
            report(f"{day}: {dict(stats.counts)}; imported {worker.stats}", force=True)
            # a day is finished only after a full run (no modality subset) in which no study
            # failed and the PACS delivered everything it announced
            if not args.dry_run and mods is None and not stats.incomplete:
                with maker() as session:
                    qr.mark_day_done(session, day)
        if stats is not None and not args.dry_run and stats.incomplete:
            incomplete.append(day)
        day -= dt.timedelta(days=1)
    if receiver is not None:
        receiver.stop()
    report(f"finished {start}..{end}", force=True)
    if incomplete:  # a timer unit must not look healthy after an incomplete run
        print(f"incomplete days (failed studies or objects not delivered): "
              f"{', '.join(d.isoformat() for d in sorted(incomplete))}", file=sys.stderr)
        return 1
    return 0


def _listening(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _cmd_info(_args: argparse.Namespace) -> int:
    s = load_settings()
    print(f"Dozimetrix {__version__}")
    print(f"config file : {s.source or '(none)'}")
    print(f"database    : {_redact(s.database_url)}")
    return 0


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dozimetrix", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", metavar="PATH", help="the configuration file (default: "
                   "./dozimetrix.toml, ~/.config/dozimetrix/dozimetrix.toml, "
                   "/etc/dozimetrix/dozimetrix.toml)")
    p.add_argument("--pidfile", metavar="PATH",
                   help="write the process id here (removed on exit)")
    p.add_argument("--log-file", metavar="PATH",
                   help="append output and log records to this file instead of the terminal")
    p.add_argument("--log-level", metavar="LEVEL", type=str.upper,
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="log level (default: the command's own, INFO or WARNING)")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="create/upgrade the database schema")
    m.add_argument("--url", help="database URL (default: from settings)")
    m.add_argument("--revision", default="head")
    m.add_argument("--require-backup", action="store_true",
                   help="refuse to change the schema unless a backup is recent")
    m.add_argument("--backup-max-hours", type=float, default=26.0,
                   help="how old the last backup may be (default 26)")
    m.add_argument("--no-backup-check", action="store_true", help="skip the backup check")
    m.set_defaults(func=_cmd_migrate)

    s = sub.add_parser("seed", help="load/refresh bundled vocabularies, group schemes, DRL sets")
    s.add_argument("--url")
    s.set_defaults(func=_cmd_seed)

    d = sub.add_parser("openrem-dump", help="dump an OpenREM database to CSV (read-only)")
    d.add_argument("--db", default="openremdb", help="PostgreSQL database name")
    d.add_argument("--ssh", help="run psql on this host via ssh, e.g. root@server")
    d.add_argument("--no-sudo", action="store_true", help="do not run psql as 'postgres'")
    d.add_argument("--out", default="var/openrem-dump", help="output directory")
    d.add_argument("--only", nargs="*", help="dump only these parts")
    d.set_defaults(func=_cmd_openrem_dump)

    im = sub.add_parser("openrem-import", help="import an OpenREM dump, then reconcile")
    im.add_argument("dump", nargs="?", default="var/openrem-dump")
    im.add_argument("--url")
    im.add_argument("--institution", default="Default", help="name of the local institution")
    im.set_defaults(func=_cmd_openrem_import)

    rc = sub.add_parser("openrem-reconcile", help="compare imported data with the dump summary")
    rc.add_argument("dump", nargs="?", default="var/openrem-dump")
    rc.add_argument("--url")
    rc.set_defaults(func=_cmd_openrem_reconcile)

    ex = sub.add_parser("export", help="export studies or events as CSV or XLSX")
    ex.add_argument("-q", "--query", default="", help='query language, e.g. "modality:CT date:30d"')
    ex.add_argument("--level", choices=["study", "event"], default="study")
    ex.add_argument("--format", choices=["csv", "xlsx"], help="default: from the file name")
    ex.add_argument("--pid", action="store_true", help="include patient identifiers")
    ex.add_argument("-o", "--output", required=True)
    ex.add_argument("--url")
    ex.set_defaults(func=_cmd_export)

    dd = sub.add_parser("drl-dataset", help="export the DRL dataset (csv / xlsx / json)")
    dd.add_argument("-q", "--query", default="origin:local")
    dd.add_argument("-o", "--output", required=True, help="the extension sets the format")
    dd.add_argument("--url")
    dd.set_defaults(func=_cmd_drl_dataset)

    sv2 = sub.add_parser("survey-export", help="anonymised paediatric survey export (one month)")
    sv2.add_argument("--month", required=True, help="YYYY-MM")
    sv2.add_argument("-q", "--query", default="origin:local")
    sv2.add_argument("-o", "--output", required=True, help=".csv, .xlsx or .json")
    sv2.add_argument("--url")
    sv2.set_defaults(func=_cmd_survey_export)

    rp = sub.add_parser("report-1255", help="quarterly INSP report, Order 1255/2016 (xlsx)")
    rp.add_argument("--year", type=int, required=True)
    rp.add_argument("--quarter", type=int, required=True, choices=[1, 2, 3, 4])
    rp.add_argument("-q", "--query", default="origin:local")
    rp.add_argument("-o", "--output", required=True)
    rp.add_argument("--url")
    rp.set_defaults(func=_cmd_report_1255)

    ex = sub.add_parser("specs", help="report specs: list them, show their fields, run one "
                                      "(a spec file dropped in the specs folder appears here)")
    ex.add_argument("action", choices=["list", "fields", "run"])
    ex.add_argument("spec", nargs="?", help="spec id (for run)")
    ex.add_argument("-p", "--param", action="append", help="key=value, e.g. year=2026 (repeat)")
    ex.add_argument("-o", "--output", help="file; the extension picks the format (no file: JSON)")
    ex.add_argument("--format", choices=["xlsx", "csv", "json", "zip"])
    ex.add_argument("--url")
    ex.set_defaults(func=_cmd_specs)

    ec = sub.add_parser("esak-curve", help="X-ray tube output curve of a device (annex 2 of "
                                           "Order 1255/2016), for ESAK without a DAP meter")
    ec.add_argument("action", choices=["set", "show", "clear"])
    ec.add_argument("--device", type=int, help="device id (the physical device, not an alias)")
    ec.add_argument("--file", help="CSV: kvp,mgy_per_mas (air kerma per mAs at the FDD)")
    ec.add_argument("--fdd", type=float, default=50.0, help="distance of the measurement, cm")
    ec.add_argument("--url")
    ec.set_defaults(func=_cmd_esak_curve)

    ed = sub.add_parser("effective-dose", help="mSv/MBq coefficients for the estimated effective "
                                               "dose of nuclear medicine (none ship)")
    ed.add_argument("action", choices=["set", "show", "clear"])
    ed.add_argument("--file", help="CSV: nuclide,form,age_to_years (empty = adult),"
                                   "msv_per_mbq,source")
    ed.add_argument("--url")
    ed.set_defaults(func=_cmd_effective_dose)

    kf = sub.add_parser("ct-kfactor", help="DLP to effective dose coefficients of CT (none ship)")
    kf.add_argument("action", choices=["set", "show", "clear"])
    kf.add_argument("--file", help="CSV: region,age_to_years (empty = adult),phantom_cm,k,source")
    kf.add_argument("--url")
    kf.set_defaults(func=_cmd_ct_kfactor)

    ce = sub.add_parser("centering", help="measure the patient centering of CT image files")
    ce.add_argument("files", nargs="+")
    ce.set_defaults(func=_cmd_centering)

    a12 = sub.add_parser("art12", help="annual DRL return, Order 1255/2016 art. 12: 20 patients "
                                       "per examination and device, Feb-Mar (zip of xlsx)")
    a12.add_argument("--year", type=int, required=True)
    a12.add_argument("-q", "--query", default="origin:local")
    a12.add_argument("-o", "--output", required=True, help="a .zip file")
    a12.add_argument("--url")
    a12.set_defaults(func=_cmd_art12)

    gr = sub.add_parser("growth", help="weight-for-age tables: install (setup), or check "
                                       "(estimate missing / implausible weights, then "
                                       "DRL exclusions P5/P95)")
    gr.add_argument("action", choices=["install", "check", "status"])
    gr.add_argument("--url")
    gr.set_defaults(func=_cmd_growth)

    rd = sub.add_parser("rdsr", help="write dose SRs (IHE REM RDSR) of stored studies to "
                                     "files (pseudonymised unless --identified); no sending")
    rd.add_argument("--study", type=int, action="append", help="study id (repeatable)")
    rd.add_argument("--query", help="query, e.g. 'modality:CT date:30d'")
    rd.add_argument("--limit", type=int, default=1000)
    rd.add_argument("--out", required=True, help="output folder")
    rd.add_argument("--identified", action="store_true",
                    help="keep patient ID, name, birth date, accession and UIDs")
    rd.add_argument("--url")
    rd.set_defaults(func=_cmd_rdsr)

    si = sub.add_parser("survey-import", help="central collection: load survey exports "
                                              "(CSV / JSON) sent by the sites")
    si.add_argument("files", nargs="+")
    si.add_argument("--url")
    si.set_defaults(func=_cmd_survey_import)

    sz = sub.add_parser("sizes", help="import patient weight / height from a CSV file "
                                      "(study UID, accession, or patient ID + date)")
    sz.add_argument("file")
    sz.add_argument("--apply", action="store_true", help="write (default: dry run)")
    sz.add_argument("--encoding", default="utf-8-sig")
    sz.add_argument("--show", type=int, default=20, help="row issues to list")
    sz.add_argument("--url")
    sz.set_defaults(func=_cmd_sizes)

    cn = sub.add_parser("cnp", help="national IDs (CNP): fill or correct sex and birth date, "
                                    "recompute exact ages (offline)")
    cn.add_argument("action", choices=["backfill"])
    cn.add_argument("--apply", action="store_true", help="write (default: dry run)")
    cn.add_argument("--url")
    cn.set_defaults(func=_cmd_cnp)

    hs = sub.add_parser("his", help="HIS / RIS: check the servers' login; run the lookup "
                                    "worker (patient, weight, height, request); queue history")
    hs.add_argument("action", choices=["check", "run", "backfill"])
    hs.add_argument("--since", help="backfill: studies from this date (YYYY-MM-DD)")
    hs.add_argument("--until", help="backfill: studies before this date")
    hs.add_argument("--device", type=int, help="backfill: one device id")
    hs.add_argument("--apply", action="store_true", help="backfill: queue (default: dry run)")
    hs.add_argument("--once", action="store_true", help="run: one batch, then exit")
    hs.add_argument("--limit", type=int, default=20, help="run: studies per batch")
    hs.add_argument("--dry-run", action="store_true",
                    help="run: one batch, print the outcome, write nothing")
    hs.add_argument("--verbose", action="store_true", help="run: print every study")
    hs.add_argument("--url")
    hs.set_defaults(func=_cmd_his)

    fg = sub.add_parser("refetch", help="forget studies imported with missing doses so that "
                                         "qr retrieves them again (repair)")
    fg.add_argument("--missing", choices=["dap", "dlp"],
                    help="dose reports (proj_rdsr: dap, ct_rdsr: dlp) without that total")
    fg.add_argument("--device", type=int, help="all projection dose reports of this device id "
                                               "(e.g. to gain fields the parser now reads)")
    fg.add_argument("--since", help="only studies from this date (YYYY-MM-DD)")
    fg.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    fg.add_argument("--url")
    fg.set_defaults(func=_cmd_refetch)

    sf = sub.add_parser("study-refetch", help="forget one study and retrieve it from the PACS "
                                                "again")
    sf.add_argument("study", type=int, help="study id")
    sf.add_argument("--by", help="user name for the audit log")
    sf.add_argument("--url")
    sf.set_defaults(func=_cmd_study_refetch)

    sk = sub.add_parser("skin", help="estimate peak skin doses of fluoroscopy studies (batch)")
    sk.add_argument("--study", type=int, help="print the estimate for one study")
    sk.add_argument("--url")
    sk.set_defaults(func=_cmd_skin)

    al = sub.add_parser("alerts", help="evaluate alert rules, send notifications, list")
    al.add_argument("action", choices=["evaluate", "notify", "list", "test-notify"])
    al.add_argument("--since", help="evaluate studies from this date (YYYY-MM-DD)")
    al.add_argument("--until", help="evaluate studies before this date (YYYY-MM-DD)")
    al.add_argument("--url")
    al.set_defaults(func=_cmd_alerts)

    dg = sub.add_parser("digests", help="monthly report digests: list, preview, run (send)")
    dg.add_argument("action", choices=["list", "preview", "run"])
    dg.add_argument("--only", help="only this digest id")
    dg.add_argument("--force", action="store_true", help="send now, even if the month went out")
    dg.add_argument("--url")
    dg.set_defaults(func=_cmd_digests)

    cl = sub.add_parser("classify", help="assign standard exam codes and DRL codes to studies")
    cl.add_argument("--study", type=int, action="append", help="only this study id (repeatable)")
    cl.add_argument("--days", type=int, help="only the studies of the last N days")
    cl.add_argument("--by", help=argparse.SUPPRESS)  # who started it (Admin → Maintenance)
    cl.add_argument("--url")
    cl.set_defaults(func=_cmd_classify)

    fx = sub.add_parser("openrem-fix", help="correct known OpenREM value errors (idempotent)")
    fx.add_argument("--url")
    fx.add_argument("--dry-run", action="store_true")
    fx.add_argument("--dump", help="OpenREM dump folder: also fill CT phantom sizes the "
                                   "first imports missed")
    fx.set_defaults(func=_cmd_openrem_fix)

    r = sub.add_parser("recompute", help="recompute derived study and device columns")
    r.add_argument("--by", help=argparse.SUPPRESS)
    r.add_argument("--url")
    r.set_defaults(func=_cmd_recompute)

    mt = sub.add_parser("maintain", help="periodic maintenance: reclassify exams, refill the stored "
                                         "bands, refresh derived values, retry failed ones")
    mt.add_argument("--days", type=int, default=90, help="studies of the last N days (default 90)")
    mt.add_argument("--full", action="store_true",
                    help="every study (also done by itself when the classification rules changed)")
    mt.add_argument("--by", help=argparse.SUPPRESS)
    mt.add_argument("--url")
    mt.set_defaults(func=_cmd_maintain)

    u = sub.add_parser("user", help="manage users")
    u.add_argument("action", choices=["set", "disable", "rename", "delete", "list"])
    u.add_argument("username", nargs="?", default="")
    u.add_argument("--roles", help=f"comma-separated: {', '.join(ROLES)}")
    u.add_argument("--name", help="display name")
    u.add_argument("--to", help="rename: the new username")
    u.add_argument("--password", help="(insecure: visible in process list) prompt if omitted")
    u.add_argument("--url")
    u.set_defaults(func=_cmd_user)

    sv = sub.add_parser("serve", help="run the web UI and API")
    sv.add_argument("--host", help="address to listen on (default from settings: 127.0.0.1)")
    sv.add_argument("--port", type=int, help="port (default from settings: 8104)")
    sv.set_defaults(func=_cmd_serve)

    do = sub.add_parser("devices-origin",
                        help="suggest local vs external devices by study volume")
    do.add_argument("--min-studies", type=int, default=100)
    do.add_argument("--apply", action="store_true")
    do.add_argument("--url")
    do.set_defaults(func=_cmd_devices_origin)

    ip = sub.add_parser("import", help="import DICOM files or folders (RDSR, DX/CR headers)")
    ip.add_argument("paths", nargs="+")
    ip.add_argument("-v", "--verbose", action="store_true")
    ip.add_argument("--url")
    ip.set_defaults(func=_cmd_import)

    rv = sub.add_parser("receive", help="run the DICOM receiver (C-STORE/C-ECHO SCP)")
    rv.add_argument("--aet", help="AE title (default from settings: DOZIMETRIX)")
    rv.add_argument("--port", type=int, help="port (default 11112)")
    rv.add_argument("--bind", help="address to listen on (default 0.0.0.0)")
    rv.add_argument("--url")
    rv.set_defaults(func=_cmd_receive)

    q = sub.add_parser("qr", help="query/retrieve dose objects from the PACS (C-FIND/C-GET)")
    q.add_argument("--node", help="DICOM node name (default: the first query/retrieve node)")
    q.add_argument("--from", dest="date_from", help="first study date YYYY-MM-DD")
    q.add_argument("--to", dest="date_to", help="last study date (default today)")
    q.add_argument("--days", type=int, help="without --from: last N days (default [qr] days, 2)")
    q.add_argument("--modalities", help="comma list (default [pacs] modalities: "
                   "CT,DX,CR,RF,XA,MG,SR); queried one at a time")
    q.add_argument("--retrieve", choices=["get", "move"],
                   help="C-GET (default) or C-MOVE to our AE title")
    q.add_argument("--dry-run", action="store_true", help="query only, retrieve nothing")
    q.add_argument("--redo", action="store_true", help="also re-run days already done")
    q.add_argument("--staff", action="store_true",
                   help="only CT studies already imported without operator / physician: "
                        "fetch one image header each")
    q.add_argument("--ct-dose", action="store_true",
                   help="only NM / PET studies already imported without a CT dose: fetch "
                        "their SR series (PET/CT whose images came first)")
    q.add_argument("--ct-headers", action="store_true",
                   help="only CT studies imported from header estimates made from the image "
                        "count: read their headers again (z range of each series)")
    q.add_argument("--dose-pages", action="store_true",
                   help="only Toshiba CT studies already imported from headers: fetch the "
                        "console's dose pages (a short Secondary Capture series, read by OCR)")
    q.add_argument("--pause", type=float, default=0.2, help="seconds between studies")
    q.add_argument("--workers", type=int, default=1,
                   help="studies retrieved in parallel (mind the PACS load)")
    q.add_argument("--host")
    q.add_argument("--port", type=int)
    q.add_argument("--aec", help="PACS AE title")
    q.add_argument("--aet", help="our AE title")
    q.add_argument("--loop", action="store_true",
                   help="run as a daemon: repeat every --interval seconds until SIGTERM")
    q.add_argument("--interval", type=int,
                   help="with --loop: seconds between the start of two runs "
                        "(default [qr] interval, 720)")
    q.add_argument("--url")
    q.set_defaults(func=_cmd_qr)

    i = sub.add_parser("info", help="show configuration")
    i.set_defaults(func=_cmd_info)
    return p


def _setup_process(args: argparse.Namespace) -> None:
    """--log-file, --log-level and --pidfile, shared by every command."""
    import atexit
    import logging
    import os

    if args.log_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.log_file)), exist_ok=True)
        f = open(args.log_file, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        os.dup2(f.fileno(), 1)  # also catches output of C libraries and child processes
        os.dup2(f.fileno(), 2)
        sys.stdout = f
        sys.stderr = f
    if args.log_level:
        # set first: a command's own basicConfig later is then a no-op
        logging.basicConfig(level=args.log_level, force=True,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.pidfile:
        path = os.path.abspath(args.pidfile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"{os.getpid()}\n")
        pid = os.getpid()

        def _remove() -> None:
            try:
                with open(path) as fh:
                    if fh.read().strip() == str(pid):
                        os.remove(path)
            except OSError:
                pass

        atexit.register(_remove)

        def _term(signum, frame):  # SIGTERM: exit normally so atexit runs
            raise SystemExit(128 + signum)

        import signal

        if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
            signal.signal(signal.SIGTERM, _term)


def _qr_loop(args: argparse.Namespace) -> int:
    """qr --loop: one run every --interval seconds; SIGTERM ends it after the current run."""
    import signal
    import threading
    import time

    stop = threading.Event()

    def _term(signum, frame):
        print("qr: stop requested, finishing the current run", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    while not stop.is_set():
        started = time.monotonic()
        try:
            rc = _cmd_qr(args)
            print(f"qr: run finished (exit {rc})", flush=True)
        except Exception as e:  # noqa: BLE001 – a failed run must not end the daemon
            import logging

            logging.getLogger("dozimetrix.qr").exception("qr run failed: %s", e)
        interval = args.interval or load_settings().qr_interval  # [qr] re-read: no restart
        stop.wait(max(0.0, interval - (time.monotonic() - started)))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        set_config_path(args.config)
    except FileNotFoundError as e:
        print(f"dozimetrix: {e}", file=sys.stderr)
        return 2
    _setup_process(args)
    from .api.data import QueryError

    try:
        if getattr(args, "loop", False) and args.func is _cmd_qr:
            return _qr_loop(args)
        return args.func(args)
    except QueryError as e:  # a filter that does not parse: not a report on everyone
        print(f"dozimetrix: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
