"""First-run setup wizard (``/setup``): checks, the first admin, site name, growth tables.

While the database has no user, every page redirects to ``/setup``. The first step needs a
one-time setup token (a 4-digit PIN): the web service prints it to its log at start and writes
it to ``<data_dir>/setup-token`` (mode 0600), so only someone with access to the server can claim
a fresh install, even on a public URL. A PIN is short, so it is replaced by a new one after
``MAX_TOKEN_FAILS`` wrong guesses (on top of the per-address throttle). Creating the first
admin signs them in; the later steps (site, DICOM nodes, growth tables) are ordinary admin
calls, and the wizard can be opened again from ``/setup`` by an admin until it is marked done.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import AppSetting, CodeSystem, DicomNode, Institution, ReferenceSet, User

log = logging.getLogger("dozimetrix.setup")
DONE_KEY = "setup.done"
SITE_KEY = "site"
MAX_TOKEN_FAILS = 10  # wrong guesses (from any address) before the PIN is replaced
ALL_ROLES = "viewer,exporter,pid,importer,admin"


class SetupError(ValueError):
    pass


def has_schema(session: Session) -> bool:
    from sqlalchemy import inspect

    return inspect(session.connection()).has_table(User.__tablename__)


def needs_admin(session: Session) -> bool:
    """No account yet (also true before the schema exists)."""
    if not has_schema(session):
        return True
    return not session.scalar(select(func.count()).select_from(User))


def prepare_database(session: Session, settings) -> list[str]:
    """Create / upgrade the schema and load the bundled seed data (both idempotent)."""
    from .db.migrate import upgrade
    from .seeds.loader import load_all

    session.close()  # release the connection: the migration opens its own
    upgrade(settings.database_url)
    report = load_all(session)
    session.flush()
    return report


def is_done(session: Session) -> bool:
    row = session.get(AppSetting, DONE_KEY)
    return bool(row and row.value)


def issue_token(data_dir: Path) -> str:
    """A new one-time token: logged, and written to <data_dir>/setup-token (0600)."""
    token = f"{secrets.randbelow(10000):04d}"  # a PIN: easy to read off a log and type
    path = Path(data_dir) / "setup-token"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        where = f" (also in {path})"
    except OSError:
        where = ""
    msg = f"First run: open /setup and enter the setup token {token}{where}"
    log.warning(msg)
    print(msg, flush=True)
    return token


def drop_token(data_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (Path(data_dir) / "setup-token").unlink()


def check_token(expected: str | None, given: str | None) -> None:
    if not expected or not given or not secrets.compare_digest(expected, given.strip()):
        raise SetupError("wrong setup token (see the web service's log, or the "
                         "setup-token file in the data folder; after "
                         f"{MAX_TOKEN_FAILS} wrong tries a new one is issued)")


def status(session: Session, settings) -> dict:
    """What a fresh install has and lacks. No secrets, no patient data."""
    from . import growth
    from .db.migrate import alembic_config

    checks = []
    if not has_schema(session):
        return {"needs_admin": True, "done": False, "schema": False,
                "checks": [{"key": "schema", "ok": False, "label": "Database schema",
                            "detail": "empty database: create it here, or run "
                                      "dozimetrix migrate && dozimetrix seed",
                            "optional": False}],
                "receiver": {"aet": settings.dicom_aet, "port": settings.dicom_port},
                "site": {}}

    def add(key, ok, label, detail="", optional=False):
        checks.append({"key": key, "ok": bool(ok), "label": label, "detail": detail,
                       "optional": optional})

    try:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(alembic_config(settings.database_url)).get_heads()
        cur = MigrationContext.configure(session.connection()).get_current_heads()
        add("schema", set(cur) == set(head), "Database schema up to date",
            "run: dozimetrix migrate" if set(cur) != set(head) else
            session.get_bind().dialect.name)
    except Exception as e:  # noqa: BLE001 – reported, not raised
        add("schema", False, "Database schema", str(e)[:200])
    n_codes = session.scalar(select(func.count()).select_from(CodeSystem)) or 0
    n_sets = session.scalar(select(func.count()).select_from(ReferenceSet)) or 0
    add("seed", n_codes and n_sets, "Vocabularies and reference levels loaded",
        f"{n_codes} code systems, {n_sets} reference sets" if n_codes else
        "run: dozimetrix seed")
    data_dir = Path(settings.data_dir)
    writable = data_dir.is_dir() and os.access(data_dir, os.W_OK)
    add("data_dir", writable, "Data folder writable", str(data_dir.resolve()))
    add("secret", Path(settings.secret_file).exists(), "Site secret created",
        f"{settings.secret_file} — back it up: pseudonyms depend on it")
    add("admin", not needs_admin(session), "Administrator account")
    site = session.get(AppSetting, SITE_KEY)
    add("site", site and (site.value or {}).get("name"), "Site name",
        (site.value or {}).get("name", "") if site else "")
    g = growth.tables(data_dir / "growth")
    add("growth", g is not None, "Growth tables (WHO / CDC weight-for-age)",
        "installed" if g else "needed for weight checks, estimates and DRL exclusions")
    n_nodes = session.scalar(select(func.count()).select_from(DicomNode)) or 0
    add("nodes", n_nodes, "DICOM nodes (PACS, modalities)", f"{n_nodes} configured",
        optional=True)
    return {
        "needs_admin": needs_admin(session), "done": is_done(session), "schema": True,
        "checks": checks,
        "receiver": {"aet": settings.dicom_aet, "port": settings.dicom_port},
        "site": (site.value if site else None) or {},
    }


def create_admin(session: Session, data: dict) -> User:
    from .api import users

    if not needs_admin(session):
        raise SetupError("an account exists already: sign in instead")
    try:
        return users.create(session, {**data, "roles": ALL_ROLES.split(",")})
    except users.UserError as e:
        raise SetupError(str(e)) from e


def save_site(session: Session, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not name or len(name) > 255:
        raise SetupError("site name: 1–255 characters")
    code = (data.get("code") or "").strip().upper() or "LOCAL"
    if not code.isascii() or len(code) > 32 or " " in code:
        raise SetupError("site code: up to 32 ASCII characters, no spaces")
    value = {"name": name, "code": code}
    row = session.get(AppSetting, SITE_KEY) or AppSetting(key=SITE_KEY)
    row.value = value
    session.add(row)
    inst = session.scalar(select(Institution).where(Institution.is_local.is_(True))
                          .order_by(Institution.id))
    if inst is None:
        inst = session.scalar(select(Institution).filter_by(code=code))
    if inst is None:
        session.add(Institution(code=code, name=name, is_local=True))
    else:
        inst.code, inst.name, inst.is_local = code, name, True
    session.flush()
    return value


def install_growth(settings) -> list[str]:
    from . import growth

    try:
        return growth.install(Path(settings.data_dir) / "growth")
    except Exception as e:  # noqa: BLE001 – network, proxy, content
        raise SetupError(f"download failed ({e}); on the server run: dozimetrix growth "
                         "install, or copy the tables into the data folder") from e


def finish(session: Session, settings) -> None:
    row = session.get(AppSetting, DONE_KEY) or AppSetting(key=DONE_KEY)
    row.value = True
    session.add(row)
    session.flush()
    drop_token(settings.data_dir)
