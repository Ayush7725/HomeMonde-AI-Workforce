"""Soak test: run the real API and workers for a long time on the demo companies, use the app the way managers do,
and check that nothing degrades. Run it before a demo or a release, ideally on Postgres as in production.

    python demo/soak.py --database-url postgresql+psycopg://user:pw@127.0.0.1:5432/soak --hours 24
    python demo/soak.py --database-url ... --minutes 30 --chaos --pg-container my-postgres

Feeds sync every minute (instead of every 30-240) so many sync, rule and housekeeping cycles run in a short
time. Every minute it records memory, database size, response times and health to demo/.runtime/soak/metrics.jsonl.
--chaos kills a worker mid-run (and restarts the database when --pg-container names it) to prove recovery.
It ends with a verdict and exits non-zero if any check failed. The database named is emptied first."""
import argparse
import http.cookiejar
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))
from run import COMPANIES, PASSWORD, RUNTIME, USERS, prepare  # noqa: E402

OUT = RUNTIME / "soak"
ERROR_LINE = re.compile(r"\bERROR\b|\bCRITICAL\b|Traceback")
EXPECTED_LINE = re.compile(r"timeout graceful shutdown exceeded")  # uvicorn closing open live streams when the soak stops


class Browser:
    """One signed-in person: a cookie jar, the CSRF token and response timings."""

    def __init__(self, base: str, email: str):
        self.base, self.email, self.csrf, self.timings, self.errors = base, email, "", [], []
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path: str, body: dict | None = None, ok=(200,)):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        start = time.monotonic()
        try:
            with self.opener.open(req, timeout=30) as r:
                status, data = r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            status, data = e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            status, data = 0, {"error": str(e)}
        self.timings.append(time.monotonic() - start)
        if status not in ok:
            self.errors.append(f"{time.strftime('%H:%M:%S')} {path} -> {status} {data.get('error', '')}".strip())
        return status, data

    def login(self):
        status, data = self.call("/api/auth/login", {"email": self.email, "password": PASSWORD})
        self.csrf = data.get("csrf", "")
        return status == 200


def act(b: Browser, rng: random.Random):
    """A few minutes of a manager's day: look around, open tasks, decide, ask, explain."""
    _, st = b.call("/api/state")
    b.call("/api/home")
    b.call("/api/work?view=queue")
    b.call("/api/notifications")
    b.call(f"/api/events?after={max(0, st.get('seq', 0) - 50)}&limit=50")
    recent = (st.get("recent") or []) + (st.get("active") or [])
    if recent:
        w = rng.choice(recent)
        _, d = b.call(f"/api/work/{w['id']}")
        if (d.get("item") or {}).get("finding", {}) and d["item"]["finding"].get("explain_available") and rng.random() < 0.3:
            b.call(f"/api/work/{w['id']}/explain", {}, ok=(200, 409))
    for ap in st.get("approvals") or []:
        if rng.random() < 0.5:
            choice = rng.choice(["APPROVED", "REJECTED", "MORE_INVESTIGATION"])
            b.call(f"/api/approvals/{ap['id']}/decision", {"decision": choice, "reason": "soak test"}, ok=(200, 403, 409))
    if rng.random() < 0.2:
        b.call("/api/questions", {"text": rng.choice(["How did sales go yesterday?", "Which products are lowest on stock?",
                                                      "Are any deliveries late this week?"])}, ok=(200, 429))


def stream(base: str, b: Browser, stop: threading.Event, counter: list):
    """Keep a live activity stream open the whole time, as the web app does, reconnecting if it drops."""
    while not stop.is_set():
        try:
            with b.opener.open(urllib.request.Request(base + "/api/events/stream"), timeout=60) as r:
                for line in r:
                    if stop.is_set():
                        return
                    if line.startswith(b"data:"):
                        counter[0] += 1
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(2)


def rss_mb(pid: int) -> float | None:
    try:
        return round(int(re.search(r"VmRSS:\s+(\d+)", Path(f"/proc/{pid}/status").read_text()).group(1)) / 1024, 1)
    except (OSError, AttributeError):
        return None


