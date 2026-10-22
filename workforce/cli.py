"""Operator CLI: validate packs, migrate, manage users, sync on demand, run the API and the worker."""
import argparse
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import select


def _alembic_cfg():
    from alembic.config import Config
    # Repository checkout or container workdir (/app) both keep alembic.ini next to migrations/.
    root = Path.cwd() if (Path.cwd() / "alembic.ini").exists() else Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


def cmd_check(a):
    from . import tenancy
    slugs = [a.tenant] if a.tenant else tenancy.available()
    if not slugs:
        sys.exit("no tenant packs found")
    failed = False
    for slug in slugs:
        try:
            cfg, _ = tenancy.load_file(tenancy.pack_path(slug))
            print(f"✓ {slug}: {len(cfg.employees)} employees, {len(cfg.datasets)} datasets, {len(cfg.metrics)} metrics, {len(cfg.rules)} rules")
        except Exception as e:  # report every pack, then fail
            failed = True
            print(f"✗ {slug}: {e}")
    sys.exit(1 if failed else 0)


def cmd_migrate(_a):
    from alembic import command
    command.upgrade(_alembic_cfg(), "head")
    print("database is up to date")


def cmd_user_add(a):
    from . import db, security, tenancy
    from .models import User
    password = os.environ.get("WORKFORCE_NEW_PASSWORD") or getpass.getpass("Password (min 12 chars): ")
    with db.session() as s:
        cfgs = tenancy.register_all(s)
        if a.tenant not in cfgs:
            sys.exit(f"unknown tenant {a.tenant}")
        areas = [x.strip() for x in (a.areas or "").split(",") if x.strip()]
        bad = set(areas) - set(cfgs[a.tenant].layout.areas)
        if bad:
            sys.exit(f"unknown areas {sorted(bad)}")
        if a.role == "manager" and not areas:
            sys.exit("managers need --areas (their business scope)")
        if s.execute(select(User).where(User.email == a.email.lower())).scalar_one_or_none():
            sys.exit("a user with that email already exists")
        s.add(User(tenant_id=a.tenant, email=a.email.lower(), name=a.name, role=a.role, areas=areas,
                   approval_limit=a.approval_limit, password_hash=security.hash_password(password)))
    print(f"added {a.role} {a.email} to {a.tenant}")


def cmd_user_disable(a):
    from . import db
    from .models import User, UserSession
    with db.session() as s:
        u = s.execute(select(User).where(User.email == a.email.lower())).scalar_one_or_none()
        if not u:
            sys.exit("no such user")
        u.active = False
        s.query(UserSession).filter(UserSession.user_id == u.id).delete()
    print(f"disabled {a.email} and ended their sessions")


def cmd_sync(a):
    from . import data, db, rules, tenancy
    with db.session() as s:
        cfg = tenancy.register_all(s)[a.tenant]
    for sid in ([a.source] if a.source else list(cfg.sources)):
        with db.session() as s:
            print(sid, data.sync_source(s, cfg, sid))
    with db.session() as s:
        print("new work items:", rules.evaluate(s, cfg))


def cmd_serve(a):
    from .api import main as serve
    serve(a.host, a.port)


def cmd_worker(_a):
    from .worker import main as run
    run()


def cmd_worker_health(_a):
    """Container health check for the worker: fails when no worker has looped recently."""
    from . import db, health
    with db.session() as s:
        st = health.worker_status(s)
    print(f"worker {st['status']}" + (f" · last beat {st['seconds_since_beat']}s ago" if st["seconds_since_beat"] is not None else ""))
    sys.exit(0 if st["status"] == "ok" else 1)


def cmd_report(a):
    from . import db, reports, tenancy
    with db.session() as s:
        cfg = tenancy.register_all(s)[a.tenant]
        r = reports.generate(s, cfg, a.kind)
        print(f"{r.id}: {r.content['overview']}")


