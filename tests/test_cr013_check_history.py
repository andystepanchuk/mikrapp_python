"""
CR-013 - check history: log file first, then database. Acceptance tests.

Run from the project folder:   python tests/test_cr013_check_history.py

No router or network is needed: only the network calls are faked. The recorder, the
log files and the database are the real ones.
"""
import asyncio, glob, json, logging, os, shutil, sys, tempfile, time, types
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT); os.chdir(ROOT)
APP_LOG_DIR = tempfile.mkdtemp()
os.environ.update(MIKRAPP_DB=tempfile.mktemp(suffix=".db"), MIKRAPP_BACKUP_DIR=tempfile.mkdtemp(),
                  MIKRAPP_CHECK_LOG=APP_LOG_DIR, MIKRAPP_SCHEDULER="off")

def _stub(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m
try:
    import asyncssh
except ImportError:
    class _E(Exception): pass
    class _C(_E):
        def __init__(self, code, reason, lang="en-US"): super().__init__(reason); self.code, self.reason = code, reason
    async def _conn(*a, **k): raise _E("stub")
    _stub("asyncssh", Error=_E, ChannelOpenError=_C, connect=_conn)
try:
    import fastapi
except ImportError:
    class _Router:
        def __init__(self, *a, **k): pass
        def get(self, *a, **k): return lambda f: f
        def post(self, *a, **k): return lambda f: f
    class _App:
        def __init__(self, *a, **k): self.state = types.SimpleNamespace()
        def add_middleware(self, *a, **k): pass
        def mount(self, *a, **k): pass
        def include_router(self, *a, **k): pass
    _stub("fastapi", APIRouter=_Router, Request=object, Form=lambda *a, **k: None, FastAPI=_App)
    class _Resp:
        def __init__(self, *a, **k): pass
    _stub("fastapi.responses", JSONResponse=_Resp, RedirectResponse=_Resp, FileResponse=_Resp)
    _stub("fastapi.staticfiles", StaticFiles=lambda **k: None)
    _stub("fastapi.templating", Jinja2Templates=lambda **k: None)
    _stub("starlette"); _stub("starlette.middleware")
    _stub("starlette.middleware.sessions", SessionMiddleware=object)
    _stub("starlette.middleware.base", BaseHTTPMiddleware=object)

import config
from db.connection import Database
from db.devices_repo import DeviceRepository
from db.check_history_repo import CheckHistoryRepository
import mikrotik.client as mc
import services.device_service as ds
from services.check_recorder import CheckRecorder
from models import CheckRecord

PASSED = []
def ok(name): PASSED.append(name); print(f"  PASS  {name}")

# ---------------------------------------------------------------- fake network (only this is fake)
BEH = {}      # ip -> behaviour
class FakeApi:
    async def talk(self, cmd, params=None):
        b = BEH[params["address"]]
        n = b.get("ping_received", 3)
        return [{"sent": "3", "received": str(n), "avg-rtt": "4ms"}]
@asynccontextmanager
async def fake__api(self):
    yield FakeApi()
async def fake_collect_api(self, extra):
    b = BEH[self.device.ip]; await asyncio.sleep(b.get("api_ms", 0.0))
    if b.get("api_err"): raise OSError(b["api_err"])
    return {"identity": "rtr", "cpu_usage": 3.0, "ram_usage": 20.0, "uptime": "1d"}
async def fake_collect_ssh(self, extra):
    b = BEH[self.device.ip]
    if b.get("ssh_err"): raise OSError(b["ssh_err"])
    return {"identity": "rtr", "cpu_usage": 3.0, "ram_usage": 20.0, "uptime": "1d"}
async def fake_probe(host, port, timeout=0.6): return BEH[host].get("probe", False)
mc.MikrotikClient._api = fake__api
mc.MikrotikClient._collect_via_api = fake_collect_api
mc.MikrotikClient._collect_via_ssh = fake_collect_ssh
mc.probe_port = fake_probe

def new_env(log_dir=None, **kw):
    db = Database(tempfile.mktemp(suffix=".db")); repo = DeviceRepository(db); hist = CheckHistoryRepository(db)
    log_dir = log_dir if log_dir is not None else tempfile.mkdtemp()
    rec = CheckRecorder(log_dir, kw.get("log_days", 30), hist, kw.get("hist_days", 90))
    return repo, hist, rec, ds.DeviceService(repo, recorder=rec), log_dir
def mk(repo, ip, name="", agent=None, kind="mikrotik", pw="pw"):
    return repo.create(1, ip, "admin" if pw else "", pw, "auto", 22, 8728, kind, agent, name)
def lines(log_dir):
    out = []
    for f in sorted(glob.glob(os.path.join(log_dir, "checks-*.jsonl"))):
        out += [json.loads(l) for l in open(f, encoding="utf-8").read().splitlines()]
    return out

async def test_records():
    print("What a record contains")
    repo, hist, rec, svc, d = new_env(); await rec.start()
    a = mk(repo, "10.0.0.1", "Core"); BEH["10.0.0.1"] = {}
    await svc.check_device(repo.get(a)); await rec.flush()
    (l,) = lines(d)
    for key in ("ts", "device_id", "name", "ip", "route", "trigger", "status", "previous_status",
                "status_changed", "latency_ms", "error", "duration_ms", "attempts"):
        assert key in l, key
    assert l["name"] == "Core" and l["ip"] == "10.0.0.1" and l["route"] == "direct" and l["trigger"] == "manual"
    assert l["status"] == "online" and l["attempts"][0]["step"] == "api" and l["attempts"][0]["ok"] is True
    assert l["ts"].endswith("+00:00") and l["duration_ms"] >= 0
    ok("AC-1/AC-2: one JSON line per check with time, device, address, route, trigger, result, error, duration, attempts")

    BEH["10.0.0.1"] = {"api_err": "timed out", "api_ms": 0.05}
    await svc.check_device(repo.get(a)); await rec.flush()
    l = lines(d)[-1]
    assert [x["step"] for x in l["attempts"]] == ["api", "ssh"]
    assert l["attempts"][0]["ok"] is False and l["attempts"][0]["error"] == "timed out" and l["attempts"][0]["ms"] >= 40
    assert l["attempts"][1]["ok"] is True and l["status"] == "online"
    ok("AC-3: 'API failed (timed out), then SSH worked' is visible inside the line, with timings")

    BEH["10.0.0.1"] = {"api_err": "timed out", "ssh_err": "refused", "probe": False}
    await svc.check_device(repo.get(a)); await rec.flush()
    l = lines(d)[-1]
    assert [x["step"] for x in l["attempts"]] == ["api", "ssh", "probe"] and l["status"] == "offline"
    assert "timed out" in l["error"] or "refused" in l["error"]
    text = hist.latest_for_device(a, 1)[0].attempts_text
    assert "api failed" in text and "ssh failed" in text and "probe failed" in text
    ok("AC-3: an offline device shows every attempt (api, ssh, probe) and the reason")
    await rec.stop()

async def test_status_change():
    print("Status changes")
    repo, hist, rec, svc, d = new_env(); await rec.start()
    a = mk(repo, "10.0.0.2", "X")
    seq = [({}, False, "online"), ({}, False, "online"),
           ({"api_err": "x", "ssh_err": "y", "probe": False}, True, "offline"),
           ({"api_err": "x", "ssh_err": "y", "probe": False}, False, "offline"),
           ({}, True, "online")]
    for beh, changed, status in seq:
        BEH["10.0.0.2"] = beh; await svc.check_device(repo.get(a)); await rec.flush()
        l = lines(d)[-1]
        assert (l["status"], l["status_changed"]) == (status, changed), (l["status"], l["status_changed"], status, changed)
    assert lines(d)[0]["previous_status"] == "checking" and lines(d)[2]["previous_status"] == "online"
    ok("AC-4: first check is not a change; online->offline and back are marked; repeats are not")
    await rec.stop()

async def test_routes_and_triggers():
    print("Routes, triggers, volume")
    repo, hist, rec, svc, d = new_env(); await rec.start()
    a1 = mk(repo, "10.1.0.1", "A1"); a2 = mk(repo, "10.2.0.1", "A2", a1)
    h = mk(repo, "10.2.0.50", "Printer", a2, "generic", ""); m = mk(repo, "10.0.0.9", "M")
    for ip in ("10.1.0.1", "10.2.0.1", "10.2.0.50", "10.0.0.9"): BEH[ip] = {}
    await svc.check_device(repo.get(h)); await rec.flush()
    l = [x for x in lines(d) if x["name"] == "Printer"][0]
    assert l["route"] == "A1 \u2192 A2" and l["latency_ms"] == 4.0 and l["attempts"][0]["step"] == "ping-api"
    ok("host behind two agents: route 'A1 \u2192 A2', ping latency and the ping attempt recorded")
    BEH["10.2.0.50"] = {"ping_received": 0}
    await svc.check_device(repo.get(h)); await rec.flush()
    l = lines(d)[-1]
    assert l["status"] == "offline" and l["attempts"][0]["ok"] is False and "no reply" in l["attempts"][0]["error"]
    ok("no ping reply -> offline; the ping attempt says 'no reply (0 of 3)'")

    n0 = len(lines(d)); await svc.check_all(trigger="scheduler"); await svc.check_all(); await rec.flush()
    new = lines(d)[n0:]
    assert len(new) == 8 and sum(1 for x in new if x["trigger"] == "scheduler") == 4 and sum(1 for x in new if x["trigger"] == "manual") == 4
    assert hist.count() == len(lines(d))
    ok("AC-1/AC-8: 2 rounds x 4 devices = exactly 8 lines, and the database has the same number as the file")
    row = hist.latest_for_device(m, 1)[0]
    assert row.device_name == "M" and row.ip == "10.0.0.9" and row.attempts and row.trigger == "manual"
    ok("AC-8: the database row carries the same fields, attempts included")
    await rec.stop()

async def test_files_and_retention():
    print("Files and retention")
    repo, hist, rec, svc, d = new_env(log_days=30, hist_days=90); await rec.start()
    now = datetime.now(timezone.utc)
    def r(day_offset, name="n"):
        return CheckRecord(device_id=1, checked_at=(now - timedelta(days=day_offset)).isoformat(timespec="milliseconds"),
                           device_name=name, ip="1.1.1.1", route="direct", trigger="manual", status="online",
                           previous_status="online", status_changed=False, latency_ms=None, error="", duration_ms=5, attempts=[])
    rec.record(r(0)); rec.record(r(1)); rec.record(r(1)); await rec.flush()
    names = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "*.jsonl")))
    assert len(names) == 2 and all(n.startswith("checks-") and n.endswith(".jsonl") for n in names)
    ok("AC-5: JSON lines, one file per UTC day (checks-YYYY-MM-DD.jsonl)")
    open(os.path.join(d, "checks-2000-01-01.jsonl"), "w").write("{}\n"); open(os.path.join(d, "notes.txt"), "w").write("keep me")
    rec.purge()
    left = set(os.listdir(d))
    assert "checks-2000-01-01.jsonl" not in left and "notes.txt" in left and len(left) == 3
    ok("AC-5: files older than MIKRAPP_CHECK_LOG_DAYS are deleted; other files are left alone")
    hist.add_many([r(200, "old"), r(100, "old2"), r(10, "recent")])
    rec.purge()
    with hist.db.connect() as c: names = {x["device_name"] for x in c.execute("SELECT device_name FROM check_history")}
    assert names == {"n", "recent"}, names
    ok("AC-9: database rows older than MIKRAPP_CHECK_HISTORY_DAYS (90) are deleted")
    keep = CheckRecorder(d, 0, hist, 0); hist.add_many([r(400, "ancient")]); keep.purge()
    assert hist.count() >= 1 and os.path.exists(os.path.join(d, names and glob.glob(os.path.join(d, "checks-*.jsonl"))[0].split(os.sep)[-1]))
    with hist.db.connect() as c: assert c.execute("SELECT COUNT(*) FROM check_history WHERE device_name='ancient'").fetchone()[0] == 1
    ok("retention 0 = keep forever")
    await rec.stop()