def sample(env: dict) -> dict:
    from sqlalchemy import func, select, text

    from workforce import db, events, health
    from workforce.models import Event, LlmUsage, Notification, Record, WorkItem
    with db.session() as s:
        out = {"health": health.worker_status(s)["status"]}
        if db.is_postgres():
            out["db_mb"] = round(s.execute(text("SELECT pg_database_size(current_database())")).scalar() / 1e6, 1)
        for name, model in (("events", Event), ("records", Record), ("work", WorkItem), ("notifications", Notification), ("llm_calls", LlmUsage)):
            out[name] = s.execute(select(func.count()).select_from(model)).scalar()
        out["stuck"] = s.execute(select(func.count()).select_from(WorkItem).where(
            WorkItem.status.in_(["RUNNING", "INVESTIGATING", "VALIDATING"]), WorkItem.lease_until < db.utcnow())).scalar()
        out["pending_notifications"] = s.execute(select(func.count()).select_from(Notification).where(Notification.delivered == "pending")).scalar()
        out["audit_ok"] = all(events.verify_chain(s, t)[0] for t in COMPANIES)
    return out


def log_errors(paths) -> list[str]:
    lines = []
    for p in paths:
        if p.exists():
            lines += [f"{p.name}: {ln}" for ln in p.read_text(errors="replace").splitlines() if ERROR_LINE.search(ln) and not EXPECTED_LINE.search(ln)]
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", required=True, help="the database to soak (it is emptied first)")
    span = ap.add_mutually_exclusive_group()
    span.add_argument("--hours", type=float)
    span.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--port", type=int, default=8150)
    ap.add_argument("--chaos", action="store_true", help="kill a worker mid-run, and restart the database if --pg-container is given")
    ap.add_argument("--pg-container", help="podman/docker container running the Postgres being soaked")
    a = ap.parse_args()
    seconds = (a.hours * 3600) if a.hours else a.minutes * 60

    os.environ["WORKFORCE_DATABASE_URL"] = a.database_url
    from workforce import db
    from workforce.db import Base
    from workforce import models  # noqa: F401
    from sqlalchemy import text
    db.configure(a.database_url)
    Base.metadata.drop_all(db.engine())
    with db.engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    env = {**os.environ, **prepare(True, a.database_url, sync_minutes=1), "PYTHONPATH": str(ROOT),
           "WORKFORCE_WORKER_CONCURRENCY": "2"}
    env.pop("ANTHROPIC_API_KEY", None)  # the soak never spends money; AI paths are covered by the test suite
    OUT.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{a.port}"

    def start(kind: str, n: int = 0):
        cmd = ["serve", "--host", "127.0.0.1", "--port", str(a.port)] if kind == "api" else ["worker"]
        log = (OUT / f"{kind}{n or ''}.log").open("a")
        return subprocess.Popen([sys.executable, "-m", "workforce.cli", *cmd], env={**env, "HOSTNAME": f"{kind}{n}"},
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)

    procs = {"api": start("api"), **{f"worker{i}": start("worker", i) for i in range(1, a.workers + 1)}}
    print(f"soak: {seconds / 60:.0f} min on {a.database_url.split('@')[-1]} · API {base} · {a.workers} workers · logs in {OUT}")
    rng = random.Random(7)
    people = [Browser(base, u[0]) for slug in COMPANIES for u in USERS[slug]]
    stop, streamed = threading.Event(), [0]
    metrics, deaths, chaos_done = [], [], set()
    try:
        for _ in range(90):  # wait until the API answers, however long start-up takes on this machine
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        for p in people:
            if not p.login():
                sys.exit(f"cannot sign in as {p.email}; see {OUT / 'api.log'}")
        threading.Thread(target=stream, args=(base, people[0], stop, streamed), daemon=True).start()
        started = time.monotonic()
        next_sample = started
        while time.monotonic() - started < seconds:
            elapsed = time.monotonic() - started
            for name, p in procs.items():
                if p.poll() is not None and name not in chaos_done:
                    deaths.append(f"{name} exited with {p.returncode} at {elapsed / 60:.1f} min")
                    procs[name] = start(name.rstrip("0123456789"), int(re.sub(r"\D", "", name) or 0))  # as a supervisor would
            if a.chaos and "kill" not in chaos_done and elapsed > seconds / 3:
                procs["worker1"].send_signal(signal.SIGKILL)  # a crash in the middle of work
                procs["worker1"].wait()
                procs["worker1"] = start("worker", 1)
                chaos_done.add("kill")
                print(f"chaos: worker1 killed and restarted at {elapsed / 60:.1f} min")
            if a.chaos and a.pg_container and "db" not in chaos_done and elapsed > 2 * seconds / 3:
                subprocess.run(["podman", "restart", a.pg_container], check=False, capture_output=True)
                chaos_done.add("db")
                print(f"chaos: database restarted at {elapsed / 60:.1f} min")
            for p in rng.sample(people, k=min(3, len(people))):
                act(p, rng)
            if time.monotonic() >= next_sample:
                try:
                    m = {"minute": round(elapsed / 60, 1), **sample(env)}
                except Exception as e:  # the database may be restarting
                    m = {"minute": round(elapsed / 60, 1), "sample_error": type(e).__name__}
                m["rss_mb"] = {n: rss_mb(p.pid) for n, p in procs.items()}
                recent = [t for p in people for t in p.timings[-50:]]
                m["p95_ms"] = round(sorted(recent)[int(len(recent) * 0.95)] * 1000) if recent else None
                m["streamed_events"] = streamed[0]
                metrics.append(m)
                with (OUT / "metrics.jsonl").open("a") as f:
                    f.write(json.dumps(m) + "\n")
                print(json.dumps(m))
                next_sample += 60
            time.sleep(5)
    finally:
        stop.set()
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=70)
            except subprocess.TimeoutExpired:
                p.kill()

    sys.exit(verdict(metrics, people, deaths, a))