def cmd_eval(a):
    from . import db, quality, tenancy
    from .tenancy import ModelRoute
    with db.session() as s:
        tenancy.register_all(s)
    strong = ModelRoute.model_validate(a.strong) if a.strong else None
    cheap = ModelRoute.model_validate(a.cheap) if a.cheap else None
    run_id, results = quality.run_suite(a.tenant, strong, cheap, a.scenario)
    if not results:
        sys.exit(f"no scenarios in {quality.suite_path(a.tenant)}")
    for r in results:
        print(f"{r.status:<8} {r.scenario:<36} {r.employee:<16} tokens {r.input_tokens + r.output_tokens:>7}  "
              f"cost {'$%.4f' % r.cost_usd if r.cost_usd is not None else 'n/a'}")
        for c in r.checks:
            if c["result"] != "pass":
                print(f"         {c['result']:<7} {c['check']} {c['detail']}")
    print(f"run {run_id}: {sum(r.status == 'PASSED' for r in results)} passed, {sum(r.status == 'FAILED' for r in results)} failed, "
          f"{sum(r.status == 'SKIPPED' for r in results)} skipped (AI checks skipped when no model credentials are set)")
    sys.exit(1 if any(r.status == "FAILED" for r in results) else 0)


def cmd_eval_draft(a):
    from . import db, quality, tenancy
    with db.session() as s:
        cfg = tenancy.register_all(s)[a.tenant]
        print(quality.draft_scenario(s, cfg, a.work), end="")


def cmd_verify(a):
    from . import db, events
    with db.session() as s:
        ok, bad = events.verify_chain(s, a.tenant)
    print("audit chain intact" if ok else f"audit chain broken at event {bad}")
    sys.exit(0 if ok else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="workforce")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate tenant packs")
    c.add_argument("--tenant")
    c.set_defaults(fn=cmd_check)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    sv = sub.add_parser("serve", help="run the API and web app")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)
    sub.add_parser("worker", help="run the background worker").set_defaults(fn=cmd_worker)
    sub.add_parser("worker-health", help="exit non-zero if no worker has looped recently").set_defaults(fn=cmd_worker_health)
    u = sub.add_parser("user-add", help="add an owner or manager")
    u.add_argument("--tenant", required=True)
    u.add_argument("--email", required=True)
    u.add_argument("--name", required=True)
    u.add_argument("--role", choices=["owner", "manager"], required=True)
    u.add_argument("--areas", help="comma-separated area ids (managers)")
    u.add_argument("--approval-limit", type=float, help="maximum amount this manager may approve")
    u.set_defaults(fn=cmd_user_add)
    d = sub.add_parser("user-disable", help="disable a user and end their sessions")
    d.add_argument("--email", required=True)
    d.set_defaults(fn=cmd_user_disable)
    sy = sub.add_parser("sync", help="sync sources now and run rules")
    sy.add_argument("--tenant", required=True)
    sy.add_argument("--source")
    sy.set_defaults(fn=cmd_sync)
    rp = sub.add_parser("report", help="generate a daily, weekly or monthly brief now")
    rp.add_argument("--tenant", required=True)
    rp.add_argument("--kind", choices=["daily", "weekly", "monthly"], default="daily")
    rp.set_defaults(fn=cmd_report)
    ev = sub.add_parser("eval", help="run the AI-quality scenario suite (makes real model calls when credentials are set)")
    ev.add_argument("--tenant", required=True)
    ev.add_argument("--strong", help="override the strong model as provider:model, e.g. anthropic:claude-sonnet-5-5")
    ev.add_argument("--cheap", help="override the cheap model as provider:model")
    ev.add_argument("--scenario", action="append", help="run only this scenario id (repeatable)")
    ev.set_defaults(fn=cmd_eval)
    ed = sub.add_parser("eval-draft", help="print a quality-test draft built from a finding's source records")
    ed.add_argument("--tenant", required=True)
    ed.add_argument("--work", required=True, help="work item id, e.g. INV-2026-00012")
    ed.set_defaults(fn=cmd_eval_draft)
    v = sub.add_parser("verify-audit", help="verify the tamper-evident audit chain")
    v.add_argument("--tenant", required=True)
    v.set_defaults(fn=cmd_verify)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
