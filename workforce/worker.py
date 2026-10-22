"""Worker process: source syncs on schedule, rule passes, and AI work items from the queue.
Run one or more of these next to the API. Postgres allows several workers; SQLite runs one."""
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.exc import OperationalError

from . import actions, data, db, health, notify, quality, reports, rules, runtime, tenancy, work
from .models import WorkItem
from .settings import get_settings

log = logging.getLogger("workforce.worker")
RULE_INTERVAL = 15 * 60
HOUSEKEEPING_INTERVAL = 10 * 60  # reports due, outcome checks
WATCHDOG_SECONDS = 15 * 60  # a main loop stuck this long exits the process so the container restarts it


class Worker:
    def __init__(self):
        self.settings = get_settings()
        self.stop = threading.Event()
        self.cfgs: dict[str, tenancy.TenantConfig] = {}
        self.last_rules: dict[str, float] = {}
        self.last_housekeeping: dict[str, float] = {}
        conc = 1 if not db.is_postgres() else max(1, self.settings.worker_concurrency)
        self.pool = ThreadPoolExecutor(max_workers=conc, thread_name_prefix="work")
        self.slots = threading.Semaphore(conc)

    def start(self):
        with db.session() as s:
            self.cfgs = tenancy.register_all(s)
        if not self.cfgs:
            raise SystemExit("no tenant packs found; set WORKFORCE_TENANTS_DIR / WORKFORCE_TENANTS")
        log.info("worker started for tenants %s", ", ".join(self.cfgs))
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        self.last_loop = time.monotonic()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        name, started = f"worker:{socket.gethostname()}:{os.getpid()}", db.utcnow()
        while not self.stop.is_set():
            try:
                with db.session() as s:
                    health.beat(s, name, started)
                self.maintain()
                self.dispatch()
            except OperationalError as e:  # database restarting or unreachable: wait and carry on, no traceback
                log.warning("database unavailable, retrying: %s", str(e.orig or e).splitlines()[0][:200])
            except Exception:
                log.exception("worker loop error")
            self.last_loop = time.monotonic()
            self.stop.wait(self.settings.worker_poll_seconds)
        log.info("stopping: waiting for running work items")
        self.pool.shutdown(wait=True)

    def _watchdog(self):
        """Leases make interrupted work safe to resume, so a hung loop (a source that never answers, a lost connection)
        is ended rather than left running: the process exits and the container's restart policy brings it back."""
        while not self.stop.wait(30):
            stalled = time.monotonic() - self.last_loop
            if stalled > WATCHDOG_SECONDS:
                log.critical("worker loop stalled for %ds; exiting so the supervisor restarts it", stalled)
                logging.shutdown()
                os._exit(70)

    def maintain(self):
        with db.session() as s:
            work.recover_expired(s, self.cfgs)
        for slug, cfg in self.cfgs.items():
            try:
                with db.exclusive(f"workforce:maintain:{slug}") as mine:
                    if mine:  # with several workers, one at a time looks after each company
                        self._maintain_tenant(slug, cfg)
            except OperationalError as e:
                log.warning("tenant %s: database unavailable during housekeeping, retrying: %s", slug, str(e.orig or e).splitlines()[0][:200])
            except Exception:  # one company's problem never stops the others
                log.exception("tenant %s: housekeeping failed; retrying next loop", slug)

    def _maintain_tenant(self, slug: str, cfg: tenancy.TenantConfig):
        synced = False
        with db.session() as s:
            due = data.sources_due(s, cfg)
        for sid in due:
            with db.session() as s:
                res = data.sync_source(s, cfg, sid)
                synced = synced or bool(res)
        if synced or time.monotonic() - self.last_rules.get(slug, 0) > RULE_INTERVAL:
            with db.session() as s:
                n = rules.evaluate(s, cfg)
            self.last_rules[slug] = time.monotonic()
            if n:
                log.info("tenant %s: %d new work items", slug, n)
        notify.deliver_pending(cfg)
        if time.monotonic() - self.last_housekeeping.get(slug, -HOUSEKEEPING_INTERVAL) >= HOUSEKEEPING_INTERVAL:
            self.last_housekeeping[slug] = time.monotonic()
            with db.session() as s:
                n = actions.expire_stale(s, cfg) + actions.fail_unconfirmed(s, cfg)
            if n:
                log.info("tenant %s: %d expired decisions or unconfirmed actions closed", slug, n)
            with db.session() as s:
                quality.check_outcomes(s, cfg)
            with db.session() as s:
                quality.propose_rule_reviews(s, cfg)
            with db.session() as s:
                n = rules.schedules_due(s, cfg)
            if n:
                log.info("tenant %s: %d scheduled reviews started", slug, n)
            with db.session() as s:
                kinds = reports.due(s, cfg)
            for kind in kinds:
                with db.session() as s:
                    reports.generate(s, cfg, kind)
                log.info("tenant %s: %s report generated", slug, kind)

    def dispatch(self):
        while self.slots.acquire(blocking=False):
            with db.session() as s:
                item = work.claim(s, list(self.cfgs), self.settings.work_lease_seconds)
                claimed = (item.tenant_id, item.id) if item else None
            if not claimed:
                self.slots.release()
                return
            self.pool.submit(self._process, *claimed)

    def _process(self, tenant_id: str, work_id: str):
        try:
            with db.session() as s:
                item = s.get(WorkItem, (tenant_id, work_id))
                runtime.run(s, self.cfgs[tenant_id], item)
        except Exception as e:
            log.exception("work item %s failed", work_id)
            with db.session() as s:
                item = s.get(WorkItem, (tenant_id, work_id))
                if item and item.status not in work.TERMINAL:
                    work.retry(s, self.cfgs[tenant_id], item, f"internal error: {type(e).__name__}")
        finally:
            self.slots.release()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    Worker().start()
