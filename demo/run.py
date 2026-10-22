"""One command to try the platform with four demo companies on dummy data.

    python demo/run.py            # generate data, prepare the database, start the API and worker
    python demo/run.py --reset    # start from an empty database

Everything lives in demo/.runtime (ignored by git). Set ANTHROPIC_API_KEY first to see the AI
employees investigate with Claude; without it they still report verified facts."""
import argparse
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "demo" / ".runtime"
PASSWORD = "demo-workforce-2026"
COMPANIES = ["homemonde", "kavya", "atlas", "bloom"]
# (email, name, role, areas, approval limit) per company
USERS = {
    "homemonde": [("founder@homemonde.demo", "HomeMonde Founder", "owner", [], None),
                  ("procurement@homemonde.demo", "Procurement Manager", "manager", ["procurement"], 100000),
                  ("hr@homemonde.demo", "HR Manager", "manager", ["people"], None)],
    "kavya": [("owner@kavya.demo", "Kavya Owner", "owner", [], None),
              ("sales@kavya.demo", "Sales Head", "manager", ["distribution", "cold_store"], None)],
    "atlas": [("ceo@atlas.demo", "Atlas CEO", "owner", [], None),
              ("purchasing@atlas.demo", "Purchasing Lead", "manager", ["purchasing"], None)],
    "bloom": [("founder@bloom.demo", "Bloom Founder", "owner", [], None),
              ("care@bloom.demo", "Customer Care Lead", "manager", ["care_centre", "storefront"], None)],
}


def prepare(reset: bool, database_url: str | None = None, sync_minutes: int | None = None) -> dict:
    """Generate the data, copy the packs and create the accounts. sync_minutes shortens every feed's schedule
    (the soak test uses it to push many sync cycles through in a short time)."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "demo"))
    from generate import generate

    if reset and RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    tenants = RUNTIME / "tenants"
    if tenants.exists():
        shutil.rmtree(tenants)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    secret = RUNTIME / "secret.key"  # lets owners save AI keys from the panel; kept with the demo database
    if not secret.exists():
        secret.write_text(secrets.token_urlsafe(32))
        secret.chmod(0o600)
    for slug in COMPANIES:  # the real HomeMonde pack plus the demo packs
        src = ROOT / ("tenants" if slug == "homemonde" else "demo/tenants") / slug
        shutil.copytree(src, tenants / slug)
        if sync_minutes:
            pack = tenants / slug / "tenant.yaml"
            pack.write_text(re.sub(r"schedule_minutes: \d+", f"schedule_minutes: {sync_minutes}", pack.read_text()))
    env = {
        "WORKFORCE_DATABASE_URL": database_url or f"sqlite:///{RUNTIME / 'workforce-demo.db'}",
        "WORKFORCE_TENANTS_DIR": str(tenants),
        "WORKFORCE_TENANTS": ",".join(COMPANIES),
        "WORKFORCE_SECURE_COOKIES": "false",
        "WORKFORCE_ENVIRONMENT": "development",
        "WORKFORCE_SECRET_KEY": os.environ.get("WORKFORCE_SECRET_KEY") or secret.read_text().strip(),
        **generate(RUNTIME / "data"),
    }
    os.environ.update(env)

    from workforce import db, security, tenancy
    from workforce.cli import cmd_migrate
    from workforce.models import User
    cmd_migrate(None)
    with db.session() as s:
        tenancy.register_all(s)
        for slug, users in USERS.items():
            for email, name, role, areas, limit in users:
                if not s.query(User).filter(User.email == email).first():
                    s.add(User(tenant_id=slug, email=email, name=name, role=role, areas=areas, approval_limit=limit,
                               password_hash=security.hash_password(PASSWORD)))
    return env


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete the demo database and start fresh")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8100), help="default: $PORT (set by hosts such as Render) or 8100")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container, so the published port reaches it")
    ap.add_argument("--database-url", help="run on this database instead of a local SQLite file, e.g. Postgres for a production-like demo")
    a = ap.parse_args()
    env = {**os.environ, **prepare(a.reset, a.database_url), "PYTHONPATH": str(ROOT)}
    print(f"\nAI Workforce demo · http://127.0.0.1:{a.port}   (password for every account: {PASSWORD})\n", flush=True)
    for slug, users in USERS.items():
        for email, _, role, areas, _ in users:
            print(f"  {slug:<10} {role:<8} {email:<30} {('areas: ' + ', '.join(areas)) if areas else 'sees everything'}")
    if not any(env.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "HF_TOKEN")):
        print("\n  No AI key is set: employees report verified facts only. Sign in as an owner and open")
        print("  Operations › AI models & keys to choose a model and paste a key.")
    print("\nPress Ctrl+C to stop.\n")
    signal.signal(signal.SIGTERM, _stop)  # stopping the launcher stops the server and worker too
    threading.Thread(target=_keep_exports_fresh, daemon=True).start()
    procs = [subprocess.Popen([sys.executable, "-m", "workforce.cli", "serve", "--host", a.host, "--port", str(a.port)], env=env, cwd=ROOT),
             subprocess.Popen([sys.executable, "-m", "workforce.cli", "worker"], env=env, cwd=ROOT)]
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            p.wait(timeout=30)


def _keep_exports_fresh(every_seconds: int = 600):
    """The demo's dummy exports are written once. A real export is rewritten on its schedule, and a file that stops
    changing is refused as out of date, so the demo marks its files as freshly written to behave like live feeds."""
    while True:
        time.sleep(every_seconds)
        for f in (RUNTIME / "data").rglob("*.csv"):
            try:
                os.utime(f)
            except OSError:
                pass


def _stop(*_):
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
