"""
CR-005 - background scheduler and periodic status checks: acceptance tests.

Run from the project folder:   python tests/test_cr005_scheduler.py

No router or network is needed. Needs `node` only for the dashboard JavaScript check
(skipped when node is not installed).
"""
import asyncio, json, os, re, shutil, subprocess, sys, tempfile, time, types
from contextlib import asynccontextmanager

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT); os.chdir(ROOT)
os.environ["MIKRAPP_DB"] = tempfile.mktemp(suffix=".db")
os.environ["MIKRAPP_BACKUP_DIR"] = tempfile.mkdtemp()

# --- stand-ins for packages missing in a bare sandbox (the real ones are used when installed) ---
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
        def __init__(self, *a, **k): self.state = types.SimpleNamespace(); self.lifespan = k.get("lifespan")
        def add_middleware(self, *a, **k): pass
        def mount(self, *a, **k): pass
        def include_router(self, *a, **k): pass
    _stub("fastapi", APIRouter=_Router, Request=object, Form=lambda *a, **k: None, FastAPI=_App)
    class _Resp:
        def __init__(self, content=None, status_code=200, *a, **k): self.body = json.dumps(content).encode() if content is not None else b""; self.url = content
    _stub("fastapi.responses", JSONResponse=_Resp, RedirectResponse=_Resp, FileResponse=_Resp)
    _stub("fastapi.staticfiles", StaticFiles=lambda **k: None)
    _stub("fastapi.templating", Jinja2Templates=lambda **k: None)
    _stub("starlette"); _stub("starlette.middleware")
    _stub("starlette.middleware.sessions", SessionMiddleware=object)
    _stub("starlette.middleware.base", BaseHTTPMiddleware=object)

import config
from db.connection import Database
from db.devices_repo import DeviceRepository
import services.device_service as ds
from services.scheduler_service import Scheduler

PASSED = []
def ok(name): PASSED.append(name); print(f"  PASS  {name}")
def new_repo(): return DeviceRepository(Database(tempfile.mktemp(suffix=".db")))

# ------------------------------------------------------------------ scheduler timing
class FakeService:
    def __init__(self, duration=0.0, fail_rounds=()):
        self.starts, self.running, self.peak, self.duration, self.fail_rounds = [], 0, 0, duration, set(fail_rounds)
    async def check_all(self, full=False, trigger='manual'):
        self.starts.append(time.monotonic()); n = len(self.starts)
        self.running += 1; self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.duration)
            if n in self.fail_rounds: raise RuntimeError("boom")
            return []
        finally: self.running -= 1

