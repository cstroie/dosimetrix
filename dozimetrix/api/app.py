"""FastAPI application: serves the static UI and the JSON API.

Privacy rules:
* every /api route except /api/login requires a session;
* responses carry patient pseudonyms only; identifiers need the `pid` role and an explicit,
  audit-logged call (POST /api/studies/{id}/identify);
* the server is meant to bind to localhost or sit behind an HTTPS reverse proxy, and the
  access log is disabled by `dozimetrix serve` because query strings may contain identifiers.
"""

from __future__ import annotations

import datetime as dt
import html as _html
import json
import re
import secrets
from collections.abc import Iterator
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import Settings, load_settings
from ..db.models import AuditLog, DicomNode, Study, User
from ..db.session import make_engine, make_sessionmaker
from ..security import (
    hash_password,
    load_or_create_secret,
    read_session_claims,
    session_stamp,
    sign_session,
    verify_password,
)
from . import admin, data, review
from .hardening import LoginThrottle, install_hardening

WEB = Path(__file__).resolve().parent.parent / "web"
LOGIN_HTML = Path(__file__).resolve().parent / "login.html"
SETUP_HTML = Path(__file__).resolve().parent / "setup.html"
COOKIE = "dt_session"
PAGES = {"": "index.html", "index.html": "index.html", "studies.html": "studies.html",
         "study.html": "study.html", "charts.html": "charts.html",
         "devices.html": "devices.html", "drl.html": "drl.html", "drl-sets.html": "drl-sets.html",
         "review.html": "review.html",
         "reports.html": "reports.html", "quality.html": "quality.html",
         "rules.html": "rules.html", "integration.html": "integration.html",
         "users.html": "users.html", "audit.html": "audit.html",
         "alerts.html": "alerts.html", "local-drl.html": "local-drl.html",
         "protocols.html": "protocols.html",
         "indications.html": "indications.html", "survey-central.html": "survey-central.html",
         "dose-statement.html": "dose-statement.html", "settings.html": "settings.html",
         "maintenance.html": "maintenance.html",
         "analysis.html": "analysis.html", "followups.html": "followups.html",
         "incidents.html": "incidents.html", "qa.html": "qa.html",
         "digests.html": "digests.html", "kfactors.html": "kfactors.html"}
OLD_PAGES = {"dicom.html": "integration.html"}
LIVE_TOPICS = {"pulse", "services", "dicom", "his", "studies", "alerts", "maintenance"}
LIVE_TOPIC_EVERY_S = {"studies": 15, "alerts": 60}  # list pages: scarce updates are enough
LIVE_SHARE_S = 2.5  # a topic computed this recently is shared between the open streams
LIVE_EVERY_S = 5  # heartbeats come every minute or so, HIS lookups every few seconds
LIVE_PING_S = 25
LIVE_RETRY_MS = 5000
LIVE_MAX_TOPICS = 20
LIVE_MAX_STREAMS = 8  # per user: a few tabs, each with one stream
LIVE_RECHECK_S = 30
LIVE_MAX_S = 600  # a stream ends after 10 min and the browser reopens it: no stale sockets  # old name -> page (DICOM, HIS and more now)


_HOST_IPS_TTL_S = 60.0
_host_ips_seen: tuple[float, list[str]] | None = None


def _host_ips() -> list[str]:
    """This machine's IPv4 addresses (what modalities and PACS must be told to send to). Asking the
    resolver for the host name can take seconds when DNS is slow, and the Integration page asks on
    every refresh, so the answer is kept for a minute."""
    global _host_ips_seen
    import time

    if _host_ips_seen and time.monotonic() - _host_ips_seen[0] < _HOST_IPS_TTL_S:
        return list(_host_ips_seen[1])
    ips = _lookup_host_ips()
    _host_ips_seen = (time.monotonic(), ips)
    return list(ips)