def verdict(metrics: list[dict], people: list, deaths: list[str], a) -> int:
    good = [m for m in metrics if "sample_error" not in m and m["minute"] >= 1]  # the first minute is start-up and the first import
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    check("no process died on its own", not deaths, "; ".join(deaths))
    if len(good) >= 4:
        q = good[len(good) // 4]
        last = good[-1]
        for n in ("api", "worker2"):
            before, after = (q["rss_mb"] or {}).get(n), (last["rss_mb"] or {}).get(n)
            if before and after:
                check(f"{n} memory stays flat (≤ +50% after warm-up)", after <= before * 1.5 + 20, f"{before} → {after} MB")
        check("worker healthy at the end", last["health"] == "ok", last["health"])
        check("nothing stuck past its lease", last["stuck"] == 0, str(last["stuck"]))
        check("notifications delivered", last["pending_notifications"] <= 5, str(last["pending_notifications"]))
        check("audit chains intact", all(m.get("audit_ok") for m in good))
        p95 = [m["p95_ms"] for m in good if m.get("p95_ms") is not None]
        check("p95 response under 1.5 s", p95 and max(p95) < 1500, f"worst {max(p95) if p95 else 'n/a'} ms")
    else:
        check("enough samples to judge", False, f"{len(good)} samples")
    errors = [e for p in people for e in p.errors]
    window = 120 if a.chaos else 0  # requests during an injected database restart may fail; nothing else may
    check("API answered every request", len(errors) <= (len(people) * 3 if window else 0), f"{len(errors)} failed: {errors[:3]}")
    logged = [ln for ln in log_errors(sorted(OUT.glob("*.log"))) if not (a.chaos and ("OperationalError" in ln or "connection" in ln.lower()))]
    check("no unexpected errors in the logs", not logged, "; ".join(logged[:3]))
    print("\nSoak verdict")
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    failed = [c for c in checks if not c[1]]
    print(f"\n{'PASSED' if not failed else f'FAILED ({len(failed)} checks)'} · metrics in {OUT / 'metrics.jsonl'}")
    return 1 if failed else 0


if __name__ == "__main__":
    main()