async def test_timing():
    print("Scheduler timing")
    svc = FakeService(); sch = Scheduler(svc, interval=0.3); t0 = time.monotonic()
    await sch.start(); await asyncio.sleep(0.05)
    assert len(svc.starts) == 1 and svc.starts[0] - t0 < 0.1
    ok("AC-1: first round starts at once, nobody has to open a page")
    await asyncio.sleep(0.75); await sch.stop()
    gaps = [b - a for a, b in zip(svc.starts, svc.starts[1:])]
    assert len(svc.starts) >= 3 and all(0.25 < g < 0.4 for g in gaps), gaps
    ok(f"AC-2: next round every interval (gaps {[round(g,2) for g in gaps]})")

    svc = FakeService(duration=0.5); sch = Scheduler(svc, interval=0.2); t0 = time.monotonic()
    await sch.start(); await asyncio.sleep(1.45); await sch.stop()
    rel = [round(s - t0, 1) for s in svc.starts]
    assert svc.peak == 1, "rounds overlapped"
    assert rel[:3] == [0.0, 0.6, 1.2], rel            # slots 0.2 and 0.4 were SKIPPED, not queued
    ok(f"AC-3: overrunning round -> missed slots skipped, never queued, never overlapping (starts {rel})")

    svc = FakeService(fail_rounds={1}); sch = Scheduler(svc, interval=0.2)
    await sch.start(); await asyncio.sleep(0.65); await sch.stop()
    assert len(svc.starts) >= 3
    ok("a round that crashes does not kill the scheduler")

    svc = FakeService(); sch = Scheduler(svc, interval=0.2, enabled=False)
    await sch.start(); await asyncio.sleep(0.3); await sch.stop()
    assert svc.starts == [] and sch._task is None
    ok("AC-7: scheduler switched off -> no automatic checks")

    svc = FakeService(duration=30); sch = Scheduler(svc, interval=1)
    await sch.start(); await asyncio.sleep(0.1)
    t = time.monotonic(); await asyncio.wait_for(sch.stop(), 2); took = time.monotonic() - t
    assert took < 1 and sch._task is None and svc.running == 0
    ok(f"AC-9: stop in the middle of a round is clean and fast ({took*1000:.0f} ms, no error)")

    for var, val, want in (("MIKRAPP_CHECK_INTERVAL", None, 60), ("MIKRAPP_CHECK_INTERVAL", "2", 5), ("MIKRAPP_CHECK_INTERVAL", "90", 90)):
        env = dict(os.environ); env.pop(var, None)
        if val: env[var] = val
        out = subprocess.run([sys.executable, "-c", "import config; print(config.CHECK_INTERVAL)"], env=env, cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert int(out) == want, (val, out)
    out = subprocess.run([sys.executable, "-c", "import config; print(config.SCHEDULER_ENABLED)"], env={**os.environ, "MIKRAPP_SCHEDULER": "off"}, cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert out == "False"
    ok("settings: interval default 60 s (minimum 5), MIKRAPP_SCHEDULER=off disables")

# ------------------------------------------------------------------ real checks, real DB
async def test_checks():
    print("Round against the database")
    r = new_repo()
    a = r.create(1, "10.0.0.1", "admin", "pw"); b = r.create(1, "10.0.0.2", "admin", "pw"); c = r.create(1, "10.0.0.3", "admin", "pw")
    DOWN = {b}
    class FakeClient:
        def __init__(self, device, via=None): self.device = device
        async def collect_metrics(self, extra_commands=False, attempts=None):
            await asyncio.sleep(0.05)
            if self.device.id in DOWN: raise OSError("timed out")
            return {"identity": f"id{self.device.id}", "cpu_usage": 3.0, "ram_usage": 20.0, "uptime": "1d"}
        async def is_reachable(self, attempts=None): return self.device.id not in DOWN
    ds.MikrotikClient = FakeClient
    svc = ds.DeviceService(r)
    assert r.last_check_stamp() == ""
    await svc.check_all()
    g = r.get
    assert g(a).status == "online" and g(c).status == "online" and g(b).status == "offline" and "timed out" in g(b).last_error
    ok("AC-4: unreachable device -> offline with the reason; all others still checked")
    assert all(g(i).last_check_at for i in (a, b, c))
    ok("every device records the time of its last check (also the offline one)")
    DOWN.clear(); await svc.check_all()
    assert g(b).status == "online" and g(b).last_error == ""
    ok("AC-5: device answers again -> online, error cleared")

    real = svc.check_device
    async def flaky(device, full=False, trigger="manual"):
        if device.id == c: raise RuntimeError("unexpected bug")
        return await real(device, full=full, trigger=trigger)
    svc.check_device = flaky
    res = {x.device_id: x for x in await svc.check_all()}
    assert res[a].status == "online" and res[b].status == "online" and "unexpected bug" in res[c].error
    ok("an unexpected error in ONE check does not lose the other results")
    svc.check_device = real

    # AC-6 data side: the stamp moves when a check finishes, and only then
    s1 = r.last_check_stamp(); assert r.last_check_stamp() == s1
    await asyncio.sleep(1.1); await svc.check_device(g(a)); s2 = r.last_check_stamp()
    assert s2 > s1
    ok("AC-6: 'last check' stamp is stable until a new check finishes, then changes")

    # AC-8: manual 'Check all' while the scheduler is running
    sch = Scheduler(svc, interval=0.2); await sch.start(); await asyncio.sleep(0.1)
    manual = await asyncio.wait_for(svc.check_all(full=True), 5); await sch.stop()
    assert len(manual) == 3
    ok("AC-8: pressing 'Check all' during scheduled rounds works")

# ------------------------------------------------------------------ migration + UI
async def test_migration_and_ui():
    print("Upgrade, dashboard and JavaScript")
    for old in ("/tmp/old_format.db", "/tmp/cr1_old.db"):
        if not os.path.exists(old): continue
        copy = tempfile.mktemp(suffix=".db"); shutil.copy(old, copy)
        r = DeviceRepository(Database(copy)); d = r.list_all()[0]
        assert d.last_check_at is None and d.port == 3316
        r.set_status(d.id, "offline", "x"); assert r.get(d.id).last_check_at
        ok(f"AC-10: existing database ({os.path.basename(old)}) upgraded automatically, data intact")
        break
    else:
        r = new_repo()
        with r.db.connect() as conn:
            conn.execute("ALTER TABLE devices DROP COLUMN last_check_at")
        Database(r.db.path)
        assert "last_check_at" in {x["name"] for x in r.db.connect().__enter__().execute("PRAGMA table_info(devices)")}
        ok("AC-10: a database without the column is upgraded automatically")

    from jinja2 import Environment, FileSystemLoader
    from db.sites_repo import SiteRepository
    r = new_repo(); did = r.create(1, "10.0.0.1", "admin", "pw"); r.set_status(did, "online")
    env = Environment(loader=FileSystemLoader("templates"))
    class R: pass
    html = env.get_template("dashboard.html").render(request=R(), devices=r.list_all(), stats=r.stats(), active="dashboard",
                                                      stamp=r.last_check_stamp(), interval=60)
    assert 'data-interval="60"' in html and f'data-stamp="{r.last_check_stamp()}"' in html and "Last check" in html
    assert 'class="ts" data-ts="20' in html
    ok("AC-6: dashboard carries the interval, the stamp and a 'Last check' column")

    import routers.dashboard as rd
    resp = rd.api_stamp(types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(devices_repo=r))))
    body = getattr(resp, "body", None)
    if body: assert json.loads(body)["stamp"] == r.last_check_stamp()
    ok("/api/stamp answers with the latest check time")

    if not shutil.which("node"): print("  SKIP  JavaScript check (node not installed)"); return
    js = open("static/app.js", encoding="utf-8").read()
    script = r"""
const vm = require('vm'); const src = process.argv[1];
let timers = [], reloads = 0, nextStamp = 'A', failFetch = false;
const ctx = { console, parseInt, isNaN, Date,
  document: { addEventListener(){}, querySelectorAll(){ return []; }, getElementById(){ return null; } },
  setInterval: (fn, ms) => { timers.push({fn, ms}); return 1; },
  location: { reload(){ reloads++; } },
  fetch: async () => { if (failFetch) throw new Error('offline'); return { ok: true, json: async () => ({ stamp: nextStamp }) }; } };
vm.createContext(ctx); vm.runInContext(src, ctx);
(async () => {
  vm.runInContext("watchForNewChecks({dataset:{interval:'60', stamp:'A'}})", ctx);
  if (timers.length !== 1 || timers[0].ms !== 60000) throw new Error('interval must be 60000 ms, got ' + JSON.stringify(timers.map(t=>t.ms)));
  await timers[0].fn(); await timers[0].fn();
  if (reloads !== 0) throw new Error('reloaded although nothing changed');
  failFetch = true; await timers[0].fn();
  if (reloads !== 0) throw new Error('reloaded on a failed request');
  failFetch = false; nextStamp = 'B'; await timers[0].fn();
  if (reloads !== 1) throw new Error('did not reload after a new check');
  vm.runInContext("watchForNewChecks({dataset:{interval:'0', stamp:''}})", ctx);
  if (timers.length !== 1) throw new Error('must not poll without an interval');
  console.log('js ok');
})().catch(e => { console.error('JS FAIL:', e.message); process.exit(1); });
"""
    out = subprocess.run(["node", "-e", script, js], capture_output=True, text=True)
    assert out.returncode == 0 and "js ok" in out.stdout, out.stderr + out.stdout
    ok("dashboard JavaScript: polls every interval, NO reload when nothing changed or the request fails, reloads when a new check finished")

# ------------------------------------------------------------------ real wiring + no notifications
async def test_wiring():
    print("Application wiring")
    import app as appmod
    application = appmod.create_app()
    assert isinstance(application.state.scheduler, Scheduler)
    async with appmod.lifespan(application):
        await asyncio.sleep(0.2)
        assert application.state.scheduler.rounds_started >= 1
    assert application.state.scheduler._task is None
    ok("AC-1/AC-9: the real application starts the scheduler with itself and stops it on shutdown")

    bad = re.compile(r"smtplib|sendmail|telegram|twilio|webhook|slack", re.I)
    hits = []
    for base, _, files in os.walk(ROOT):
        if "tests" in base or "__pycache__" in base: continue
        for f in files:
            if f.endswith(".py") and bad.search(open(os.path.join(base, f), encoding="utf-8").read()): hits.append(f)
    assert not hits, hits
    ok("AC-11: no notification code anywhere in the application")

async def main():
    for fn in (test_timing, test_checks, test_migration_and_ui, test_wiring): await fn()
    print(f"\nALL {len(PASSED)} CR-005 CHECKS PASSED")
asyncio.run(main())