async def test_failures_and_speed():
    print("Never in the way of a check")
    # a log folder that cannot be created: the path is an existing FILE
    blocker = tempfile.mktemp(); open(blocker, "w").write("x")
    repo, hist, rec, svc, _ = new_env(log_dir=blocker); await rec.start()
    a = mk(repo, "10.0.0.3", "Y"); BEH["10.0.0.3"] = {}
    handler_msgs = []
    class H(logging.Handler):
        def emit(self, record): handler_msgs.append(record.getMessage())
    logging.getLogger("mikrapp.history").addHandler(H())
    res = []
    for _ in range(3):
        res.append(await svc.check_device(repo.get(a))); await rec.flush()
    assert all(x.status == "online" for x in res)
    assert hist.count() == 3, "the database sink must keep working when the file sink is broken"
    warns = [m for m in handler_msgs if "log file failed" in m]
    assert len(warns) == 1, warns
    ok("AC-6: log file broken -> checks still succeed, database still written, ONE warning (not one per line)")
    await rec.stop()

    class BrokenRepo:
        def add_many(self, recs): raise RuntimeError("database is locked")
        def purge_older_than(self, c): return 0
    d2 = tempfile.mkdtemp(); rec2 = CheckRecorder(d2, 30, BrokenRepo(), 90); repo2 = DeviceRepository(Database(tempfile.mktemp(suffix=".db")))
    svc2 = ds.DeviceService(repo2, recorder=rec2); await rec2.start()
    b = mk(repo2, "10.0.0.3", "Z"); r2 = await svc2.check_device(repo2.get(b)); await rec2.flush()
    assert r2.status == "online" and len(lines(d2)) == 1
    ok("AC-6: database broken -> checks still succeed, log file still written")
    await rec2.stop()

    repo, hist, rec, svc, d = new_env(); await rec.start()
    ids = [mk(repo, f"10.9.0.{i}", f"h{i}") for i in range(5)]
    for i in range(5): BEH[f"10.9.0.{i}"] = {}
    orig = rec._write_file
    def slow(batch): time.sleep(1.0); orig(batch)
    rec._write_file = slow
    t = time.monotonic(); await svc.check_all(); took = time.monotonic() - t
    assert took < 0.5, took
    assert len(lines(d)) < 5, "the write must happen after the checks returned"
    await rec.flush(); assert len(lines(d)) == 5
    ok(f"AC-7: a 1-second-slow disk does not delay checks (5 checks returned in {took*1000:.0f} ms; lines arrived later)")
    await rec.stop()