def _lookup_host_ips() -> list[str]:
    import socket

    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address of the default route, no packet is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            ips.add(sk.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class RevalidatedStatic(StaticFiles):
    """Static files that the browser must revalidate (ETag / 304) on every load: without a
    Cache-Control header it caches scripts heuristically and keeps showing an old page after
    an update."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class LoginIn(BaseModel):
    username: str
    password: str


class LocalDRLIn(BaseModel):
    name: str
    grid: str = "national"
    start: dt.date
    end: dt.date
    min_n: int = 10
    rule: str = "pooled"
    p75_n: int = 30
    q: str = "origin:local"
    estimated: bool = False
    rows: list[dict]


class ActionIn(BaseModel):
    year: int | None = None
    quarter: int | None = None
    finding: str | None = None
    action: str | None = None
    owner: str | None = None
    status: str | None = None  # open | doing | done


class GlandularityIn(BaseModel):
    percent: float | None = None  # 0..100, None removes it


class IndicationIn(BaseModel):
    code: str | None = None


class IndicationMapIn(BaseModel):
    text: str
    code: str | None = None


class InstitutionNameIn(BaseModel):
    name: str | None = None


class PasswordIn(BaseModel):
    current: str
    new: str


class TokenIn(BaseModel):
    token: str


class SetupAdminIn(BaseModel):
    token: str
    username: str
    password: str
    name: str | None = None


class JobIn(BaseModel):
    """Start a maintenance job: ``days`` limits it to the last N days, none = every study."""
    days: int | None = Field(default=None, ge=1, le=3650)


class SiteIn(BaseModel):
    name: str
    code: str | None = None


class DevicePatch(BaseModel):
    display_name: str | None = None
    modality_override: str | None = None
    origin: str | None = None
    his_policy: str | None = None


class MergeIn(BaseModel):
    record_ids: list[int]
    survivor_id: int
    display_name: str | None = None


class DismissIn(BaseModel):
    record_ids: list[int]


class UserIn(BaseModel):
    username: str | None = None
    name: str | None = None
    roles: list[str] | None = None
    active: bool | None = None
    password: str | None = None


class NodeIn(BaseModel):
    name: str | None = None
    aet: str | None = None
    host: str | None = None
    port: int | None = None
    enabled: bool | None = None
    query_retrieve: bool | None = None
    sends: bool | None = None
    retrieve: str | None = None
    move_destination: str | None = None
    modalities: str | None = None
    max_results: int | None = None
    timeout: int | None = None
    sync_days: int | None = None
    notes: str | None = None


class HisServerIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = None  # write-only; "" clears it
    enabled: bool | None = None
    timeout: int | None = None
    modalities: str | None = None
    window_before_h: int | None = None
    window_after_h: int | None = None
    min_interval_s: float | None = None
    backfill_hours: str | None = None
    notes: str | None = None


class AckIn(BaseModel):
    note: str | None = None


class QAIn(BaseModel):
    kind: str
    done_on: dt.date
    next_due: dt.date | None = None
    factor: float | None = None
    result: str | None = Field(None, max_length=255)
    note: str | None = Field(None, max_length=1024)


class IncidentIn(BaseModel):
    alert_id: int | None = None
    study_id: int | None = None
    title: str | None = Field(None, max_length=255)


class IncidentPatch(BaseModel):
    title: str | None = Field(None, max_length=255)
    cause: str | None = None
    actions: str | None = None
    outcome: str | None = None
    reportable: bool | None = None
    reportable_basis: str | None = Field(None, max_length=1024)
    reported_at: dt.datetime | None = None
    report_reference: str | None = Field(None, max_length=128)
    note: str | None = None  # appended to the log
    action: str | None = None  # close | reopen (the reason is in note)


class FollowupIn(BaseModel):
    status: str | None = None
    outcome: str | None = Field(None, max_length=255)
    note: str | None = Field(None, max_length=1024)
    check_due: dt.date | None = None


class AckManyIn(BaseModel):
    ids: list[int]
    note: str | None = None


class RulePatch(BaseModel):
    enabled: bool | None = None
    threshold: float | None = None
    severity: str | None = None
    notify: bool | None = None
    window_days: int | None = None
    max_age_days: int | None = None
    name: str | None = None


class RuleIn(BaseModel):
    """A new alert rule (the kinds that need no extra settings; device rules are seeded)."""
    name: str
    kind: str
    metric: str | None = None
    modality: str | None = None
    threshold: float | None = None  # in the metric's stored unit (Gy, Gy.cm2, ...)
    window_days: int | None = None
    max_age_days: int | None = None
    severity: str = "warn"
    notify: bool = False
    note: str | None = None


class SettingsIn(BaseModel):
    """Site-wide settings (Admin → Settings): all optional, a partial update."""
    name: str | None = None  # the site name and code are the first-run wizard's
    code: str | None = None
    base_url: str | None = None  # "" = back to the toml's [notify] base_url
    spool_retention_days: int | None = None  # null = back to the toml's value


class NotifyIn(BaseModel):
    """Notification settings an admin sets in the UI (all optional: a partial update). An empty
    list/string clears a field back to the toml value; the two secrets are write-only."""
    email_to: list[str] | None = None
    email_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_starttls: bool | None = None
    smtp_ssl: bool | None = None
    smtp_user: str | None = None
    ntfy_url: str | None = None
    smtp_password: str | None = None  # "" = unchanged
    ntfy_token: str | None = None
    clear_smtp_password: bool = False
    clear_ntfy_token: bool = False


class NotifyTestIn(BaseModel):
    channel: str


RULE_KINDS = ("threshold", "cumulative", "count", "drl_ratio", "pregnant")


class DecisionIn(BaseModel):
    family: str  # rx | rf | ct
    field: str  # protocol | anatomy | study_description | procedure_name | ...
    text: str
    code: str | None = None  # nomenclator code; null clears the decision


def create_app(settings: Settings | None = None, engine: Engine | None = None,
               secret: bytes | None = None, now: dt.datetime | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = engine or make_engine(settings.database_url)
    maker = make_sessionmaker(engine)
    secret = secret or load_or_create_secret(settings.secret_file)
    app = FastAPI(title="Dozimetrix", docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json")
    app.state.secret = secret
    app.state.settings = settings
    app.state.fixed_now = now  # tests only
    from ..specs import plugins as _plugins

    app.state.plugins = _plugins.load(settings.plugins_dir)  # trusted code, see specs/plugins.py
    if app.state.plugins:
        with maker() as s0:
            for pl in app.state.plugins:
                s0.add(AuditLog(username="system", action="plugin", target=pl["file"],
                                detail={"sha256": pl["sha256"], "error": pl["error"]}))
            s0.commit()

    def db() -> Iterator[Session]:
        with maker() as session:
            yield session

    install_hardening(app, settings)
    app.state.live_streams = {}
    from .resultcache import ResultCache

    app.state.result_cache = rc = ResultCache(settings.result_cache_s)
    throttle = LoginThrottle()
    app.state.login_throttle = throttle

    def _stamp(user: User) -> str:
        return session_stamp(secret, user.password_hash, user.roles or "",
                             user.session_epoch or 0)

    def session_user(request: Request, session: Session) -> User | None:
        """The active user a valid cookie names, if its password and roles are unchanged."""
        claims = read_session_claims(secret, request.cookies.get(COOKIE))
        if not claims:
            return None
        user = session.get(User, claims[0])
        if user is None or not user.is_active:
            return None
        if claims[1] != _stamp(user):
            return None
        return user

    def set_session_cookie(request: Request, response: Response, user: User) -> None:
        response.set_cookie(
            COOKIE, sign_session(secret, user.id, settings.session_hours,
                                 stamp=_stamp(user)),
            httponly=True, samesite="strict",
            # always Secure over HTTPS (X-Forwarded-Proto from a trusted proxy)
            secure=settings.secure_cookies or request.url.scheme == "https",
            max_age=int(settings.session_hours * 3600), path="/")

    def current_user(request: Request, session: Session = Depends(db)) -> User:
        user = session_user(request, session)
        if user is None:
            raise HTTPException(401, "login required")
        return user

    def admin_only(user: User) -> None:
        if "admin" not in user.role_set():
            raise HTTPException(403, "the admin role is required")

    def admin_user(user: User = Depends(current_user)) -> User:
        if "admin" not in user.role_set():
            raise HTTPException(403, "the admin role is required")
        return user

    def reports_user(user: User = Depends(current_user)) -> User:
        """The pages under Reports and Quality (analyses, registers): the exporter or the admin
        role, the same rule as for downloading reports; writes stay admin only."""
        if not user.role_set() & {"exporter", "admin"}:
            raise HTTPException(403, "the exporter role is required")
        return user

    def admin_write(session: Session, request: Request, user: User, action: str,
                    target: str, fn):
        try:
            result = fn()
        except admin.AdminError as e:
            session.rollback()
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, action, target, result)
        session.commit()
        return result

    def clock() -> dt.datetime:
        return app.state.fixed_now or dt.datetime.now()

    # ------------------------------------------------------------------ first-run setup

    from .. import setup as first_run

    app.state.setup_token = None
    app.state.setup_fails = 0

    def setup_token(session: Session) -> str | None:
        """The one-time token while no account exists (issued at start, or lazily)."""
        if not first_run.needs_admin(session):
            return None
        if app.state.setup_token is None:
            app.state.setup_token = first_run.issue_token(settings.data_dir)
        return app.state.setup_token

    with maker() as _s:
        setup_token(_s)

    def optional_user(request: Request, session: Session) -> User | None:
        return session_user(request, session)

    def setup_guard(request: Request, session: Session) -> User | None:
        """No account yet: open (the token protects the first write). Else: admin only."""
        if first_run.needs_admin(session):
            return None
        user = optional_user(request, session)
        if user is None:
            raise HTTPException(401, "login required")
        if "admin" not in user.role_set():
            raise HTTPException(403, "the admin role is required")
        return user

    def _token_ok(request: Request, session: Session, token: str) -> None:
        try:
            first_run.check_token(setup_token(session), token)
        except first_run.SetupError:
            throttle.fail(request, "setup")
            app.state.setup_fails += 1
            if app.state.setup_fails >= first_run.MAX_TOKEN_FAILS:  # a PIN is guessable: rotate
                app.state.setup_fails = 0
                app.state.setup_token = first_run.issue_token(settings.data_dir)
            raise

    def _setup(fn):
        try:
            return fn()
        except first_run.SetupError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/setup/status")
    def setup_status(request: Request, session: Session = Depends(db)):
        setup_guard(request, session)
        return first_run.status(session, settings)

    @app.post("/api/setup/database")
    def setup_database(body: TokenIn, request: Request,
                       session: Session = Depends(db)):
        """Fresh install only (no account yet; token): create the schema, load seeds."""
        throttle.check(request, "setup")
        _setup(lambda: _token_ok(request, session, body.token))
        report = first_run.prepare_database(session, settings)
        session.commit()
        return {"report": report}

    @app.post("/api/setup/admin")
    def setup_admin(body: SetupAdminIn, request: Request, response: Response,
                    session: Session = Depends(db)):
        throttle.check(request, "setup")
        _setup(lambda: _token_ok(request, session, body.token))
        user = _setup(lambda: first_run.create_admin(session, body.model_dump()))
        _audit(session, request, user, "setup.admin", user.username)
        session.commit()
        app.state.setup_token = None
        set_session_cookie(request, response, user)
        return _me(user)

    @app.post("/api/setup/site")
    def setup_site(body: SiteIn, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        out = _setup(lambda: first_run.save_site(session, body.model_dump()))
        _audit(session, request, user, "setup.site", out["code"], out)
        session.commit()
        return out

    @app.post("/api/setup/growth")
    def setup_growth(request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        report = _setup(lambda: first_run.install_growth(settings))
        _audit(session, request, user, "setup.growth", "install", {"report": report})
        session.commit()
        return {"report": report}

    @app.post("/api/setup/finish")
    def setup_finish(request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        first_run.finish(session, settings)
        _audit(session, request, user, "setup.finish", None)
        session.commit()
        return {"done": True}

    # ------------------------------------------------------------------ auth

    @app.post("/api/login")
    def login(body: LoginIn, request: Request, response: Response,
              session: Session = Depends(db)):
        throttle.check(request, "login", body.username)
        user = session.scalar(select(User).filter_by(username=body.username))
        # always run scrypt, so the time taken does not tell which usernames exist
        good = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
        ok = bool(user and user.is_active and good)
        if not ok:
            throttle.fail(request, "login", body.username)
        else:
            throttle.clear(request, "login", body.username)
        _audit(session, request, user if ok else None, "login" if ok else "login_failed",
               body.username)
        session.commit()
        if not ok:
            raise HTTPException(401, "invalid username or password")
        user.last_login_at = dt.datetime.now()
        session.commit()
        set_session_cookie(request, response, user)
        return _me(user)

    @app.post("/api/logout")
    def logout(request: Request, response: Response, session: Session = Depends(db)):
        """Sign out: the cookie goes, and so does every session of this user (a token that was
        copied stops working), by raising the user's session epoch."""
        user = session_user(request, session)
        if user is not None:
            user.session_epoch = (user.session_epoch or 0) + 1
            session.commit()
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return _me(user)

    # a user's own settings on the server (chart workspaces, saved study queries), small
    PREF_KEY = re.compile(r"(workspace[A-Za-z0-9._ \-]{0,100}|studies\.saved)")

    @app.get("/api/me/prefs/{key}")
    def get_pref(key: str, user: User = Depends(current_user),
                 session: Session = Depends(db)):
        from ..db.models import UserPref

        if not PREF_KEY.fullmatch(key):
            raise HTTPException(400, "unknown preference")
        row = session.scalar(select(UserPref).filter_by(user_id=user.id, key=key))
        return {"key": key, "value": row.value if row else None}

    @app.put("/api/me/prefs/{key}")
    def put_pref(key: str, body: dict = Body(...), user: User = Depends(current_user),  # noqa: B008
                 session: Session = Depends(db)):
        from ..db.models import UserPref

        if not PREF_KEY.fullmatch(key):
            raise HTTPException(400, "unknown preference")
        value = body.get("value")
        if len(json.dumps(value)) > 256_000:
            raise HTTPException(413, "preference too large")
        row = session.scalar(select(UserPref).filter_by(user_id=user.id, key=key))
        if row is None:
            if session.scalar(select(func.count()).select_from(UserPref)
                              .filter_by(user_id=user.id)) >= 100:
                raise HTTPException(400, "too many saved preferences")
            row = UserPref(user_id=user.id, key=key)
            session.add(row)
        row.value = value
        row.updated_at = clock()
        session.commit()
        return {"key": key, "saved": True}

    # ------------------------------------------------------------------ shared views
    # Workspaces and study queries an admin publishes for everyone (users keep their own in
    # user_pref). Everyone reads them; only admins write, and every write is audited.

    VIEW_KINDS = {"workspace", "study_query"}

    def _view_json(v) -> dict:
        return {"id": v.id, "kind": v.kind, "name": v.name, "value": v.value,
                "position": v.position, "updated_at": v.updated_at}

    def _view_body(body: dict, partial: bool) -> dict:
        out = {}
        if "kind" in body or not partial:
            if body.get("kind") not in VIEW_KINDS:
                raise HTTPException(400, "kind must be workspace or study_query")
            out["kind"] = body["kind"]
        if "name" in body or not partial:
            name = str(body.get("name") or "").strip()
            if not name or len(name) > 120:
                raise HTTPException(400, "a name of 1 to 120 characters is required")
            out["name"] = name
        if "value" in body or not partial:
            value = body.get("value")
            if not isinstance(value, dict) or not isinstance(value.get("q", ""), str):
                raise HTTPException(400, "value must be an object with a text q")
            if len(json.dumps(value)) > 256_000:
                raise HTTPException(413, "view too large")
            out["value"] = value
        if "position" in body:
            out["position"] = int(body["position"] or 0)
        return out

    def _view_clash(session: Session, kind: str, name: str, skip: int | None = None) -> None:
        from ..db.models import SharedView

        other = session.scalar(select(SharedView).filter_by(kind=kind, name=name))
        if other is not None and other.id != skip:
            raise HTTPException(409, f"a shared view named “{name}” already exists")

    @app.get("/api/views")
    def list_views(kind: str | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db)):
        from ..db.models import SharedView

        stmt = select(SharedView).order_by(SharedView.position, SharedView.name)
        if kind:
            stmt = stmt.filter_by(kind=kind)
        return {"views": [_view_json(v) for v in session.scalars(stmt)]}

    @app.post("/api/views")
    def create_view(request: Request, body: dict = Body(...),  # noqa: B008
                    user: User = Depends(admin_user), session: Session = Depends(db)):
        from ..db.models import SharedView

        fields = _view_body(body, partial=False)
        _view_clash(session, fields["kind"], fields["name"])
        if "position" not in fields:  # new ones go last
            last = session.scalar(select(func.max(SharedView.position))
                                  .filter_by(kind=fields["kind"]))
            fields["position"] = (last or 0) + 1
        v = SharedView(**fields, created_by=user.id, updated_by=user.id, updated_at=clock())
        session.add(v)
        session.flush()
        _audit(session, request, user, "view.create", f"{v.kind}:{v.id}", {"name": v.name})
        session.commit()
        return _view_json(v)

    @app.put("/api/views/{view_id}")
    def update_view(view_id: int, request: Request, body: dict = Body(...),  # noqa: B008
                    user: User = Depends(admin_user), session: Session = Depends(db)):
        from ..db.models import SharedView

        v = session.get(SharedView, view_id)
        if v is None:
            raise HTTPException(404, "no such view")
        fields = _view_body({k: x for k, x in body.items() if k != "kind"}, partial=True)
        if "name" in fields:
            _view_clash(session, v.kind, fields["name"], skip=v.id)
        for k, x in fields.items():
            setattr(v, k, x)
        v.updated_by, v.updated_at = user.id, clock()
        _audit(session, request, user, "view.update", f"{v.kind}:{v.id}",
               {"name": v.name, "fields": sorted(fields)})
        session.commit()
        return _view_json(v)

    @app.delete("/api/views/{view_id}")
    def delete_view(view_id: int, request: Request, user: User = Depends(admin_user),
                    session: Session = Depends(db)):
        from ..db.models import SharedView

        v = session.get(SharedView, view_id)
        if v is None:
            raise HTTPException(404, "no such view")
        _audit(session, request, user, "view.delete", f"{v.kind}:{v.id}", {"name": v.name})
        session.delete(v)
        session.commit()
        return {"deleted": view_id}

    @app.post("/api/views/order")
    def order_views(request: Request, body: dict = Body(...),  # noqa: B008
                    user: User = Depends(admin_user), session: Session = Depends(db)):
        from ..db.models import SharedView

        ids = [int(i) for i in body.get("ids") or []]
        rows = {v.id: v for v in session.scalars(
            select(SharedView).filter_by(kind=body.get("kind")))}
        if set(ids) != set(rows):
            raise HTTPException(400, "ids must list every shared view of that kind once")
        for pos, i in enumerate(ids):
            rows[i].position = pos
        _audit(session, request, user, "view.order", body.get("kind"), {"ids": ids})
        session.commit()
        return {"ordered": len(ids)}

    # The filters a page opens with when the user has none of their own, set by an admin.
    # Field name -> type, per page; anything else is refused.
    PAGE_DEFAULTS = {
        "drl": {"q": str, "set": str, "by": str, "minN": int},
        "quality": {"q": str},
        "alerts": {"status": str, "rule": str, "level": str, "modality": str},
    }

    @app.get("/api/page-defaults")
    def get_page_defaults(user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import AppSetting

        out = {}
        for page in PAGE_DEFAULTS:
            row = session.get(AppSetting, f"page_default.{page}")
            out[page] = row.value if row and row.value else {}
        return out

    @app.put("/api/page-defaults/{page}")
    def put_page_defaults(page: str, request: Request, body: dict = Body(...),  # noqa: B008
                          user: User = Depends(admin_user), session: Session = Depends(db)):
        from ..db.models import AppSetting

        spec = PAGE_DEFAULTS.get(page)
        if spec is None:
            raise HTTPException(404, "no such page")
        unknown = set(body) - set(spec)
        if unknown:
            raise HTTPException(400, f"unknown fields: {', '.join(sorted(unknown))}")
        value = {}
        for k, x in body.items():
            if x is None or x == "":
                continue
            if not isinstance(x, spec[k]) or isinstance(x, bool) or len(str(x)) > 500:
                raise HTTPException(400, f"{k} must be a {spec[k].__name__}")
            value[k] = x
        key = f"page_default.{page}"
        row = session.get(AppSetting, key) or AppSetting(key=key)
        row.value = value
        session.add(row)
        _audit(session, request, user, "page_default.set", page, value)
        session.commit()
        return {"page": page, "value": value}

    # ------------------------------------------------------------------ data

    @app.get("/api/overview")
    def overview(user: User = Depends(current_user), session: Session = Depends(db)):
        return data.overview(session, clock(), settings.specs_dir)

    @app.get("/api/devices")
    def devices(user: User = Depends(current_user), session: Session = Depends(db)):
        return data.devices(session)

    @app.patch("/api/devices/{device_id}")
    def device_patch(device_id: int, body: DevicePatch, request: Request,
                     user: User = Depends(admin_user), session: Session = Depends(db)):
        patch = body.model_dump(exclude_unset=True)
        return admin_write(session, request, user, "device.update", f"device:{device_id}",
                           lambda: admin.update_device(session, device_id, patch))

    @app.post("/api/devices/merge")
    def device_merge(body: MergeIn, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        return admin_write(session, request, user, "device.merge",
                           f"device:{body.survivor_id}",
                           lambda: admin.merge_devices(session, body.record_ids,
                                                       body.survivor_id, body.display_name))

    @app.post("/api/devices/{device_id}/split")
    def device_split(device_id: int, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        return admin_write(session, request, user, "device.split", f"device:{device_id}",
                           lambda: admin.split_device(session, device_id))

    @app.post("/api/devices/merge-suggestions/dismiss")
    def device_dismiss(body: DismissIn, request: Request, user: User = Depends(admin_user),
                       session: Session = Depends(db)):
        return admin_write(session, request, user, "device.merge_dismissed",
                           ",".join(map(str, body.record_ids)),
                           lambda: admin.dismiss_suggestion(session, body.record_ids))

    @app.get("/api/drl")
    def drl(user: User = Depends(current_user)):
        return data.drl()

    @app.get("/api/drl/sets")
    def drl_sets(user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod

        return drl_mod.reference_sets(session)

    @app.get("/api/drl/sets/{key}")
    def drl_set(key: str, user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod

        try:
            return drl_mod.reference_values(session, key)
        except drl_mod.DRLError as e:
            raise HTTPException(404, str(e)) from e

    def _period(start: dt.date | None, end: dt.date | None) -> tuple[dt.date, dt.date]:
        end = end or clock().date()
        start = start or end - dt.timedelta(days=365)
        if start > end:
            raise HTTPException(400, "start is after end")
        if end >= dt.date.max:
            raise HTTPException(400, "end is too far in the future")
        return start, end

    def _no_period_in(q: str) -> None:
        """Local DRLs take their period from start/end, which the set records: a date: in the
        selection would narrow it without saying so."""
        from .query import parse

        if any(t.key == "date" for t in parse(q)):
            raise data.QueryError("the period is set by From and To: remove date: from the "
                                  "selection")

    def _period_conds(q: str, start: dt.date, end: dt.date, session: Session) -> list:
        _no_period_in(q)
        return [*data.checked_conditions(q, clock(), data.overrides(session)),
                Study.study_datetime >= dt.datetime.combine(start, dt.time()),
                Study.study_datetime < dt.datetime.combine(end + dt.timedelta(days=1),
                                                           dt.time())]

    @app.get("/api/drl/local/propose")
    def local_drl_propose(q: str = "origin:local", start: dt.date | None = None,
                          end: dt.date | None = None, grid: str = "national",
                          min_n: int = 10, estimated: bool = False, rule: str = "pooled",
                          p75_n: int = 30, scope: str = "all",
                          user: User = Depends(current_user), session: Session = Depends(db)):
        """Proposed local DRLs: P75 from p75_n values, else the median (rule pooled), or the
        ICRP 135 room rule; see localdrl.py."""
        from .. import localdrl

        start, end = _period(start, end)
        try:
            out = rc.get(session, "local-drl-propose",
                         (q, start, end, grid, min_n, estimated, rule, p75_n, scope),
                         lambda: localdrl.propose(session, _period_conds(q, start, end, session),
                                                  grid, max(min_n, 1), estimated, rule,
                                                  max(p75_n, 1), scope))
        except localdrl.LocalDRLError as e:
            raise HTTPException(400, str(e)) from e
        return {**out, "start": start.isoformat(), "end": end.isoformat(), "q": q}

    @app.get("/api/protocols/compare")
    def protocols_compare(region: str, system: str = "ro-drl-ms838-2025",
                          q: str = "origin:local", start: dt.date | None = None,
                          end: dt.date | None = None, grid: str = "icrp",
                          by: str = "protocol", by_device: bool = True, min_n: int = 10,
                          estimated: bool = False, user: User = Depends(reports_user),
                          session: Session = Depends(db)):
        """The protocols of one CT region side by side, per ICRP 135 patient group:
        distributions of dose and technique, dose rank; see protocols.py."""
        from .. import protocols

        start, end = _period(start, end)
        try:
            out = protocols.compare(session, _period_conds(q, start, end, session), region,
                                    system=system, grid=grid, by=by, by_device=by_device,
                                    min_n=max(min_n, 1), estimated_weights=estimated)
        except protocols.ProtocolsError as e:
            raise HTTPException(400, str(e)) from e
        return {**out, "start": start.isoformat(), "end": end.isoformat(), "q": q}

    @app.post("/api/drl/local")
    def local_drl_adopt(body: LocalDRLIn, request: Request, user: User = Depends(admin_user),
                        session: Session = Depends(db)):
        from .. import localdrl

        _no_period_in(body.q)
        try:
            rs = localdrl.adopt(session, body.rows, name=body.name, grid=body.grid,
                                start=body.start, end=body.end, min_n=body.min_n,
                                query=body.q + ("" if body.estimated
                                                else " (estimated weights left out)"),
                                now=clock(), rule=body.rule, p75_n=body.p75_n)
        except localdrl.LocalDRLError as e:
            session.rollback()
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "drl.local.adopt", rs.key,
               {"name": rs.name, "values": len(rs.values), "grid": body.grid,
                "start": body.start.isoformat(), "end": body.end.isoformat()})
        session.commit()
        return {"key": rs.key, "name": rs.name, "values": len(rs.values)}

    @app.delete("/api/drl/sets/{key}")
    def drl_set_delete(key: str, request: Request, user: User = Depends(admin_user),
                       session: Session = Depends(db)):
        from .. import localdrl

        try:
            out = localdrl.delete(session, key)
        except localdrl.LocalDRLError as e:
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "drl.local.delete", key, out)
        session.commit()
        return out

    @app.get("/api/drl/local/review")
    def local_drl_review(request: Request, set: str, q: str = "origin:local",
                         start: dt.date | None = None, end: dt.date | None = None,
                         min_n: int = 10, format: str = "json",
                         user: User = Depends(current_user), session: Session = Depends(db)):
        """An adopted local set against a new period (yearly review); json or xlsx."""
        from .. import localdrl

        start, end = _period(start, end)
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        try:
            rep = localdrl.review(session, set, _period_conds(q, start, end, session),
                                  max(min_n, 1))
        except localdrl.LocalDRLError as e:
            raise HTTPException(404, str(e)) from e
        rep = {**rep, "start": start.isoformat(), "end": end.isoformat(), "q": q}
        if format == "json":
            return rep
        _exporter(user)
        _audit(session, request, user, "report", f"drl.local.review:{set}", {"q": q})
        session.commit()
        info = {"Perioada": f"{start.isoformat()} – {end.isoformat()}", "Selecție": q,
                "Generat": clock().isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username}
        return _file(lambda fh: localdrl.write_review_xlsx(rep, fh, info), "xlsx",
                     f"revizuire-nrd-local-{end:%Y%m%d}", clock())

    @app.get("/api/drl/compare")
    def drl_compare(q: str = "origin:local", set: str = "ro-ms838-2025", by: str = "facility",
                    min_n: int = 20, estimated: bool = False,
                    user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod

        try:
            return rc.get(session, "drl-compare", (q, set, by, min_n, estimated),
                          lambda: data.drl_compare(session, q, clock(), set, by, max(min_n, 1),
                                                   estimated))
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/modalities")
    def modalities(user: User = Depends(current_user)):
        from ..modality_profiles import PROFILES

        return PROFILES

    @app.get("/api/studies")
    def studies(q: str = "", sort: str = "date", dir: str = "desc", page: int = 1,
                size: int = 50, user: User = Depends(current_user),
                session: Session = Depends(db)):
        return data.studies(session, q, sort, dir, max(page, 1), min(max(size, 1), 500), clock())

    @app.get("/api/studies/{study_id}")
    def study(study_id: int, user: User = Depends(current_user),
              session: Session = Depends(db)):
        from .. import quality

        out = data.study(session, study_id)
        if out is None:
            raise HTTPException(404, "study not found")
        out["quality"] = quality.study_findings(session, study_id)
        from .. import indications

        out["indication"] = indications.of_study(session, study_id)
        return out

    # ------------------------------------------------------------------ plan of measures

    ACTION_STATUS = ("open", "doing", "done")

    def _action_dict(a) -> dict:
        return {"id": a.id, "year": a.year, "quarter": a.quarter, "finding": a.finding,
                "action": a.action, "owner": a.owner, "status": a.status,
                "created_by": a.created_by,
                "updated_at": a.updated_at.isoformat(timespec="minutes") if a.updated_at else None}

    @app.get("/api/actions")
    def actions_list(year: int, quarter: int, user: User = Depends(current_user),
                     session: Session = Depends(db)):
        """The quarter's plan of optimisation measures (art. 7(2))."""
        from ..db.models import OptimizationAction as A

        rows = session.scalars(select(A).where(A.year == year, A.quarter == quarter)
                               .order_by(A.id))
        return {"year": year, "quarter": quarter, "rows": [_action_dict(a) for a in rows]}

    @app.post("/api/actions")
    def actions_add(body: ActionIn, request: Request, user: User = Depends(current_user),
                    session: Session = Depends(db)):
        from ..db.models import OptimizationAction as A

        admin_only(user)
        text_ok = (body.finding or "").strip() and (body.action or "").strip()
        if body.year is None or body.quarter not in (1, 2, 3, 4) or not text_ok:
            raise HTTPException(400, "year, quarter 1..4, finding and action are required")
        if (body.status or "open") not in ACTION_STATUS:
            raise HTTPException(400, "status is open|doing|done")
        a = A(year=body.year, quarter=body.quarter, finding=body.finding.strip()[:1000],
              action=body.action.strip()[:1000], owner=(body.owner or "").strip()[:120] or None,
              status=body.status or "open", created_by=user.username)
        session.add(a)
        session.flush()
        _audit(session, request, user, "action.add", f"action:{a.id}",
               {"period": f"{a.year}Q{a.quarter}"})
        session.commit()
        return _action_dict(a)

    @app.put("/api/actions/{action_id}")
    def actions_edit(action_id: int, body: ActionIn, request: Request,
                     user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import OptimizationAction as A

        admin_only(user)
        a = session.get(A, action_id)
        if a is None:
            raise HTTPException(404, "action not found")
        if body.status is not None and body.status not in ACTION_STATUS:
            raise HTTPException(400, "status is open|doing|done")
        changed = {}
        for k in ("finding", "action", "owner", "status"):
            v = getattr(body, k)
            if v is not None and v.strip() != (getattr(a, k) or ""):
                changed[k] = v.strip()[:1000]
                setattr(a, k, v.strip()[:1000] or None)
        if not a.finding or not a.action:
            raise HTTPException(400, "finding and action cannot be empty")
        _audit(session, request, user, "action.edit", f"action:{a.id}", changed)
        session.commit()
        return _action_dict(a)

    @app.delete("/api/actions/{action_id}")
    def actions_delete(action_id: int, request: Request, user: User = Depends(current_user),
                       session: Session = Depends(db)):
        from ..db.models import OptimizationAction as A

        admin_only(user)
        a = session.get(A, action_id)
        if a is None:
            raise HTTPException(404, "action not found")
        _audit(session, request, user, "action.delete", f"action:{a.id}",
               {"period": f"{a.year}Q{a.quarter}", "action": a.action[:120]})
        session.delete(a)
        session.commit()
        return {"deleted": action_id}

    @app.post("/api/deadlines/{deadline_id}/done")
    def deadline_done(deadline_id: str, request: Request, user: User = Depends(current_user),
                      session: Session = Depends(db)):
        """Mark a reporting deadline as sent: only a person knows that (nothing is sent)."""
        from .. import deadlines
        from ..db.models import AppSetting

        admin_only(user)
        from ..specs import loader as _loader

        known = {d["id"] for d in deadlines.upcoming(
            clock().date(), (), list(_loader.discover(settings.specs_dir).specs.values()))}
        known |= {d["id"] for d in deadlines.drl_reviews(session, clock().date())}
        if deadline_id not in known:
            raise HTTPException(404, "no such deadline in its window")
        row = session.get(AppSetting, deadlines.DONE_KEY) or AppSetting(key=deadlines.DONE_KEY)
        row.value = sorted({*(row.value or []), deadline_id})
        session.add(row)
        _audit(session, request, user, "deadline.done", deadline_id)
        session.commit()
        return {"done": deadline_id}

    @app.put("/api/studies/{study_id}/glandularity")
    def study_glandularity(study_id: int, body: GlandularityIn, request: Request,
                           user: User = Depends(admin_user), session: Session = Depends(db)):
        """The breast glandularity of a mammography study, typed in (DICOM does not carry it)."""
        st = session.get(Study, study_id)
        if st is None:
            raise HTTPException(404, "study not found")
        if st.modality != "MG":
            raise HTTPException(400, "glandularity is for mammography studies")
        if body.percent is not None and not 0 <= body.percent <= 100:
            raise HTTPException(400, "percent is 0..100")
        before = st.glandularity_pct
        st.glandularity_pct = body.percent
        _audit(session, request, user, "glandularity", f"study:{study_id}",
               {"from": before, "to": body.percent})
        session.commit()
        return {"study_id": study_id, "glandularity_pct": st.glandularity_pct}

    @app.put("/api/studies/{study_id}/indication")
    def study_indication(study_id: int, body: IndicationIn, request: Request,
                         user: User = Depends(admin_user), session: Session = Depends(db)):
        """Code one study by hand (``code`` null: back to automatic matching)."""
        from .. import indications

        if session.get(Study, study_id) is None:
            raise HTTPException(404, "study not found")
        code = _indication_code(session, body.code)
        indications.set_study(session, study_id, code)
        _audit(session, request, user, "indication.study", f"study:{study_id}",
               {"code": body.code})
        session.commit()
        return indications.of_study(session, study_id)

    def _indication_code(session: Session, code: str | None):
        from ..db.models import Code, CodeSystem
        from ..indications import SYSTEM

        if code is None:
            return None
        c = session.scalar(select(Code).join(CodeSystem).where(
            CodeSystem.key == SYSTEM, Code.code == code))
        if c is None:
            raise HTTPException(400, f"unknown indication code {code!r}")
        return c

    @app.get("/api/indications/review")
    def indications_review(limit: int = 200, user: User = Depends(admin_user),
                           session: Session = Depends(db)):
        from .. import indications

        return indications.review(session, min(max(limit, 1), 1000))

    @app.put("/api/indications/mappings")
    def indications_decide(body: IndicationMapIn, request: Request,
                           user: User = Depends(admin_user), session: Session = Depends(db)):
        """One text -> one code for every study with it (``code`` null removes it)."""
        from .. import indications

        code = _indication_code(session, body.code)
        indications.set_mapping(session, body.text, code)
        ids = indications.studies_with_text(session, body.text)
        stats = indications.update(session, ids)
        _audit(session, request, user, "indication.decide", body.text[:120],
               {"code": body.code, "studies": len(ids)})
        session.commit()
        return {"studies": len(ids), "coded": dict(stats)}

    @app.post("/api/studies/{study_id}/his")
    def study_his_requery(study_id: int, request: Request, user: User = Depends(admin_user),
                          session: Session = Depends(db)):
        from ..his import enrich

        if session.get(Study, study_id) is None:
            raise HTTPException(404, "study not found")
        enrich.enqueue(session, [study_id], reset=True)
        _audit(session, request, user, "his.requery", f"study:{study_id}", None)
        session.commit()
        return data.his_summary(session, study_id)

    @app.post("/api/studies/{study_id}/reanalyze")
    def study_reanalyze(study_id: int, request: Request, user: User = Depends(admin_user),
                        session: Session = Depends(db)):
        """Classify and derive this study again from its stored events (nothing is fetched)."""
        from .. import maintenance

        if session.get(Study, study_id) is None:
            raise HTTPException(404, "study not found")
        out = maintenance.reanalyze_studies(session, [study_id])
        _audit(session, request, user, "study.reanalyze", f"study:{study_id}", out)
        session.commit()
        return out

    @app.post("/api/studies/{study_id}/refetch", status_code=202)
    def study_refetch(study_id: int, request: Request, user: User = Depends(admin_user),
                      session: Session = Depends(db)):
        """Forget this study and retrieve it from the PACS again, in a detached process (the
        study is gone until the retrieval has imported it again)."""
        import subprocess
        import sys

        if session.get(Study, study_id) is None:
            raise HTTPException(404, "study not found")
        cmd = [sys.executable, "-m", "dozimetrix.cli"]
        if settings.source:
            cmd += ["--config", str(settings.source)]
        cmd += ["study-refetch", str(study_id), "--by", user.username]
        if not settings.source:
            cmd += ["--url", settings.database_url]
        logfile = settings.data_dir / "jobs.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        _audit(session, request, user, "study.refetch.queued", f"study:{study_id}", None)
        session.commit()
        with open(logfile, "ab") as out:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out,  # noqa: S603
                             stderr=subprocess.STDOUT, start_new_session=True)
        return {"queued": True}

    @app.post("/api/import/sizes")
    def import_sizes(request: Request, apply: bool = False,
                     body: bytes = Body(..., media_type="text/csv"),
                     user: User = Depends(admin_user), session: Session = Depends(db)):
        """Weight / height CSV (raw body). Dry run unless ?apply=1; see sizes_import."""
        from .. import sizes_import

        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = body.decode("cp1250")
        try:
            res = sizes_import.run(session, text, apply=apply)
        except sizes_import.RowError as e:
            session.rollback()
            raise HTTPException(400, str(e)) from e
        if apply:
            _audit(session, request, user, "sizes.import", "csv", res["counts"])
            session.commit()
        else:
            session.rollback()
        return res

    @app.get("/api/studies/{study_id}/skin")
    def study_skin(study_id: int, user: User = Depends(current_user),
                   session: Session = Depends(db)):
        from .. import skindose

        r = skindose.for_study(session, study_id)
        if r is None:
            raise HTTPException(404, "study not found")
        return r

    @app.get("/api/quality")
    def quality_report(q: str = "origin:local date:90d", limit: int = 200,
                       user: User = Depends(reports_user), session: Session = Depends(db)):
        from .. import quality

        def compute():
            conds = data.checked_conditions(q, clock(), data.overrides(session))
            out = quality.run(session, conds, min(max(limit, 1), 1000),
                              names=data.device_names(session), physical=data._physical_ids(session))
            out["q"] = q
            return out

        return rc.get(session, "quality", (q, limit), compute)

    @app.post("/api/studies/{study_id}/identify")
    def identify(study_id: int, request: Request, user: User = Depends(current_user),
                 session: Session = Depends(db)):
        if "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required to see patient identifiers")
        out = data.identify(session, study_id)
        if out is None:
            raise HTTPException(404, "study not found")
        _audit(session, request, user, "identify", f"study:{study_id}")
        session.commit()
        return out

    @app.get("/api/studies/{study_id}/dose-statement")
    def dose_statement(study_id: int, request: Request, user: User = Depends(current_user),
                       session: Session = Depends(db)):
        """The written dose information of art. 4. It names the patient: pid role, audited."""
        if "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required to see patient identifiers")
        out = data.dose_statement(session, study_id)
        if out is None:
            raise HTTPException(404, "study not found")
        _audit(session, request, user, "identify", f"dose-statement:study:{study_id}")
        session.commit()
        return out

    @app.get("/api/stats")
    def stats(q: str = "", metric: str = "dlp_total", by: str = "exam",
              system: str | None = None, exam: str | None = None,
              age_scheme: str = "ro-drl-age", weight_scheme: str = "ext-body-weight",
              bins: int = 0, min_n: int = 1, user: User = Depends(current_user),
              session: Session = Depends(db)):
        from .. import stats as st

        try:
            return rc.get(session, "stats", (q, metric, by, system, exam, age_scheme, weight_scheme,
                                             bins, min_n),
                          lambda: data.stats(session, q, clock(), metric, by, system, exam,
                                             age_scheme, weight_scheme, min(max(bins, 0), 100),
                                             max(min_n, 1)))
        except st.StatsError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/charts/size-bands")
    def size_bands(device: str, kind: str = "weight", other: str = "hide", q: str = "",
                   user: User = Depends(current_user), session: Session = Depends(db)):
        """``device``: one device id, or several separated by commas."""
        try:
            ids = [int(x) for x in device.split(",") if x.strip()]
        except ValueError as e:
            raise HTTPException(400, "device must be an id or a comma-separated list of ids") from e
        try:
            return rc.get(session, "size-bands", (q, tuple(ids), kind, other),
                          lambda: data.size_bands(session, q, clock(), ids, kind, other))
        except data.QueryError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/stats/ct/acquisitions")
    def ct_acquisitions(q: str = "", user: User = Depends(current_user),
                        session: Session = Depends(db)):
        return rc.get(session, "ct-acquisitions", (q,), lambda: data.ct_acquisitions(session, q, clock()))

    @app.get("/api/stats/{group}/rows")
    def group_rows(group: str, q: str = "", user: User = Depends(current_user),
                   session: Session = Depends(db)):
        return rc.get(session, "group-rows", (group, q), lambda: data.group_rows(session, q, clock(), group))

    # ------------------------------------------------------------------ classification

    def _review(fn):
        try:
            return fn()
        except review.ReviewError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/classification/summary")
    def classification_summary(user: User = Depends(current_user),
                               session: Session = Depends(db)):
        return review.cached_summary(session)

    @app.get("/api/classification/unclassified")
    def classification_unclassified(family: str = "rx", limit: int = 100,
                                    user: User = Depends(current_user),
                                    session: Session = Depends(db)):
        return _review(lambda: review.cached_unclassified(session, family, min(max(limit, 1), review.STORE_LIMIT)))

    @app.get("/api/classification/mappings")
    def classification_mappings(user: User = Depends(current_user),
                                session: Session = Depends(db)):
        return review.mappings(session)

    @app.put("/api/classification/mappings")
    def classification_decide(body: DecisionIn, request: Request,
                              user: User = Depends(admin_user),
                              session: Session = Depends(db)):
        out = _review(lambda: review.decide(session, body.family, body.field, body.text,
                                            body.code))
        _audit(session, request, user, "classification.decide",
               f"{body.family}:{body.field}", out)
        session.commit()
        return out

    @app.get("/api/codes/{system}")
    def code_list(system: str, user: User = Depends(current_user),
                  session: Session = Depends(db)):
        return _review(lambda: review.codes(session, system))

    # ------------------------------------------------------------------ users, audit

    def _users(fn):
        from . import users

        try:
            return fn(users)
        except users.UserError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/api/me/password")
    def my_password(body: PasswordIn, request: Request, response: Response,
                    user: User = Depends(current_user), session: Session = Depends(db)):
        """Change one's own password (the account menu)."""
        u = session.get(User, user.id)
        _users(lambda m: m.change_own_password(u, body.current, body.new))
        _audit(session, request, u, "user.password", f"user:{u.username}")
        session.commit()
        set_session_cookie(request, response, u)  # other sessions of this user end here
        return {"ok": True}

    @app.get("/api/users")
    def user_list(user: User = Depends(admin_user), session: Session = Depends(db)):
        from . import users

        return {"users": users.listing(session), "roles": users.ROLE_HELP}

    @app.post("/api/users")
    def user_create(body: UserIn, request: Request, user: User = Depends(admin_user),
                    session: Session = Depends(db)):
        from . import users

        u = _users(lambda m: m.create(session, body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "user.create", f"user:{u.username}",
               {"roles": u.roles, "name": u.display_name})
        session.commit()
        return users.as_dict(u)

    @app.patch("/api/users/{username}")
    def user_update(username: str, body: UserIn, request: Request, response: Response,
                    user: User = Depends(admin_user), session: Session = Depends(db)):
        from . import users

        u, changes = _users(lambda m: m.update(session, user, username,
                                               body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "user.update", f"user:{u.username}", changes)
        session.commit()
        if u.id == user.id:  # an admin editing themself stays logged in here
            set_session_cookie(request, response, u)
        return {**users.as_dict(u), "changes": changes}

    @app.delete("/api/users/{username}")
    def user_delete(username: str, request: Request, user: User = Depends(admin_user),
                    session: Session = Depends(db)):
        gone = _users(lambda m: m.remove(session, user, username))
        _audit(session, request, user, "user.delete", f"user:{username}", gone)
        session.commit()
        return {"ok": True}

    @app.get("/api/audit")
    def audit_log(limit: int = 200, action: str | None = None, user_name: str | None = None,
                  text: str | None = None, since: str | None = None,
                  before_id: int | None = None, user: User = Depends(admin_user),
                  session: Session = Depends(db)):
        import datetime as _dt

        from . import users

        try:
            day = _dt.date.fromisoformat(since) if since else None
        except ValueError as e:
            raise HTTPException(400, "since is YYYY-MM-DD") from e
        return users.audit(session, min(max(limit, 1), 1000), action, user_name, text, day,
                           before_id)

    # ------------------------------------------------------------------ DICOM nodes

    def _nodes(fn):
        from ..net import nodes

        try:
            return fn(nodes)
        except nodes.NodeError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/dicom/status")
    def dicom_status(request: Request, user: User = Depends(current_user),
                     session: Session = Depends(db)):
        import datetime as _dt

        from ..net import nodes, qr

        is_admin = "admin" in user.role_set()
        if is_admin:
            nodes.import_config(session, settings)
            session.commit()
        today = clock().date()
        done = qr.done_days(session)
        days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(119, -1, -1)]
        return {
            "local": {"aet": settings.dicom_aet, "port": settings.dicom_port,
                      "bind": settings.dicom_bind, "ips": _host_ips(),
                      "web": f"{request.url.hostname}:{request.url.port or (443 if request.url.scheme == 'https' else 80)}",
                      "web_listen": f"{settings.web_host}:{settings.web_port}",
                      "allowed": nodes.allowed_senders(session, settings.dicom_allowed_aets)
                      if is_admin else []},
            "receiver": nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=120),
            "qr": nodes.read_status(session, nodes.QR_KEY, stale_after=nodes.QR_STALE_S),
            "coverage": [{"date": d, "done": d in done} for d in days],
            "nodes": [nodes.as_dict(n) if is_admin else {"id": n.id, "name": n.name}
                      for n in session.scalars(select(DicomNode).order_by(DicomNode.name))],
        }

    @app.get("/api/backup/status")
    def backup_status(user: User = Depends(admin_user)):
        """Read-only: when deploy/backup/dozimetrix-backup last finished well (its marker file)."""
        marker = Path(settings.data_dir) / "backup-last-ok"
        try:
            ts = int(marker.read_text().strip())
        except (OSError, ValueError):
            return {"state": "never", "last_ok": None, "age_h": None, "max_hours": 26}
        last = dt.datetime.fromtimestamp(ts, dt.UTC)
        age_h = max(0.0, (dt.datetime.now().timestamp() - ts) / 3600)
        return {"state": "ok" if age_h <= 26 else "stale", "last_ok": last.isoformat(),
                "age_h": round(age_h, 1), "max_hours": 26}

    @app.post("/api/dicom/nodes")
    def dicom_create(body: NodeIn, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        n = _nodes(lambda nodes: nodes.create(session, body.model_dump(exclude_unset=True)))
        from ..net.nodes import as_dict

        out = as_dict(n)
        _audit(session, request, user, "dicom_node.create", f"node:{n.name}",
               {k: v for k, v in out.items() if k != "last_echo"})
        session.commit()
        return out

    @app.patch("/api/dicom/nodes/{node_id}")
    def dicom_update(node_id: int, body: NodeIn, request: Request,
                     user: User = Depends(admin_user), session: Session = Depends(db)):
        n, changes = _nodes(lambda nodes: nodes.update(
            session, node_id, body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "dicom_node.update", f"node:{n.name}", changes)
        session.commit()
        return {"id": n.id, "changes": changes}

    @app.delete("/api/dicom/nodes/{node_id}")
    def dicom_delete(node_id: int, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        d = _nodes(lambda nodes: nodes.delete(session, node_id))
        _audit(session, request, user, "dicom_node.delete", f"node:{d['name']}", d)
        session.commit()
        return {"deleted": d["name"]}

    @app.post("/api/dicom/nodes/{node_id}/echo")
    def dicom_echo(node_id: int, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        res = _nodes(lambda nodes: nodes.echo(session, node_id, settings.dicom_aet))
        session.commit()
        return res

    # ------------------------------------------------------------------ HIS servers

    def _his(fn):
        from ..his import servers

        try:
            return fn(servers)
        except servers.ServerError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/his/status")
    def his_status(user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import AppSetting, HisLookup, HisServer
        from ..his import servers

        row = session.get(AppSetting, servers.STATUS_KEY)
        worker = dict(row.value or {}) if row else None
        if worker and worker.get("at"):
            age = (clock() - dt.datetime.fromisoformat(worker["at"])).total_seconds()
            worker["stale"] = age > 600
        queue = dict(session.execute(select(HisLookup.status, func.count())
                                     .group_by(HisLookup.status)).all())
        return {"worker": worker, "queue": queue, "waiting": his_waiting(session),
                "servers": [servers.as_dict(s) if "admin" in user.role_set()
                            else {"id": s.id, "name": s.name, "enabled": s.enabled}
                            for s in session.scalars(select(HisServer).order_by(HisServer.name))]}

    # ------------------------------------------------------------------ live (SSE)
    # The receiver, PACS sync and HIS worker are separate processes that only meet the web
    # server in the database, so the stream reads a small snapshot every LIVE_EVERY_S and
    # sends a topic only when it changed. One open tab = one idle async task, no thread.

    def study_stamp(session, sid: int) -> dict | None:
        """A fingerprint of one study: its row, events, alerts, HIS outcome and patient."""
        from ..db.models import Alert, Event, HisLookup, Patient, Study

        st = session.get(Study, sid)
        if st is None:
            return None
        ev = session.execute(select(func.count(Event.id), func.max(Event.id))
                             .where(Event.study_id == sid)).one()
        al = session.execute(select(Alert.id, Alert.status).where(Alert.study_id == sid)
                             .order_by(Alert.id)).all()
        his = session.get(HisLookup, sid)
        pat = session.get(Patient, st.patient_id) if st.patient_id else None
        return {"at": st.updated_at, "events": list(ev), "alerts": [list(a) for a in al],
                "his": [his.status, his.fetched_at] if his else None,
                "patient": pat.updated_at if pat else None}

    def his_waiting(session) -> dict:
        """Why pending HIS lookups wait: due now, a retry later (forms come late) or history
        held for the server's backfill hours (the same rules as the worker's queue)."""
        from ..db.models import HisLookup, Study
        from ..his import enrich, servers

        now = dt.datetime.now()  # the worker's clock
        active = servers.active(session)
        hours = active[0].backfill_hours if active else None
        open_hours = enrich._in_hours(hours, now)
        pend = HisLookup.status == "pending"
        later = HisLookup.next_try_at > now
        retry, next_at = session.execute(
            select(func.count(), func.min(HisLookup.next_try_at)).where(pend, later)).one()
        history = 0
        if not open_hours:
            history = session.scalar(
                select(func.count()).select_from(HisLookup)
                .join(Study, Study.id == HisLookup.study_id)
                .where(pend, ~later | HisLookup.next_try_at.is_(None),
                       (Study.study_datetime < now - enrich.RECENT)
                       | Study.study_datetime.is_(None))) or 0
        total = session.scalar(select(func.count()).select_from(HisLookup).where(pend)) or 0
        return {"due": total - retry - history, "retry": retry,
                "next_retry": next_at.isoformat(timespec="minutes") if next_at else None,
                "history": history, "backfill_hours": hours}

    def live_snapshot(topics: set[str]) -> dict:
        from ..db.models import AppSetting, HisLookup, Study
        from ..his import servers
        from ..net import nodes

        out: dict = {}
        with maker() as session:
            if "pulse" in topics:  # cheap: pages refetch their own data when it moves
                from .. import alerts as _alerts
                from ..db.models import Alert

                out["pulse"] = {"study": session.scalar(select(func.max(Study.id))),
                                "alert": session.scalar(select(func.max(Alert.id))),
                                "alerts_open": _alerts.open_count(session)}
            if "services" in topics:
                out["services"] = services_health(session)
            if "studies" in topics:
                # per modality, from the latest 2000 studies (PK scan): the newest id and the
                # last change (more images, HIS details, a new classification)
                newest: dict[str, list] = {}
                for sid, mod, upd in session.execute(
                        select(Study.id, Study.modality, Study.updated_at)
                        .order_by(Study.id.desc()).limit(2000)):
                    cur = newest.setdefault(mod or "?", [sid, upd])
                    if upd and (cur[1] is None or upd > cur[1]):
                        cur[1] = upd
                out["studies"] = newest
            for t in topics:  # "study:<id>": what an open study page shows changed
                if t.startswith("study:"):
                    out[t] = study_stamp(session, int(t[6:]))
            if "alerts" in topics:
                from .. import alerts as _alerts
                from ..db.models import Alert

                out["alerts"] = {"open": _alerts.open_count(session),
                                 "newest": session.scalar(select(func.max(Alert.id)))}
            if "dicom" in topics:
                out["dicom"] = {
                    "receiver": nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=120),
                    "qr": nodes.read_status(session, nodes.QR_KEY, stale_after=nodes.QR_STALE_S)}
            if "maintenance" in topics:
                from .. import jobs as _jobs

                out["maintenance"] = {"current": _jobs.current(session, _jobs.lock_path(settings.data_dir))}
            if "his" in topics:
                row = session.get(AppSetting, servers.STATUS_KEY)
                worker = dict(row.value or {}) if row else None
                if worker and worker.get("at"):
                    age = (clock() - dt.datetime.fromisoformat(worker["at"])).total_seconds()
                    worker["stale"] = age > 600
                queue = dict(session.execute(select(HisLookup.status, func.count())
                                             .group_by(HisLookup.status)).all())
                recent = session.execute(
                    select(HisLookup, Study.accession, Study.accession_is_hashed,
                           Study.study_datetime)
                    .join(Study, Study.id == HisLookup.study_id)
                    .where(HisLookup.fetched_at.is_not(None))
                    .order_by(HisLookup.fetched_at.desc()).limit(20)).all()
                out["his"] = {"worker": worker, "queue": queue, "waiting": his_waiting(session),
                              "recent": [{
                    "study_id": lk.study_id, "status": lk.status,
                    "at": lk.fetched_at.isoformat(timespec="seconds"),
                    "study_at": when.isoformat(timespec="minutes") if when else None,
                    "accession": None if hashed else acc, "request_id": lk.request_id,
                    "score": lk.match_score, "how": (lk.detail or {}).get("type"),
                    "written": (lk.detail or {}).get("written"),
                    "next_try": lk.next_try_at.isoformat(timespec="minutes")
                    if lk.status == "pending" and lk.next_try_at else None}
                    for lk, acc, hashed, when in recent]}
        return out

    live_shared: dict[str, tuple[float, object]] = {}  # topic -> (when, value)

    def shared_snapshot(topics: set[str]) -> dict:
        """``live_snapshot``, but a topic computed less than LIVE_SHARE_S ago is reused: every
        open tab runs its own stream, and the same few queries would run for each of them
        every few seconds. (Per-study topics are not shared.)"""
        import time as _time

        now_t = _time.monotonic()
        out: dict = {}
        need = {t for t in topics if t.startswith("study:")}
        for t in topics - need:
            hit = live_shared.get(t)
            if hit and now_t - hit[0] < LIVE_SHARE_S:
                out[t] = hit[1]
            else:
                need.add(t)
        if need:
            fresh = live_snapshot(need)
            for t, v in fresh.items():
                if not t.startswith("study:"):
                    live_shared[t] = (now_t, v)
            out.update(fresh)
        return out

    def services_health(session: Session) -> list[dict]:
        """The background services, for any signed-in user: state ok | warn | alert | idle."""
        from .. import __version__
        from ..db.models import AppSetting
        from ..his import servers
        from ..net import nodes

        out = [{"key": "web", "name": "Web server", "state": "ok", "detail": f"v{__version__}"}]
        rx = nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=120)
        spool = (rx or {}).get("spool") or {}
        out.append({"key": "receiver", "name": "DICOM receiver",
                    "state": "alert" if not rx or not rx["running"] else
                    "warn" if spool.get("derive_gave_up") else "ok",
                    "at": rx and rx["at"], "age_s": rx and rx["age_s"],
                    "detail": "never started" if not rx else
                    f"{rx.get('aet')} :{rx.get('port')} · {rx.get('received', 0)} received"
                    + (f" · {spool['derive_pending']} to process" if spool.get("derive_pending")
                       else "")})
        qr = nodes.read_status(session, nodes.QR_KEY, stale_after=nodes.QR_STALE_S)
        out.append({"key": "qr", "name": "PACS sync",
                    "state": "ok" if qr and qr["running"] else "idle",
                    "at": qr and qr["at"], "age_s": qr and qr["age_s"],
                    "detail": _qr_detail(qr)})
        if servers.active(session):
            row = session.get(AppSetting, servers.STATUS_KEY)
            w = dict(row.value or {}) if row else {}
            age = int((clock() - dt.datetime.fromisoformat(w["at"])).total_seconds()) \
                if w.get("at") else None
            wt = his_waiting(session)
            parts = [f"{wt['due']} due" if wt["due"] else "",
                     f"{wt['retry']} retry later" if wt["retry"] else "",
                     f"{wt['history']} wait for {wt['backfill_hours']}" if wt["history"] else ""]
            waiting = " · ".join(p for p in parts if p)
            out.append({"key": "his", "name": "HIS worker",
                        "state": "alert" if w.get("error") or age is None or age > 600 else "ok",
                        "at": w.get("at"), "age_s": age,
                        "detail": w.get("error") or (waiting or "queue empty")})
        else:
            out.append({"key": "his", "name": "HIS worker", "state": "idle",
                        "detail": "no HIS server enabled"})
        return out

    def _qr_detail(qr: dict | None) -> str:
        """'2013-01-17: {…counters…}; imported {…}' -> 'at 2013-01-17'; other messages as is."""
        msg = (qr or {}).get("message") or ""
        if not msg:
            return "not run yet"
        m = re.match(r"(\d{4}-\d{2}-\d{2}):", msg)
        return f"{'at' if qr['running'] else 'last'} {m.group(1)}" if m else msg[:80]

    @app.get("/api/live")
    async def live(request: Request, topics: str = "pulse"):
        import asyncio
        import json as _json

        from starlette.concurrency import run_in_threadpool

        def who():
            with maker() as session:
                return current_user(request, session)

        user = await run_in_threadpool(who)  # 401 before the stream opens
        if len(topics) > 1024:
            raise HTTPException(400, "too many topics")
        want = {t for t in topics.split(",")
                if t in LIVE_TOPICS or re.fullmatch(r"study:\d{1,12}", t)}
        if len([t for t in want if t.startswith("study:")]) > 3:
            raise HTTPException(400, "at most 3 study topics")
        if len(want) > LIVE_MAX_TOPICS:
            raise HTTPException(400, f"at most {LIVE_MAX_TOPICS} topics")
        open_streams = app.state.live_streams
        if open_streams.get(user.id, 0) >= LIVE_MAX_STREAMS:
            raise HTTPException(429, "too many live streams open")
        uid = user.id

        def still_allowed() -> bool:
            try:
                who()
                return True
            except HTTPException:
                return False
        if "admin" not in user.role_set():
            want -= {"his", "maintenance"}  # admin-only topics

        async def stream():
            open_streams[uid] = open_streams.get(uid, 0) + 1
            try:
                async for chunk in _stream():
                    yield chunk
            finally:
                open_streams[uid] -= 1
                if open_streams[uid] <= 0:
                    open_streams.pop(uid, None)

        async def _stream():
            last: dict = {}
            quiet = elapsed = 0.0
            yield f"retry: {LIVE_RETRY_MS}\n\n"
            due_at: dict[str, float] = {}
            while True:
                now_t = elapsed
                topics_now = {t for t in want if due_at.get(t, 0.0) <= now_t}
                for t in topics_now & LIVE_TOPIC_EVERY_S.keys():
                    due_at[t] = now_t + LIVE_TOPIC_EVERY_S[t]
                snap = await run_in_threadpool(shared_snapshot, topics_now) if topics_now else {}
                for topic, value in snap.items():
                    # ages tick every second: compare without them, send them along
                    key = _json.dumps(value, sort_keys=True, default=str,
                                      separators=(",", ":"))
                    key = re.sub(r'"age_s":\d+,?', "", key)
                    if last.get(topic) != key:
                        last[topic] = key
                        quiet = 0.0
                        yield f"event: {topic}\ndata: {_json.dumps(value, default=str)}\n\n"
                if elapsed >= LIVE_MAX_S or await request.is_disconnected():
                    return  # the browser reconnects after `retry`, with a fresh state
                if elapsed and elapsed % LIVE_RECHECK_S < LIVE_EVERY_S \
                        and not await run_in_threadpool(still_allowed):
                    return  # logged out, disabled, or the password / roles changed
                await asyncio.sleep(LIVE_EVERY_S)
                quiet += LIVE_EVERY_S
                elapsed += LIVE_EVERY_S
                if quiet >= LIVE_PING_S:  # keep proxies from closing an idle stream
                    quiet = 0.0
                    yield ": ping\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/his/servers")
    def his_create(body: HisServerIn, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        s = _his(lambda sv: sv.create(session, secret, body.model_dump(exclude_unset=True)))
        from ..his.servers import as_dict

        out = as_dict(s)
        _audit(session, request, user, "his_server.create", f"his:{s.name}",
               {k: v for k, v in out.items() if k != "last_check"})
        session.commit()
        return out

    @app.patch("/api/his/servers/{server_id}")
    def his_update(server_id: int, body: HisServerIn, request: Request,
                   user: User = Depends(admin_user), session: Session = Depends(db)):
        s, changes = _his(lambda sv: sv.update(session, secret, server_id,
                                               body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "his_server.update", f"his:{s.name}", changes)
        session.commit()
        return {"id": s.id, "changes": changes}

    @app.delete("/api/his/servers/{server_id}")
    def his_delete(server_id: int, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        d = _his(lambda sv: sv.delete(session, server_id))
        _audit(session, request, user, "his_server.delete", f"his:{d['name']}", d)
        session.commit()
        return {"deleted": d["name"]}

    @app.post("/api/his/servers/{server_id}/check")
    def his_check(server_id: int, user: User = Depends(admin_user),
                  session: Session = Depends(db)):
        res = _his(lambda sv: sv.check(session, secret, server_id))
        session.commit()
        return res

    # ------------------------------------------------------------------ alerts

    def _alert_filters(rule, level, since, until, modality):
        import datetime as _dt

        try:
            return {"rule": rule or None, "level": level or None, "modality": modality or None,
                    "since": _dt.datetime.fromisoformat(since) if since else None,
                    "until": (_dt.datetime.fromisoformat(until) + _dt.timedelta(days=1))
                    if until else None}
        except ValueError as e:
            raise HTTPException(400, "since/until are YYYY-MM-DD") from e

    @app.get("/api/alerts")
    def alert_list(status: str = "open", limit: int = 50, offset: int = 0,
                   rule: str | None = None, level: str | None = None,
                   since: str | None = None, until: str | None = None,
                   modality: str | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db)):
        from .. import alerts

        return alerts.listing(session, None if status == "all" else status,
                              min(max(limit, 1), 500), names=data.device_names(session),
                              offset=max(offset, 0),
                              **_alert_filters(rule, level, since, until, modality))

    @app.get("/api/alerts/summary")
    def alert_summary(status: str = "open", rule: str | None = None, level: str | None = None,
                      since: str | None = None, until: str | None = None,
                      modality: str | None = None, user: User = Depends(current_user),
                      session: Session = Depends(db)):
        from .. import alerts

        return alerts.counts(session, None if status == "all" else status,
                             **_alert_filters(rule, level, since, until, modality))

    @app.post("/api/alerts/ack")
    def alert_ack_many(body: AckManyIn, request: Request, user: User = Depends(current_user),
                       session: Session = Depends(db)):
        from .. import alerts

        admin_only(user)
        if not body.ids or len(body.ids) > 1000:
            raise HTTPException(400, "1 to 1000 alert ids")
        n = alerts.acknowledge(session, body.ids, user.username, body.note, clock())
        _audit(session, request, user, "alert.ack", f"alerts:{n}",
               {"ids": body.ids[:50], "note": body.note})
        session.commit()
        return {"acknowledged": n}

    @app.get("/api/alerts/{alert_id}/studies")
    def alert_studies(alert_id: int, user: User = Depends(current_user),
                      session: Session = Depends(db)):
        from .. import alerts

        rows = alerts.window_studies(session, alert_id, names=data.device_names(session))
        if rows is None:
            raise HTTPException(404, "alert not found")
        return rows

    @app.post("/api/alerts/{alert_id}/ack")
    def alert_ack(alert_id: int, body: AckIn, request: Request,
                  user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import Alert

        admin_only(user)
        a = session.get(Alert, alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        a.status, a.ack_by, a.ack_at = "acknowledged", user.username, clock()
        a.ack_note = (body.note or "")[:1024] or None
        _audit(session, request, user, "alert.ack", f"alert:{alert_id}",
               {"study_id": a.study_id, "note": a.ack_note})
        session.commit()
        return {"id": a.id, "status": a.status, "ack_by": a.ack_by}

    @app.get("/api/followups")
    def followups(status: str | None = None, limit: int = 200,
                  user: User = Depends(reports_user), session: Session = Depends(db)):
        """High-dose follow-ups (fluoroscopy / interventional): pending = open or informed."""
        from .. import followup

        if status not in (None, "pending", *followup.STATUSES):
            raise HTTPException(400, "status is pending, " + ", ".join(followup.STATUSES))
        return followup.listing(session, status, clock().date(), max(1, min(limit, 1000)),
                                names=data.device_names(session))

    @app.post("/api/followups/{followup_id}")
    def followup_update(followup_id: int, body: FollowupIn, request: Request,
                        user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import followup

        admin_only(user)
        try:
            f = followup.update(session, followup_id, user.username, clock(),
                                status=body.status, outcome=body.outcome, note=body.note,
                                check_due=body.check_due)
        except followup.FollowupError as e:
            raise HTTPException(404 if "not found" in str(e) else 400, str(e)) from None
        _audit(session, request, user, "followup", f"followup:{followup_id}",
               {"study_id": f.study_id, "status": f.status, "outcome": f.outcome})
        session.commit()
        return {"id": f.id, "status": f.status}

    @app.get("/api/followups/{followup_id}/letter", response_class=HTMLResponse)
    def followup_letter(followup_id: int, request: Request, lang: str = "ro",
                        user: User = Depends(current_user), session: Session = Depends(db)):
        """The patient letter, ready to print. It names the patient: pid role, audited."""
        from .. import followup

        if "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required to see patient identifiers")
        try:
            page = followup.letter(session, followup_id, lang)
        except followup.FollowupError as e:
            raise HTTPException(400, str(e)) from None
        if page is None:
            raise HTTPException(404, "follow-up not found")
        _audit(session, request, user, "identify", f"followup-letter:{followup_id}")
        session.commit()
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/api/incidents")
    def incidents(status: str | None = None, limit: int = 200,
                  user: User = Depends(reports_user), session: Session = Depends(db)):
        from .. import incidents as inc

        try:
            return inc.listing(session, status, max(1, min(limit, 1000)))
        except inc.IncidentError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/incidents/{incident_id}")
    def incident_detail(incident_id: int, user: User = Depends(reports_user),
                        session: Session = Depends(db)):
        from .. import incidents as inc

        out = inc.detail(session, incident_id)
        if out is None:
            raise HTTPException(404, "incident not found")
        return out

    @app.post("/api/incidents")
    def incident_open(body: IncidentIn, request: Request, user: User = Depends(current_user),
                      session: Session = Depends(db)):
        from .. import incidents as inc

        admin_only(user)
        try:
            i = inc.open_incident(session, user.username, clock(), alert_id=body.alert_id,
                                  study_id=body.study_id, title=body.title)
        except inc.IncidentError as e:
            raise HTTPException(404 if "not found" in str(e) else 400, str(e)) from None
        _audit(session, request, user, "incident", f"incident:{i.id}",
               {"alert_id": i.alert_id, "study_id": i.study_id})
        session.commit()
        return {"id": i.id, "status": i.status}

    @app.post("/api/incidents/{incident_id}")
    def incident_update(incident_id: int, body: IncidentPatch, request: Request,
                        user: User = Depends(current_user), session: Session = Depends(db)):
        """Change fields, add a note, close or reopen (action); every change is logged."""
        from .. import incidents as inc

        admin_only(user)
        fields = body.model_dump(exclude_unset=True)
        text, action = fields.pop("note", None), fields.pop("action", None)
        if action not in (None, "close", "reopen"):
            raise HTTPException(400, "action is close or reopen")
        now = clock()
        try:
            if action == "reopen":
                inc.reopen(session, incident_id, user.username, now, text or "")
                text = None
            if fields:
                inc.update(session, incident_id, user.username, now, **fields)
            if text:
                inc.note(session, incident_id, user.username, now, text)
            if action == "close":
                inc.close(session, incident_id, user.username, now)
        except inc.IncidentError as e:
            session.rollback()
            raise HTTPException(404 if "not found" in str(e) else 400, str(e)) from None
        _audit(session, request, user, "incident", f"incident:{incident_id}",
               {"fields": sorted(fields), "action": action})
        session.commit()
        return inc.detail(session, incident_id)

    @app.get("/api/digests")
    def digests_get(user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import digests

        admin_only(user)
        return digests.load(session)

    @app.put("/api/digests")
    def digests_put(request: Request, items: list[dict] = Body(...),
                    user: User = Depends(current_user), session: Session = Depends(db)):
        """Replace the monthly digest schedules: name, recipients (to), study query (q)."""
        from .. import digests

        admin_only(user)
        try:
            out = digests.save(session, items)
        except digests.DigestError as e:
            raise HTTPException(400, str(e)) from None
        _audit(session, request, user, "digests", "digests", {"n": len(out)})
        session.commit()
        return out

    @app.get("/api/digests/{digest_id}/preview")
    def digest_preview(digest_id: str, user: User = Depends(current_user),
                       session: Session = Depends(db)):
        """The last month's digest as it would be mailed (nothing is sent)."""
        from .. import digests

        admin_only(user)
        d = next((x for x in digests.load(session) if x["id"] == digest_id), None)
        if d is None:
            raise HTTPException(404, "digest not found")
        now = clock()
        start, end = digests.previous_month(now)
        rep = digests.build(session, d["q"], start, end, now)
        subject, text, page = digests.render(d["name"], rep)
        return {"subject": subject, "text": text, "html": page, "data": rep}

    @app.get("/api/devices/{device_id}/qa")
    def device_qa(device_id: int, user: User = Depends(reports_user),
                  session: Session = Depends(db)):
        """The device's QA and calibration register, newest first."""
        from .. import qa

        return qa.entries(session, device_id)

    @app.post("/api/devices/{device_id}/qa")
    def device_qa_add(device_id: int, body: QAIn, request: Request,
                      user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import qa

        admin_only(user)
        try:
            e = qa.add(session, device_id, user.username, **body.model_dump())
        except qa.QAError as exc:
            raise HTTPException(404 if "not found" in str(exc) else 400, str(exc)) from None
        _audit(session, request, user, "qa", f"device:{device_id}",
               {"kind": e.kind, "done_on": e.done_on.isoformat(), "factor": e.factor})
        session.commit()
        return {"id": e.id}

    @app.delete("/api/qa/{entry_id}")
    def device_qa_delete(entry_id: int, request: Request, user: User = Depends(current_user),
                         session: Session = Depends(db)):
        from .. import qa

        admin_only(user)
        try:
            device_id = qa.remove(session, entry_id)
        except qa.QAError as exc:
            raise HTTPException(404, str(exc)) from None
        _audit(session, request, user, "qa.delete", f"device:{device_id}", {"entry": entry_id})
        session.commit()
        return {"deleted": entry_id}

    @app.get("/api/qa/due")
    def qa_due(days: int = 30, user: User = Depends(reports_user),
               session: Session = Depends(db)):
        """QA tests and calibrations overdue or due within ``days``."""
        from .. import qa

        return qa.due(session, clock().date(), max(0, min(days, 365)),
                      names=data.device_names(session))

    @app.get("/api/ct-kfactors")
    def ct_kfactors(user: User = Depends(current_user), session: Session = Depends(db)):
        """The DLP to effective dose coefficients entered for CT (none ship), with their source."""
        from .. import ctdose
        from ..db.models import CtKFactor

        return {"regions": list(ctdose.REGIONS),
                "rows": [{"region": c.region, "age_to_years": c.age_to_years,
                          "phantom_cm": c.phantom_cm, "k": c.k, "source": c.source}
                         for c in session.scalars(select(CtKFactor).order_by(
                             CtKFactor.region, CtKFactor.phantom_cm, CtKFactor.age_to_years))]}

    @app.put("/api/ct-kfactors")
    def ct_kfactors_put(request: Request, rows: list[dict] = Body(...),
                        user: User = Depends(current_user), session: Session = Depends(db)):
        """Replace the coefficients (an empty list removes them); admin, audited."""
        from .. import ctdose

        admin_only(user)
        try:
            parsed = [(str(r.get("region") or "").strip(),
                       None if r.get("age_to_years") in (None, "") else float(r["age_to_years"]),
                       int(r.get("phantom_cm")), float(r.get("k")), str(r.get("source") or ""))
                      for r in rows]
            n = ctdose.set_rows(session, parsed)
        except (ValueError, TypeError) as e:  # CtDoseError is a ValueError
            raise HTTPException(400, str(e)) from None
        _audit(session, request, user, "config", "ct-kfactors", {"rows": n})
        session.commit()
        return {"rows": n}

    @app.get("/api/alert-rules")
    def alert_rules(user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import AlertRule
        from ..seeds.loader import _toml

        seeded = {r["key"] for r in _toml("alert_rules.toml")["rule"]}
        return [_rule_dict(r, seeded) for r in session.scalars(select(AlertRule).order_by(AlertRule.id))]

    def _rule_dict(r, seeded: set[str]) -> dict:
        cols = ("id", "key", "name", "enabled", "kind", "modality", "metric", "threshold",
                "unit", "window_days", "max_age_days", "severity", "notify", "note")
        return {**{c: getattr(r, c) for c in cols}, "custom": r.key not in seeded}

    # ------------------------------------------------------------ site settings

    @app.get("/api/maintenance")
    def maintenance_info(user: User = Depends(admin_user), session: Session = Depends(db)):
        from .. import jobs

        return jobs.overview(session, jobs.lock_path(settings.data_dir))

    @app.post("/api/maintenance/{name}")
    def maintenance_start(name: str, body: JobIn, request: Request,
                          user: User = Depends(admin_user), session: Session = Depends(db)):
        from .. import jobs

        if name not in jobs.JOBS:
            raise HTTPException(404, "unknown job")
        days = body.days if jobs.JOBS[name]["days"] else None
        try:
            state = jobs.start(settings, session, name, days, user.username)
        except jobs.Busy as e:
            raise HTTPException(409, str(e)) from e
        _audit(session, request, user, "maintenance.start", name, {"days": days})
        session.commit()
        return state

    @app.get("/api/settings")
    def site_settings(user: User = Depends(admin_user), session: Session = Depends(db)):
        from sqlalchemy.engine import make_url

        from .. import __version__, siteconf
        from ..db.models import AppSetting
        from ..setup import SITE_KEY

        site = dict((session.get(AppSetting, SITE_KEY) or AppSetting(key=SITE_KEY)).value or {})
        ui = siteconf.stored(session)
        toml_url = (settings.notify or {}).get("base_url")
        url = make_url(settings.database_url)
        return {
            "site": {"name": site.get("name"), "code": site.get("code")},
            "base_url": {"value": ui.get("base_url") or toml_url,
                         "source": "ui" if ui.get("base_url") else ("toml" if toml_url else None)},
            "spool_retention_days": {
                "value": ui.get("spool_retention_days", settings.spool_retention_days),
                "source": "ui" if "spool_retention_days" in ui else "toml",
                "toml": settings.spool_retention_days},
            # read-only: set in dozimetrix.toml, a change needs a restart
            "deployment": {
                "version": __version__,
                "config_file": str(settings.source) if settings.source else None,
                "database": {"driver": url.drivername, "host": url.host, "name": url.database},
                "dicom": {"aet": settings.dicom_aet, "port": settings.dicom_port,
                          "bind": settings.dicom_bind,
                          "allowed_aets": settings.dicom_allowed_aets},
                "web": {"listen": f"{settings.web_host}:{settings.web_port}",
                        "trusted_proxies": settings.web_trusted_proxies,
                        "allowed_hosts": settings.web_allowed_hosts,
                        "secure_cookies": settings.secure_cookies,
                        "session_hours": settings.session_hours,
                        "max_upload_mb": settings.max_upload_mb},
                "paths": {"data_dir": str(settings.data_dir), "spool_dir": str(settings.spool_dir),
                          "secret_file": str(settings.secret_file),
                          "specs_dir": str(settings.specs_dir),
                          "plugins_dir": str(settings.plugins_dir)},
            },
        }

    @app.put("/api/settings")
    def site_settings_put(body: SettingsIn, request: Request, user: User = Depends(admin_user),
                          session: Session = Depends(db)):
        from .. import siteconf
        from ..setup import SITE_KEY, SetupError

        patch = body.model_dump(exclude_unset=True)
        changes = {}
        try:
            if "name" in patch or "code" in patch:
                from ..db.models import AppSetting

                cur = dict((session.get(AppSetting, SITE_KEY) or AppSetting(key=SITE_KEY)).value
                           or {})
                new = first_run.save_site(session, {"name": patch.get("name", cur.get("name")),
                                                    "code": patch.get("code", cur.get("code"))})
                changes.update({k: {"from": cur.get(k), "to": new[k]}
                                for k in ("name", "code") if cur.get(k) != new[k]})
            changes.update(siteconf.save(session, siteconf.clean(patch)))
        except (SetupError, siteconf.SiteConfError) as e:
            session.rollback()
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "settings.update", "site", changes)
        session.commit()
        return {"changes": changes}

    # ------------------------------------------------------------ notification channels

    def _notify_stored(session: Session) -> dict:
        from ..db.models import AppSetting
        from ..notify import SETTINGS_KEY

        row = session.get(AppSetting, SETTINGS_KEY)
        return dict(row.value or {}) if row else {}

    @app.get("/api/notify")
    def notify_get(user: User = Depends(admin_user), session: Session = Depends(db)):
        from datetime import timedelta

        from ..db.models import Alert, AlertRule
        from ..notify import UI_FIELDS, load_settings

        stored, toml = _notify_stored(session), settings.notify or {}
        cfg = load_settings(session, toml, secret)
        fields = {k: {"value": getattr(cfg, k),
                      "source": "ui" if k in stored else ("toml" if k in toml else None)}
                  for k in UI_FIELDS}
        since = clock() - timedelta(hours=24)
        count = lambda *w: session.scalar(  # noqa: E731
            select(func.count()).select_from(Alert).join(AlertRule, AlertRule.id == Alert.rule_id)
            .where(AlertRule.notify.is_(True), *w))
        last = session.scalars(
            select(Alert.notify_error).where(Alert.notify_error.like("failed%"))
            .order_by(Alert.id.desc()).limit(1)).first()
        return {
            "fields": fields, "channels": cfg.channels,
            "smtp_password_set": bool(cfg.smtp_password or cfg.smtp_password_file),
            "ntfy_token_set": bool(cfg.ntfy_token or cfg.ntfy_token_file),
            "status": {
                "waiting": count(Alert.notified_at.is_(None)),
                "sent_24h": count(Alert.notified_at >= since, Alert.notified_channels.is_not(None)),
                "failed": count(Alert.notify_error.like("failed%")),
                "last_error": last,
            },
        }

    @app.put("/api/notify")
    def notify_put(body: NotifyIn, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        from ..db.models import AppSetting
        from ..notify import SECRET_FIELDS, SETTINGS_KEY, UI_FIELDS

        patch = body.model_dump(exclude_unset=True)
        addr = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
        v = (patch.get("ntfy_url") or "").strip()
        if v and not re.match(r"^https?://[^\s/]+", v):
            raise HTTPException(400, "ntfy_url must be an http:// or https:// URL")
        if patch.get("smtp_port") is not None and not 1 <= patch["smtp_port"] <= 65535:
            raise HTTPException(400, "smtp_port is 1-65535")
        if patch.get("email_to") is not None:
            patch["email_to"] = [a.strip() for a in patch["email_to"] if a.strip()]
            if not all(addr.match(a) for a in patch["email_to"]):
                raise HTTPException(400, "email_to has an address that is not valid")
        if patch.get("email_from") and not addr.match(patch["email_from"].strip()):
            raise HTTPException(400, "email_from is not a valid address")
        stored = _notify_stored(session)
        before, changes = dict(stored), {}
        for k in UI_FIELDS:
            if k not in patch:
                continue
            v = patch[k]
            if isinstance(v, str):
                v = v.strip()
            if v in (None, "", []):
                stored.pop(k, None)  # back to the toml value
            else:
                stored[k] = v
            if before.get(k) != stored.get(k):
                changes[k] = {"from": before.get(k), "to": stored.get(k)}
        from .. import secretbox

        for attr, col in SECRET_FIELDS.items():
            if patch.get(f"clear_{attr}"):
                if stored.pop(col, None):
                    changes[attr] = "cleared"
            elif patch.get(attr):
                stored[col] = secretbox.seal(secret, patch[attr])
                changes[attr] = "set"  # never the value, here or in the audit log
        row = session.get(AppSetting, SETTINGS_KEY) or AppSetting(key=SETTINGS_KEY)
        row.value = stored
        session.merge(row)
        _audit(session, request, user, "notify.update", "notify", changes)
        session.commit()
        return {"changes": changes}

    @app.post("/api/notify/test")
    def notify_test(body: NotifyTestIn, request: Request, user: User = Depends(admin_user),
                    session: Session = Depends(db)):
        from .. import notify

        if body.channel not in notify.SENDERS:
            raise HTTPException(400, "channel is email|ntfy")
        cfg = notify.load_settings(session, settings.notify, secret)
        try:
            notify.send_test(cfg, body.channel, getattr(app.state, "notify_senders", None))
            ok, err = True, None
        except Exception as exc:  # noqa: BLE001 – the admin needs to read why
            ok, err = False, f"{type(exc).__name__}: {exc}"[:300]
        _audit(session, request, user, "notify.test", body.channel, {"ok": ok, "error": err})
        session.commit()
        return {"ok": ok, "error": err}

    @app.get("/api/alert-rules/metrics")
    def alert_rule_metrics(user: User = Depends(current_user)):
        """The metrics a new rule can watch (the form offers these)."""
        from ..stats import METRICS

        return [{"key": k, "label": m.label, "unit": m.unit, "level": m.level}
                for k, m in METRICS.items()]

    def _rule_name(raw: str | None) -> str:
        name = (raw or "").strip()
        if not name or len(name) > 255:
            raise HTTPException(400, "a name of 1-255 characters is required")
        if any(ord(c) < 32 or ord(c) == 127 for c in name):  # it goes into message headers
            raise HTTPException(400, "the name cannot contain control characters or line breaks")
        return name

    @app.post("/api/alert-rules")
    def alert_rule_create(body: RuleIn, request: Request, user: User = Depends(admin_user),
                          session: Session = Depends(db)):
        from ..db.models import AlertRule
        from ..seeds.loader import _toml
        from ..stats import METRICS

        name = _rule_name(body.name)
        if body.kind not in RULE_KINDS:
            raise HTTPException(400, f"kind is {'|'.join(RULE_KINDS)}")
        if body.severity not in ("warn", "alert"):
            raise HTTPException(400, "severity is warn|alert")
        modality = None
        if body.modality and body.modality.strip():
            from ..modality_profiles import PROFILES

            # a group stands for its modalities (DX = DX and CR, as everywhere else); a code
            # no group knows would make a rule that never fires
            known = {m: [m] for p in PROFILES for m in p["modalities"]}
            known.update({p["code"]: p["modalities"] for p in PROFILES})
            parts = [m.strip().upper() for m in body.modality.split(",") if m.strip()]
            bad = [m for m in parts if m not in known]
            if not parts or bad:
                every = ", ".join(sorted({m for v in known.values() for m in v}))
                raise HTTPException(400, f"unknown modality {', '.join(bad) or body.modality!r}:"
                                         f" use {every}")
            modality = ",".join(dict.fromkeys(m for x in parts for m in known[x]))
        if body.max_age_days is not None and not 1 <= body.max_age_days <= 3650:
            raise HTTPException(400, "the age limit must be positive")
        metric = unit = None
        if body.kind in ("threshold", "cumulative"):
            m = METRICS.get(body.metric or "")
            if m is None:
                raise HTTPException(400, "choose a metric")
            if body.kind == "cumulative" and m.level != "study":
                raise HTTPException(400, "a cumulative rule needs a per-study metric")
            metric, unit = body.metric, m.unit
        elif body.metric:
            raise HTTPException(400, f"a {body.kind} rule has no metric")
        if body.kind == "pregnant":
            threshold = 0.0
        else:
            if body.threshold is None or body.threshold <= 0:
                raise HTTPException(400, "the threshold must be greater than zero")
            threshold = body.threshold
        if body.kind in ("cumulative", "count"):
            if body.window_days is None or body.window_days < 1:
                raise HTTPException(400, "a window of at least 1 day is required")
        elif body.window_days is not None:
            raise HTTPException(400, f"a {body.kind} rule has no window")
        base = (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rule")[:56]
        taken = set(session.scalars(select(AlertRule.key)))
        key, n = base, 1
        while key in taken or key in {r["key"] for r in _toml("alert_rules.toml")["rule"]}:
            n += 1
            key = f"{base}-{n}"
        r = AlertRule(key=key, name=name, enabled=True, kind=body.kind, modality=modality,
                      metric=metric, threshold=threshold, unit=unit,
                      window_days=body.window_days, max_age_days=body.max_age_days,
                      severity=body.severity, notify=body.notify,
                      note=(body.note or "").strip()[:1024] or None)
        session.add(r)
        session.flush()
        out = _rule_dict(r, set())
        _audit(session, request, user, "alert_rule.create", f"rule:{key}",
               {k: v for k, v in out.items() if k not in ("id", "custom")})
        session.commit()
        return out

    @app.delete("/api/alert-rules/{rule_id}")
    def alert_rule_delete(rule_id: int, request: Request, user: User = Depends(admin_user),
                          session: Session = Depends(db)):
        from ..db.models import Alert, AlertRule
        from ..seeds.loader import _toml

        r = session.get(AlertRule, rule_id)
        if r is None:
            raise HTTPException(404, "rule not found")
        if r.key in {x["key"] for x in _toml("alert_rules.toml")["rule"]}:
            raise HTTPException(400, "a default rule cannot be deleted: switch it off instead")
        n = session.scalar(select(func.count()).select_from(Alert).where(Alert.rule_id == r.id))
        _audit(session, request, user, "alert_rule.delete", f"rule:{r.key}",
               {"name": r.name, "alerts_removed": n})
        session.delete(r)
        session.commit()
        return {"deleted": r.name, "alerts_removed": n}

    @app.patch("/api/alert-rules/{rule_id}")
    def alert_rule_patch(rule_id: int, body: RulePatch, request: Request,
                         user: User = Depends(admin_user), session: Session = Depends(db)):
        from ..db.models import AlertRule

        r = session.get(AlertRule, rule_id)
        if r is None:
            raise HTTPException(404, "rule not found")
        patch = body.model_dump(exclude_unset=True)
        if "severity" in patch and patch["severity"] not in ("warn", "alert"):
            raise HTTPException(400, "severity is warn|alert")
        for k in ("enabled", "notify", "severity", "name"):
            if k in patch and patch[k] is None:
                raise HTTPException(400, f"{k} cannot be empty")
        if "name" in patch:
            patch["name"] = _rule_name(patch["name"])
        if patch.get("threshold") is not None and r.kind not in ("pregnant", "protocol_band") \
                and patch["threshold"] <= 0:
            raise HTTPException(400, "the threshold must be greater than zero")
        for k in ("window_days", "max_age_days"):
            if patch.get(k) is not None and not 1 <= patch[k] <= 3650:
                raise HTTPException(400, f"{k} is 1 to 3650 days")
        if "window_days" in patch and patch["window_days"] is not None \
                and r.kind not in ("cumulative", "count"):
            raise HTTPException(400, f"a {r.kind} rule has no window")
        if "window_days" in patch and patch["window_days"] is None \
                and r.kind in ("cumulative", "count"):
            raise HTTPException(400, "a window of at least 1 day is required")
        changes = {k: {"from": getattr(r, k), "to": v} for k, v in patch.items()
                   if getattr(r, k) != v}
        for k, v in patch.items():
            setattr(r, k, v)
        _audit(session, request, user, "alert_rule.update", f"rule:{r.key}", changes)
        session.commit()
        return {"id": r.id, "changes": changes}

    # ------------------------------------------------------------------ exports

    @app.get("/api/export")
    def export(request: Request, q: str = "", level: str = "study", format: str = "csv",
               pid: bool = False, user: User = Depends(current_user),
               session: Session = Depends(db)):
        import tempfile

        from ..export import lists

        roles = user.role_set()
        if not roles & {"exporter", "admin"}:
            raise HTTPException(403, "the exporter role is required")
        if pid and "pid" not in roles:
            raise HTTPException(403, "the pid role is required to export patient identifiers")
        if level not in ("study", "event") or format not in ("csv", "xlsx"):
            raise HTTPException(400, "level is study|event, format is csv|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        names, physical = data.device_names(session), data._physical_ids(session)
        _audit(session, request, user, "export", f"{level}:{format}",
               {"q": q, "pid": pid})
        session.commit()
        fname = f"dozimetrix-{level}s-{now:%Y%m%d-%H%M}.{format}"
        headers = {"Content-Disposition": f'attachment; filename="{fname}"',
                   "Cache-Control": "no-store"}

        def source(s):
            return lists.rows(s, conds, level, pid, device_names=names, physical=physical)

        if format == "xlsx":
            # closed by the streaming generator once the file has been sent
            tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115
            with maker() as s:
                lists.write_xlsx(tmp, level, pid, source(s), meta=[
                    ("Exported", now.isoformat(sep=" ", timespec="seconds")),
                    ("By", user.username), ("Query", q or "(all)"), ("Level", level),
                    ("Patient identifiers", "yes" if pid else "no (pseudonyms)")])
            tmp.seek(0)

            def chunks():
                with tmp:
                    while block := tmp.read(1 << 16):
                        yield block

            return StreamingResponse(
                chunks(), headers=headers,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        def csv_stream():
            import csv
            import io

            with maker() as s:
                buf = io.StringIO()
                w = csv.writer(buf, lineterminator="\r\n")
                buf.write("\ufeff")  # BOM: Excel opens UTF-8 CSV with diacritics intact
                w.writerow([h for h, _ in lists.columns(level, pid)])
                for i, row in enumerate(source(s)):
                    w.writerow([lists._csv_value(v) for v in row])
                    if i % 500 == 499:
                        yield buf.getvalue().encode("utf-8")
                        buf.seek(0)
                        buf.truncate()
                yield buf.getvalue().encode("utf-8")

        return StreamingResponse(csv_stream(), headers=headers, media_type="text/csv")

    def _exporter(user: User) -> None:
        if not user.role_set() & {"exporter", "admin"}:
            raise HTTPException(403, "the exporter role is required")

    def _file(write, fmt: str, stem: str, now: dt.datetime):
        """Run ``write(fh)`` into a temporary file, then stream it as a download."""
        import tempfile

        media = {"csv": "text/csv", "json": "application/json", "dcm": "application/dicom",
                 "zip": "application/zip",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        # closed by the streaming generator once the file has been sent
        tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115
        write(tmp)
        tmp.seek(0)

        def chunks():
            with tmp:
                while block := tmp.read(1 << 16):
                    yield block

        fname = f"{stem}-{now:%Y%m%d-%H%M}.{fmt}"
        return StreamingResponse(chunks(), media_type=media[fmt], headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store"})

    @app.get("/api/studies/{study_id}/rdsr")
    def study_rdsr(study_id: int, request: Request, pid: bool = False,
                   user: User = Depends(current_user), session: Session = Depends(db)):
        """The study's dose SR (IHE REM; RRDSR for NM/PET) as a download; pseudonymised unless
        pid=1 (pid role).
        A file download only: Dozimetrix does not send dose SRs anywhere."""
        from ..export import rdsr

        _exporter(user)
        if pid and "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required to export patient identifiers")
        st = session.get(Study, study_id)
        if st is None:
            raise HTTPException(404, "study not found")
        if not st.events:
            raise HTTPException(400, "the study has no dose events")
        ds = rdsr.build(st, app.state.secret, identified=pid)
        _audit(session, request, user, "export", f"rdsr:study:{study_id}", {"pid": pid})
        session.commit()
        return _file(lambda fh: rdsr.write(ds, fh), "dcm", f"rdsr-{study_id}", clock())

    @app.get("/api/drl/dataset")
    def drl_dataset(request: Request, q: str = "origin:local", format: str = "xlsx",
                    user: User = Depends(current_user), session: Session = Depends(db)):
        from ..export import drl_dataset as ds

        _exporter(user)
        if format not in ("csv", "xlsx", "json"):
            raise HTTPException(400, "format is csv|xlsx|json")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        _audit(session, request, user, "export", f"drl-dataset:{format}", {"q": q})
        session.commit()

        def write(fh):
            with maker() as s:
                ds.write(format, fh, ds.dataset_rows(s, conds, secret), ds.metadata(q, now))

        return _file(write, format, "dozimetrix-drl-dataset", now)

    @app.get("/api/drl/summary")
    def drl_summary(request: Request, q: str = "origin:local", set: str = "ro-ms838-2025",
                    min_n: int = 20, format: str = "xlsx", user: User = Depends(current_user),
                    session: Session = Depends(db)):
        from .. import drl as drl_mod
        from ..export import drl_dataset as ds
        from ..export.xlsx import XlsxWriter

        _exporter(user)
        if format not in ("csv", "xlsx", "json"):
            raise HTTPException(400, "format is csv|xlsx|json")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        try:
            rows = ds.summary_rows(session, conds, set, max(min_n, 1))
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "export", f"drl-summary:{format}",
               {"q": q, "set": set, "min_n": min_n})
        session.commit()
        meta = {**ds.metadata(q, now, len(rows)), "dataset": "dozimetrix-icrp135-summary",
                "reference_set": set, "min_n": min_n}

        def write(fh):
            if format == "json":
                fh.write(json.dumps({**meta, "rows": rows}, ensure_ascii=False,
                                    indent=1).encode("utf-8"))
            elif format == "csv":
                import csv
                import io

                text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                w = csv.DictWriter(text, ds.SUMMARY_FIELDS, lineterminator="\r\n")
                w.writeheader()
                from ..export.lists import safe_text

                w.writerows({k: ("" if v is None else safe_text(v)) for k, v in r.items()}
                            for r in rows)
                text.flush()
                text.detach()
            else:
                with XlsxWriter(fh) as wb:
                    ws = wb.sheet("ICRP 135 summary", ds.SUMMARY_FIELDS,
                                  [9, 18, 18, 30, 10, 9, 10] + [9] * 13)
                    for r in rows:
                        ws.row([r[f] for f in ds.SUMMARY_FIELDS])
                    about = wb.sheet("About", ["Item", "Value"], [16, 90])
                    for k, v in meta.items():
                        about.row([k, v])

        return _file(write, format, "dozimetrix-icrp135-summary", now)

    @app.get("/api/survey/export")
    def survey_export(request: Request, month: str, q: str = "origin:local",
                      format: str = "csv", user: User = Depends(current_user),
                      session: Session = Depends(db)):
        from ..export import drl_dataset as ds
        from ..export import survey

        _exporter(user)
        if format not in ("csv", "xlsx", "json"):
            raise HTTPException(400, "format is csv|xlsx|json")
        try:
            start, end = survey.month_range(month)
        except ValueError as e:
            raise HTTPException(400, "month is YYYY-MM") from e
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        _audit(session, request, user, "export", f"survey:{month}:{format}", {"q": q})
        session.commit()
        meta = {**ds.metadata(q, now), "dataset": "dozimetrix-paediatric-survey",
                "version": survey.SURVEY_VERSION, "month": month,
                "plan": "MS-CNCAN-SDNP-2026-001 draft v1.0 (March 2026)"}

        def write(fh):
            with maker() as s:
                ds.write(format, fh, survey.rows(s, conds, secret), meta, survey.DICTIONARY)

        return _file(write, format, f"dozimetrix-survey-{month}", now)

    # ------------------------------------------------------------------ central survey

    def _central(user: User) -> None:
        if not user.role_set() & {"admin", "importer"}:
            raise HTTPException(403, "the importer or admin role is required")

    def _agg(fn):
        from .. import aggregate

        try:
            return fn(aggregate)
        except aggregate.SurveyError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/survey/uploads")
    def survey_upload(request: Request, filename: str | None = None,
                      body: bytes = Body(..., media_type="text/csv"),
                      user: User = Depends(current_user), session: Session = Depends(db)):
        """A site's survey export (CSV or JSON) into the central collection."""
        _central(user)
        try:
            up = _agg(lambda a: a.upload(session, body, filename=filename, user=user.username))
        except HTTPException:
            session.rollback()
            raise
        _audit(session, request, user, "survey.upload", f"{up.institution}:{up.period}",
               {"rows": up.n_rows, "rejected": up.n_rejected, "file": filename})
        session.commit()
        return {"id": up.id, "institution": up.institution, "period": up.period,
                "rows": up.n_rows, "rejected": up.n_rejected, "report": up.report}

    @app.get("/api/survey/uploads")
    def survey_uploads(user: User = Depends(current_user), session: Session = Depends(db)):
        _central(user)
        return _agg(lambda a: a.uploads(session))

    @app.delete("/api/survey/uploads/{upload_id}")
    def survey_upload_delete(upload_id: int, request: Request,
                             user: User = Depends(admin_user), session: Session = Depends(db)):
        out = _agg(lambda a: a.remove(session, upload_id))
        _audit(session, request, user, "survey.delete", f"upload:{upload_id}", out)
        session.commit()
        return out

    @app.put("/api/survey/institutions/{code}")
    def survey_institution(code: str, body: InstitutionNameIn, request: Request,
                           user: User = Depends(admin_user), session: Session = Depends(db)):
        _agg(lambda a: a.name_institution(session, code, body.name))
        _audit(session, request, user, "survey.institution", code, {"name": body.name})
        session.commit()
        return {"code": code, "name": body.name}

    @app.get("/api/survey/national")
    def survey_national(request: Request, start: str, end: str, grid: str = "rp185",
                        estimated: bool = False, format: str = "json",
                        user: User = Depends(current_user), session: Session = Depends(db)):
        """National statistics over the uploaded exports (P75 of institution medians)."""
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        rep = _agg(lambda a: a.national(session, start, end, grid=grid,
                                        estimated_weights=estimated))
        if format == "json":
            return rep
        _exporter(user)
        _audit(session, request, user, "report", f"survey.national:{start}..{end}",
               {"grid": grid})
        session.commit()
        from .. import aggregate

        info = {"Generat": clock().isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username}
        return _file(lambda fh: aggregate.write_xlsx(rep, fh, info), "xlsx",
                     f"studiu-national-{start}-{end}", clock())

    @app.get("/api/reports/drl-trend")
    def report_drl_trend(request: Request, year: int | None = None, quarter: int | None = None,
                         quarters: int = 6, set: str = "ro-ms838-2025",
                         q: str = "origin:local", format: str = "json",
                         user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod
        from ..reports import trend

        _exporter(user)
        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 2 <= quarters <= 12:
            raise HTTPException(400, "quarter is 1..4, quarters is 2..12")
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        try:
            report = trend.build(session, conds, year, quarter, quarters, set)
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "report", f"drl-trend:{year}Q{quarter}:{format}",
               {"q": q, "set": set, "quarters": quarters})
        session.commit()
        if format == "json":
            return report
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: trend.write_xlsx(report, fh, info), "xlsx",
                     f"drl-trend-{year}-T{quarter}", now)

    @app.get("/api/reports/operators")
    def report_operators(request: Request, year: int | None = None, quarter: int | None = None,
                         quarters: int = 4, q: str = "origin:local", min_n: int = 20,
                         format: str = "json", user: User = Depends(admin_user),
                         session: Session = Depends(db)):
        """Doses per operator against the department median (exam, age group). Staff names:
        admin only, audited."""
        from ..reports import operators, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8")
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = operators.build(session, conds, start.date(), (end - dt.timedelta(days=1)).date(),
                              max(min_n, 1))
        _audit(session, request, user, "report", f"operators:{year}Q{quarter}:{format}",
               {"q": q, "quarters": quarters})
        session.commit()
        if format == "json":
            return rep
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: operators.write_xlsx(rep, fh, info), "xlsx",
                     f"operatori-{year}-T{quarter}", now)

    @app.get("/api/reports/dose-check")
    def report_dose_check(request: Request, year: int | None = None, quarter: int | None = None,
                          quarters: int = 1, q: str = "origin:local", format: str = "json",
                          user: User = Depends(current_user), session: Session = Depends(db)):
        """CT Dose Check (NEMA XR-25): notifications and alerts per protocol and room, with the
        reasons operators gave for proceeding."""
        from ..reports import dosecheck, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8")
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = {"start": start.date().isoformat(),
               "end": (end - dt.timedelta(days=1)).date().isoformat(),
               **dosecheck.build(session, conds)}
        if format == "json":
            return rep
        return _file(lambda fh: dosecheck.write_csv(rep, fh), "csv",
                     f"dose-check-{year}-T{quarter}", now)

    @app.get("/api/reports/exposure-index")
    def report_exposure_index(request: Request, year: int | None = None,
                              quarter: int | None = None, quarters: int = 1,
                              q: str = "origin:local", format: str = "json",
                              user: User = Depends(current_user),
                              session: Session = Depends(db)):
        """Deviation index (IEC 62494-1) per room and protocol, with the target EI."""
        from ..reports import exposure, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8")
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = {"start": start.date().isoformat(),
               "end": (end - dt.timedelta(days=1)).date().isoformat(),
               **exposure.build(session, conds)}
        if format == "json":
            return rep
        return _file(lambda fh: exposure.write_csv(rep, fh), "csv",
                     f"exposure-index-{year}-T{quarter}", now)

    @app.get("/api/reports/collimation")
    def report_collimation(request: Request, year: int | None = None,
                           quarter: int | None = None, quarters: int = 1,
                           q: str = "origin:local", format: str = "json",
                           user: User = Depends(current_user), session: Session = Depends(db)):
        """Radiography field area (DAP / kerma) per room against the department: tells
        over-collimation of particular exams from a DAP calibration error."""
        from ..reports import collimation, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8")
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = {"start": start.date().isoformat(),
               "end": (end - dt.timedelta(days=1)).date().isoformat(),
               **collimation.build(session, conds)}
        if format == "json":
            return rep
        return _file(lambda fh: collimation.write_csv(rep, fh), "csv",
                     f"collimation-{year}-T{quarter}", now)

    @app.get("/api/reports/scan-length")
    def report_scan_length(request: Request, year: int | None = None,
                           quarter: int | None = None, quarters: int = 1,
                           q: str = "origin:local", ratio: float = 1.3, format: str = "json",
                           user: User = Depends(current_user), session: Session = Depends(db)):
        """CT scan length outliers per exam and age group (scans running past their region)."""
        from ..reports import scanlength, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8 or not 1.0 < ratio <= 5.0:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8, ratio 1..5")
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = {"start": start.date().isoformat(),
               "end": (end - dt.timedelta(days=1)).date().isoformat(),
               **scanlength.build(session, conds, ratio=ratio)}
        if format == "json":
            return rep
        return _file(lambda fh: scanlength.write_csv(rep, fh), "csv",
                     f"scan-length-{year}-T{quarter}", now)

    @app.get("/api/reports/optimisation")
    def report_optimisation(request: Request, change: dt.date, days: int = 180,
                            set: str = "ro-ms838-2025", q: str = "origin:local",
                            exam: str | None = None, min_n: int = 10, note: str | None = None,
                            format: str = "json", user: User = Depends(current_user),
                            session: Session = Depends(db)):
        """Doses before and after a protocol change, per exam and group, with a significance
        test, and the reviewer's image-quality note."""
        from .. import drl
        from ..reports import optimisation

        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        now = clock()
        try:
            rep = optimisation.build(session, data.checked_conditions(q, now,
                                                                      data.overrides(session)),
                                     change, days=days, set_key=set, min_n=max(min_n, 2),
                                     exam=exam, note=note)
        except drl.DRLError as e:
            raise HTTPException(400, str(e)) from None
        if format == "json":
            return rep
        return _file(lambda fh: optimisation.write_csv(rep, fh), "csv",
                     f"optimisation-{change:%Y%m%d}", now)

    @app.get("/api/reports/repeats")
    def report_repeats(request: Request, year: int | None = None, quarter: int | None = None,
                       quarters: int = 1, q: str = "origin:local", gap: float = 10.0,
                       operators: bool = False, format: str = "json",
                       user: User = Depends(current_user), session: Session = Depends(db)):
        """Possible repeats in radiography (a proxy for the reject rate) per room and exam.
        Per operator: administrators only (staff names), audited."""
        from ..reports import repeats, trend
        from ..reports.insp1255 import quarter_range

        if operators:
            admin_only(user)
        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8 or not 1 <= gap <= 120:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8, gap is 1..120 minutes")
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = {"start": start.date().isoformat(),
               "end": (end - dt.timedelta(days=1)).date().isoformat(),
               **repeats.build(session, conds, gap_min=gap, by_operator=operators)}
        if operators:
            _audit(session, request, user, "report", f"repeats:{year}Q{quarter}:operators",
                   {"q": q})
            session.commit()
        if format == "json":
            return rep
        return _file(lambda fh: repeats.write_csv(rep, fh), "csv",
                     f"repeats-{year}-T{quarter}", now)

    @app.get("/api/reports/effective-dose")
    def report_effective_dose(request: Request, year: int | None = None,
                              quarter: int | None = None, quarters: int = 1,
                              q: str = "origin:local", format: str = "json",
                              user: User = Depends(current_user),
                              session: Session = Depends(db)):
        """CT effective dose by the DLP method (needs the coefficients entered): per exam and
        age group, and the collective dose per room. A population-level estimate."""
        from ..reports import effective, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8")
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = {"start": start.date().isoformat(),
               "end": (end - dt.timedelta(days=1)).date().isoformat(),
               **effective.build(session, conds)}
        if format == "json":
            return rep
        return _file(lambda fh: effective.write_csv(rep, fh), "csv",
                     f"effective-dose-{year}-T{quarter}", now)

    @app.get("/api/reports/centering")
    def report_centering(request: Request, year: int | None = None, quarter: int | None = None,
                         quarters: int = 1, q: str = "origin:local", format: str = "json",
                         user: User = Depends(current_user), session: Session = Depends(db)):
        """CT patient centering per room (needs [ingest] measure_centering; unverified)."""
        from ..reports import centering, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8")
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json|csv")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = {"start": start.date().isoformat(),
               "end": (end - dt.timedelta(days=1)).date().isoformat(),
               **centering.build(session, conds)}
        if format == "json":
            return rep
        return _file(lambda fh: centering.write_csv(rep, fh), "csv",
                     f"centering-{year}-T{quarter}", now)

    @app.get("/api/reports/insp1255")
    def report_insp1255(request: Request, year: int, quarter: int, q: str = "origin:local",
                        format: str = "json", user: User = Depends(current_user),
                        session: Session = Depends(db)):
        from ..reports import insp1255

        _exporter(user)
        try:
            start, end = insp1255.quarter_range(year, quarter)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        report = insp1255.build(session, conds, start, end)
        _audit(session, request, user, "report", f"insp1255:{year}Q{quarter}:{format}",
               {"q": q})
        session.commit()
        if format == "json":
            return insp1255.as_json(report)
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: insp1255.write_xlsx(report, fh, info), "xlsx",
                     f"insp-1255-{year}-T{quarter}", now)

    from . import exports as _exports

    _exports.register(app, db=db, current_user=current_user, clock=clock, file_response=_file,
                      audit=_audit, settings=settings, secret=secret)

    @app.get("/api/reports/art12")
    def report_art12(request: Request, year: int, q: str = "origin:local", format: str = "json",
                     user: User = Depends(current_user), session: Session = Depends(db)):
        """Annual DRL return (art. 12): the summary as JSON, or the zip of workbooks. The
        workbooks carry the first 7 CNP digits and the indication, so they need ``pid``."""
        from ..reports import art12

        _exporter(user)
        if format not in ("json", "zip"):
            raise HTTPException(400, "format is json|zip")
        if format == "zip" and "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required: the workbooks hold patient "
                                     "identifiers")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        books = art12.build(session, year, conds)
        _audit(session, request, user, "report", f"art12:{year}:{format}",
               {"q": q, "pid": format == "zip"})
        session.commit()
        if format == "json":
            return {"year": year, "per_sheet": art12.PATIENTS_PER_SHEET,
                    "window": [art12.window(year)[0].date().isoformat(),
                               (art12.window(year)[1] - dt.timedelta(days=1)).date().isoformat()],
                    "workbooks": art12.summary(books)}
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: art12.write_zip(books, fh, info), "zip", f"art12-{year}", now)

    @app.get("/api/reports/grid")
    def report_grid(request: Request, year: int, quarter: int, q: str = "origin:local",
                    format: str = "json", min_n: int = 10, estimated: bool = False,
                    user: User = Depends(current_user), session: Session = Depends(db)):
        """The second grid: ICRP 135 / RP 185 groups (head by age, body by weight)."""
        from ..reports import grid
        from ..reports.insp1255 import quarter_range

        _exporter(user)
        try:
            start, end = quarter_range(year, quarter)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        report = grid.build(session, conds, start, end, max(min_n, 1), estimated)
        _audit(session, request, user, "report", f"grid:{year}Q{quarter}:{format}",
               {"q": q, "estimated": estimated})
        session.commit()
        if format == "json":
            return report
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: grid.write_xlsx(report, fh, info), "xlsx",
                     f"grila-icrp135-{year}-T{quarter}", now)

    @app.get("/api/lookup")
    def lookup(q: str = "", limit: int = 6, user: User = Depends(current_user),
               session: Session = Depends(db)):
        return data.lookup(session, q, min(max(limit, 1), 20))

    # ------------------------------------------------------------------ pages

    @app.get("/login", response_class=HTMLResponse)
    def login_page(session: Session = Depends(db)):
        if first_run.needs_admin(session):
            return RedirectResponse("/setup", status_code=303)
        return FileResponse(LOGIN_HTML)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, session: Session = Depends(db)):
        if not first_run.needs_admin(session):
            user = optional_user(request, session)
            if user is None:
                return RedirectResponse("/login", status_code=303)
            if "admin" not in user.role_set():
                raise HTTPException(403, "the admin role is required")
        return FileResponse(SETUP_HTML, headers={"Cache-Control": "no-store"})

    page_cache: dict[str, tuple[int, str]] = {}  # file -> (modified time, text)

    def page_html(name: str) -> str:
        """A page's HTML, read again only when its file changed."""
        path = WEB / name
        mtime = path.stat().st_mtime_ns
        hit = page_cache.get(name)
        if hit is None or hit[0] != mtime:
            hit = page_cache[name] = (mtime, path.read_text(encoding="utf-8"))
        return hit[1]

    @app.get("/{page:path}", response_class=HTMLResponse)
    def pages(page: str, request: Request, session: Session = Depends(db)):
        if page in OLD_PAGES:  # renamed pages: old bookmarks keep working
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/{OLD_PAGES[page]}{query}", status_code=301)
        if page.startswith(("assets/", "api/")) or page not in PAGES:
            raise HTTPException(404)
        if session_user(request, session) is None:
            return RedirectResponse("/setup" if first_run.needs_admin(session) else "/login",
                                    status_code=303)
        html = page_html(PAGES[page])
        origin = _html.escape(str(request.base_url).rstrip("/"), quote=True)
        html = re.sub(r"<html(\s|>)", lambda m: f'<html data-api="{origin}"{m.group(1)}', html,
                      count=1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/assets", RevalidatedStatic(directory=WEB / "assets"), name="assets")

    @app.exception_handler(data.QueryError)
    def bad_query(request: Request, exc: data.QueryError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    # route order: pages catch-all must come after the API; FastAPI matches in order of
    # registration, and the static mount is checked before routes for /assets.
    app.router.routes.sort(key=lambda r: getattr(r, "path", "") == "/{page:path}")
    return app


def _me(user: User) -> dict:
    return {"username": user.username, "name": user.display_name,
            "roles": sorted(user.role_set())}


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # verified against for unknown users


# audited, but they change no data: the cached answers stay valid
_READ_ONLY_ACTIONS = {"login", "login_failed", "identify", "export", "report"}


def _audit(session: Session, request: Request, user: User | None, action: str,
           target: str | None, detail: dict | None = None) -> None:
    cache = getattr(getattr(request, "app", None), "state", None)
    cache = getattr(cache, "result_cache", None)
    if cache is not None and action not in _READ_ONLY_ACTIONS:
        cache.clear()  # something changed through the UI: the heavy answers are recomputed
    session.add(AuditLog(user_id=user.id if user else None,
                         username=user.username if user else None, action=action,
                         target=target[:250] if isinstance(target, str) else target, detail=detail,
                         remote_addr=request.client.host if request.client else None))