async def test_flush_and_survival():
    print("Shutdown and deleted devices")
    repo, hist, rec, svc, d = new_env(); await rec.start()
    a = mk(repo, "10.0.0.4", "Gone"); BEH["10.0.0.4"] = {}
    for _ in range(20): await svc.check_device(repo.get(a))
    await rec.stop()                                   # no explicit flush
    assert len(lines(d)) == 20 and hist.count() == 20
    ok("AC-12: shutting down writes everything still queued (20 of 20)")
    repo.delete(a)
    rows = hist.latest_for_device(a, 5)
    assert len(rows) == 5 and rows[0].device_name == "Gone" and rows[0].ip == "10.0.0.4"
    ok("AC-12: the history of a deleted device stays, with its name and address")

async def test_device_page_and_upgrade():
    print("Device page and upgrade")
    repo, hist, rec, svc, d = new_env(); await rec.start()
    a = mk(repo, "10.0.0.5", "P"); BEH["10.0.0.5"] = {}
    for i in range(60): await svc.check_device(repo.get(a))
    BEH["10.0.0.5"] = {"api_err": "boom", "ssh_err": "boom", "probe": False}; await svc.check_device(repo.get(a)); await rec.flush()
    rows = hist.latest_for_device(a, 50)
    assert len(rows) == 50 and rows[0].status == "offline" and rows[0].checked_at >= rows[-1].checked_at
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("templates"))
    class R: pass
    html = env.get_template("device_detail.html").render(
        request=R(), active="devices", device=repo.get(a), sites=[], agents=[], offline_for="", history=rows,
        detail=dict(error=None, interfaces=[], ip_addresses=[], dhcp_leases=[], neighbors_by_interface={}, watchdog=None, backups=[]))
    assert "Check history" in html and "changed from online" in html and "api failed" in html and html.count('class="ts"') >= 50
    ok("AC-10: the device page shows the latest 50 checks, newest first, with changes and attempts")
    await rec.stop()
    for old in ("/tmp/old_format.db", "/tmp/cr1_old.db"):
        if os.path.exists(old):
            c = tempfile.mktemp(suffix=".db"); shutil.copy(old, c); db = Database(c)
            with db.connect() as conn:
                assert conn.execute("SELECT name FROM sqlite_master WHERE name='check_history'").fetchone()
            CheckHistoryRepository(db).add_many([]); ok(f"AC-11: existing database ({os.path.basename(old)}) gets the new table automatically"); break
    else:
        ok("AC-11: new table is part of the schema (no old database file here to upgrade)")

async def test_wiring():
    print("The real application")
    import app as appmod
    application = appmod.create_app()
    repo = application.state.devices_repo
    x = mk(repo, "10.0.0.7", "W", pw=""); BEH["10.0.0.7"] = {"probe": True}
    async with appmod.lifespan(application):
        await application.state.device_service.check_device(repo.get(x), trigger="manual")
    files = glob.glob(os.path.join(APP_LOG_DIR, "checks-*.jsonl"))
    assert files and any(json.loads(l)["name"] == "W" for l in open(files[0], encoding="utf-8"))
    assert application.state.history_repo.latest_for_device(x, 1)[0].status == "online"
    ok("the app starts the recorder with itself, a check lands in file AND database, shutdown flushes")

async def main():
    for fn in (test_records, test_status_change, test_routes_and_triggers, test_files_and_retention,
               test_failures_and_speed, test_flush_and_survival, test_device_page_and_upgrade, test_wiring):
        await fn()
    print(f"\nALL {len(PASSED)} CR-013 CHECKS PASSED")
asyncio.run(main())
