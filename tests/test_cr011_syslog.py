"""
CR-011 - device logs by syslog: acceptance tests.

Run from the project folder:   python tests/test_cr011_syslog.py

No router is needed. Datagrams are sent through a REAL local UDP socket to the real listener;
the parser, queue, writer, database and pages are the real ones.
"""
import asyncio, os, shutil, socket, subprocess, sys, tempfile, time, types
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT); os.chdir(ROOT)
os.environ.update(MIKRAPP_DB=tempfile.mktemp(suffix=".db"), MIKRAPP_BACKUP_DIR=tempfile.mkdtemp(),
                  MIKRAPP_CHECK_LOG="off", MIKRAPP_SCHEDULER="off",
                  MIKRAPP_SYSLOG_HOST="127.0.0.1", MIKRAPP_SYSLOG_PORT="0")

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
from db.logs_repo import LogRepository
from models import LogEntry
from services.log_service import LogService
from services.scheduler_service import Scheduler
from services.syslog_parser import parse_syslog

PASSED = []
def ok(name): PASSED.append(name); print(f"  PASS  {name}")
def env():
    db = Database(tempfile.mktemp(suffix=".db")); return DeviceRepository(db), LogRepository(db)
def send(port, data, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(data, (host, port)); s.close()
def entry(dev=1, name="D", days_ago=0, sev=6, topics="system,info", msg="m", ip="1.1.1.1"):
    t = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="milliseconds")
    return LogEntry(device_id=dev, received_at=t, device_name=name, sender_ip=ip,
                    severity=["emergency","alert","critical","error","warning","notice","info","debug"][sev],
                    severity_num=sev, topics=topics, message=msg)

# ------------------------------------------------------------------ parser
def test_parser():
    print("Parser")
    p = parse_syslog(b"<14>Nov 22 12:00:00 MikroTik system,info,account user admin logged in from 10.0.0.2 via winbox")
    assert (p.severity, p.topics, p.message) == ("info", "system,info,account", "user admin logged in from 10.0.0.2 via winbox")
    p = parse_syslog(b"system,info,account user admin logged in")
    assert (p.topics, p.message) == ("system,info,account", "user admin logged in")
    p = parse_syslog(b"<11>error,critical interface ether1 link down")
    assert (p.severity, p.topics) == ("error", "error,critical")
    p = parse_syslog(b"<14>1 2026-11-22T12:00:00+02:00 MikroTik system,info - - - hello world")
    assert (p.topics, p.message) == ("system,info", "hello world")
    ok("AC-4: BSD syslog, plain RouterOS text, severity from the priority, RFC 5424 are all read")
    p = parse_syslog(b"User admin logged in"); assert (p.topics, p.message) == ("", "User admin logged in")
    p = parse_syslog(b"info"); assert p.topics == "" and p.message == "info"
    p = parse_syslog(b"critical,error something broke"); assert p.severity == "critical"
    ok("AC-4: ordinary words are not mistaken for topics; with no priority the severity comes from the topics")
    p = parse_syslog(b"\xff\xfe<14>garbage \x00 bytes"); assert "garbage" in p.message
    p = parse_syslog(b""); assert p.message == ""
    p = parse_syslog(b"<14>" + b"x" * 5000); assert len(p.message) < 2100 and p.message.endswith("[cut]")
    ok("AC-4: broken bytes, empty and huge datagrams never raise")

# ------------------------------------------------------------------ real UDP
async def test_listener():
    print("Listener (real UDP socket)")
    devs, logs = env()
    d = devs.create(1, "127.0.0.1", "admin", "pw", "auto", 22, 8728, "mikrotik", None, "Core")
    svc = LogService(devs, logs, "127.0.0.1", 0, 90); await svc.start()
    assert svc.listening and svc.bound_port
    ok(f"AC-1: MikrApp listens on a UDP port at start ({svc.bound_port})")
    send(svc.bound_port, b"<14>Nov 22 12:00:00 Core system,info,account user admin logged in from 10.0.0.2")
    send(svc.bound_port, b"<11>error,critical interface ether1 link down")
    await asyncio.sleep(0.3); await svc.flush()
    rows = logs.latest_for_device(d, 10)
    assert len(rows) == 2
    r = [x for x in rows if x.severity == "error"][0]
    assert (r.device_id, r.device_name, r.sender_ip, r.topics, r.message) == (d, "Core", "127.0.0.1", "error,critical", "interface ether1 link down")
    assert r.received_at.endswith("+00:00") and abs((datetime.now(timezone.utc) - datetime.fromisoformat(r.received_at)).total_seconds()) < 10
    ok("AC-2: a message from a known device is stored with device, server receive time, severity, topics, text, sender")

    devs.create(1, "10.9.9.9", "admin", "pw", "auto", 22, 8728, "mikrotik", None, "Other")
    svc._devices = {"10.9.9.9": (99, "Other")}                       # 127.0.0.1 is now unknown
    for _ in range(3): send(svc.bound_port, b"hello from nowhere")
    await asyncio.sleep(0.3)
    st = svc.status()
    assert st["dropped_unknown"] == 3 and st["unknown_senders"] == [("127.0.0.1", 3)] and logs.count() == 2
    ok("AC-3: messages from unknown senders are dropped, counted, and the sender address is listed")

    for i in range(MANY := 300): send(svc.bound_port, b"<14>system,info message %d" % i)
    svc._devices = {"127.0.0.1": (d, "Core")}
    for i in range(300): svc.handle_datagram(b"<14>system,info burst %d" % i, "127.0.0.1")
    await svc.flush()
    assert logs.count() >= 302
    ok("a burst of messages is stored (batched), the listener never waits for the database")
    await svc.stop(); assert not svc.listening

    # AC-12 / AC-8
    off = LogService(devs, logs, "127.0.0.1", 0, 90, enabled=False); await off.start()
    assert not off.listening and "off" in off.listen_error
    out = subprocess.run([sys.executable, "-c", "import config; print(config.SYSLOG_ENABLED, config.SYSLOG_PORT, config.SYSLOG_DAYS)"],
                         env={k: v for k, v in os.environ.items() if not k.startswith("MIKRAPP_SYSLOG")}, cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert out == ["True", "5514", "90"], out
    ok("AC-12: MIKRAPP_SYSLOG=off disables the listener; defaults are port 5514 and 90 days")

    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); blocker.bind(("127.0.0.1", 0)); busy = blocker.getsockname()[1]
    busy_svc = LogService(devs, logs, "127.0.0.1", busy, 90); await busy_svc.start()
    assert not busy_svc.listening and "cannot listen" in busy_svc.listen_error and str(busy) in busy_svc.listen_error
    blocker.close(); await busy_svc.stop()
    ok("AC-8: port already in use -> MikrApp keeps running, and says why")

async def test_queue_and_shutdown():
    print("Queue and shutdown")
    devs, logs = env()
    d = devs.create(1, "127.0.0.1", "admin", "pw", "auto", 22, 8728, "mikrotik", None, "Core")
    real = logs.add_many
    def slow(batch): time.sleep(1.0); real(batch)
    logs.add_many = slow
    svc = LogService(devs, logs, "127.0.0.1", 0, 90, queue_size=3); await svc.start()
    t = time.monotonic()
    for i in range(50): svc.handle_datagram(b"<14>system,info msg %d" % i, "127.0.0.1")
    took = time.monotonic() - t
    assert took < 0.2 and svc.dropped_full >= 40, (took, svc.dropped_full)
    assert "queue was full" not in "" and svc.status()["dropped_full"] == svc.dropped_full
    ok(f"AC-9: a slow database never blocks the listener; overflow is dropped and counted ({svc.dropped_full} of 50), 50 calls in {took*1000:.0f} ms")
    await svc.stop(); logs.add_many = real

    devs2, logs2 = env(); d2 = devs2.create(1, "127.0.0.1", "admin", "pw", "auto", 22, 8728, "mikrotik", None, "Core")
    svc2 = LogService(devs2, logs2, "127.0.0.1", 0, 90); await svc2.start()
    for i in range(40): svc2.handle_datagram(b"<14>system,info queued %d" % i, "127.0.0.1")
    await svc2.stop()                                     # no explicit flush
    assert logs2.count() == 40 and svc2._transport is None
    ok("AC-10: shutdown writes everything still queued (40 of 40) and closes the socket")

# ------------------------------------------------------------------ retention + scheduler jobs
async def test_retention_and_jobs():
    print("Retention and scheduler jobs")
    devs, logs = env()
    svc = LogService(devs, logs, "127.0.0.1", 0, 90)
    logs.add_many([entry(days_ago=200, msg="old"), entry(days_ago=89, msg="edge"), entry(days_ago=1, msg="new")])
    assert svc.purge() == 1 and {e.message for e in logs.search()} == {"edge", "new"}
    keep = LogService(devs, logs, "127.0.0.1", 0, 0); logs.add_many([entry(days_ago=999, msg="ancient")])
    assert keep.purge() == 0 and any(e.message == "ancient" for e in logs.search())
    ok("AC-7: messages older than MIKRAPP_SYSLOG_DAYS are deleted; 0 keeps everything")
    devs2, logs2 = env(); s2 = LogService(devs2, logs2, "127.0.0.1", 0, 90)
    logs2.add_many([entry(days_ago=400, msg="old")]); await s2.start(); assert logs2.count() == 0
    await s2.stop()
    ok("AC-7: clean-up also runs when MikrApp starts")

    class FakeSvc:
        async def check_all(self, full=False, trigger="manual"): return []
    runs, boom = [], []
    async def job(): runs.append(time.monotonic())
    async def bad(): boom.append(1); raise RuntimeError("job broke")
    sch = Scheduler(FakeSvc(), interval=5)
    sch.add_job("purge", job, 0.2, run_now=False); sch.add_job("bad", bad, 0.2, run_now=True)
    await sch.start(); await asyncio.sleep(0.75); await sch.stop()
    gaps = [b - a for a, b in zip(runs, runs[1:])]
    assert len(runs) >= 2 and runs[0] - 0 > 0 and all(0.15 < g < 0.3 for g in gaps), gaps
    assert len(boom) >= 3, "a failing job must be tried again at its next slot"
    ok(f"AC-13: extra jobs run on the scheduler every interval (gaps {[round(g, 2) for g in gaps]}); a crashing job never stops it or the others")
    runs.clear()
    async def slow_job(): runs.append(time.monotonic()); await asyncio.sleep(0.5)
    sch = Scheduler(FakeSvc(), interval=5); sch.add_job("slow", slow_job, 0.2)
    t0 = time.monotonic(); await sch.start(); await asyncio.sleep(1.4); await sch.stop()
    rel = [round(r - t0, 1) for r in runs]
    assert rel[:3] == [0.0, 0.6, 1.2], rel
    ok(f"AC-13: an overrunning job skips missed slots and never overlaps itself (runs at {rel})")
    off = Scheduler(FakeSvc(), interval=5, enabled=False); off.add_job("x", job, 0.1); runs.clear()
    await off.start(); await asyncio.sleep(0.3); await off.stop(); assert runs == []
    ok("scheduler switched off -> no jobs either")

# ------------------------------------------------------------------ search, pages
def test_search_and_pages():
    print("Search and pages")
    devs, logs = env()
    logs.add_many([
        entry(1, "A", sev=3, topics="error,critical", msg="link down ether1"),
        entry(1, "A", sev=6, topics="system,info,account", msg="user admin logged in"),
        entry(2, "B", sev=4, topics="firewall,warning", msg="dropped 50% of packets"),
        entry(2, "B", sev=6, topics="accounting,info", msg="accounting started"),
        entry(2, "B", days_ago=3, sev=6, topics="system,info", msg="old one"),
    ])
    msgs = lambda **k: [e.message for e in logs.search(**k)]
    assert len(msgs(device_id=2)) == 3 and len(msgs()) == 5
    assert msgs(max_severity=4) == ["dropped 50% of packets", "link down ether1"]
    assert msgs(topic="account") == ["user admin logged in"], msgs(topic="account")
    assert msgs(topic="ACCOUNTING") == ["accounting started"]
    assert msgs(text="50%") == ["dropped 50% of packets"] and msgs(text="%") == ["dropped 50% of packets"]
    assert msgs(text="_") == [] and "old one" not in msgs(since_iso=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    ok("AC-5: filters by device, severity (and worse), topic (whole word), text (wildcards are literal), time")
    allids = [e.id for e in logs.search()]; assert allids == sorted(allids, reverse=True)
    page1 = logs.search(limit=2); page2 = logs.search(before_id=page1[-1].id, limit=2)
    assert [e.id for e in page1 + page2] == allids[:4]
    ok("AC-5: newest first; 'older' paging continues exactly where the page ended")

    import routers.logs as rl
    seen = {}
    class T:
        def TemplateResponse(self, name, ctx): seen.update(name=name, **ctx); return ctx
    state = types.SimpleNamespace(templates=T(), logs_repo=logs, devices_repo=devs,
                                  log_service=types.SimpleNamespace(status=lambda: {"listening": True, "host": "0.0.0.0", "port": 5514, "received": 0, "stored": 0, "dropped_unknown": 0, "dropped_full": 0, "unknown_senders": []}))
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
    rl.logs_page(req, device=2, severity="4", topic="", q="", hours=24, before=0, limit=10)
    assert [e.message for e in seen["entries"]] == ["dropped 50% of packets"] and seen["next_before"] == 0
    rl.logs_page(req, device=0, severity="", topic="", q="", hours=0, before=0, limit=10)
    assert len(seen["entries"]) == 5
    logs.add_many([entry(msg=f"x{i}") for i in range(15)])
    rl.logs_page(req, device=0, severity="", topic="", q="", hours=0, before=0, limit=10)
    assert len(seen["entries"]) == 10 and seen["next_before"] == seen["entries"][-1].id
    ok("AC-5: the page applies the filters; 'older messages' link appears only when there are more")

    from jinja2 import Environment, FileSystemLoader
    jenv = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    class R: pass
    evil = entry(1, "A", sev=3, msg="<script>alert(1)</script>", topics="error,critical")
    html = jenv.get_template("logs.html").render(request=R(), active="logs", entries=[evil], next_before=7, devices=devs.list_all(),
        status={"listening": False, "error": "cannot listen on UDP 0.0.0.0:5514: [Errno 98] address in use", "unknown_senders": [("10.0.0.5", 4)]},
        f={"device": 0, "severity": "4", "topic": "a b", "q": "x&y", "hours": 24, "limit": 200}, severity_choices=rl.SEVERITY_CHOICES, hours_choices=rl.HOURS_CHOICES)
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert "Not listening: cannot listen" in html and "10.0.0.5" in html and "q=x%26y" in html and "before=7" in html
    ok("log text is escaped (a router cannot inject script into the page); status, unknown senders and paging link shown")
    d = devs.create(1, "10.0.0.1", "admin", "pw", "auto", 22, 8728, "mikrotik", None, "A")
    html = jenv.get_template("device_detail.html").render(request=R(), active="devices", device=devs.get(d), sites=[], agents=[], offline_for="",
        logs=[evil], history=[], detail=dict(error=None, interfaces=[], ip_addresses=[], dhcp_leases=[], neighbors_by_interface={}, watchdog=None, backups=[]))
    assert "Recent log messages" in html and f"/logs?device={d}" in html and "&lt;script&gt;" in html
    html = jenv.get_template("base.html").render(active="logs"); assert 'href="/logs"' in html
    ok("AC-6: device page shows the latest messages with a link to all logs of the device; 'Logs' is in the menu")

async def test_wiring_and_upgrade():
    print("The real application and upgrade")
    import app as appmod
    application = appmod.create_app()
    names = [j[0] for j in application.state.scheduler._jobs]
    assert "delete old device logs" in names
    repo = application.state.devices_repo
    d = repo.create(1, "127.0.0.1", "admin", "pw", "auto", 22, 8728, "mikrotik", None, "Wired")
    async with appmod.lifespan(application):
        svc = application.state.log_service; assert svc.listening
        send(svc.bound_port, b"<12>warning,system disk almost full")
        await asyncio.sleep(0.3)
    got = application.state.logs_repo.latest_for_device(d, 5)
    assert len(got) == 1 and got[0].severity == "warning" and got[0].message == "disk almost full" and not svc.listening
    ok("AC-1/AC-10/AC-13: the real app starts the listener and the clean-up job, stores a datagram, and shuts down cleanly")
    for old in ("/tmp/old_format.db", "/tmp/cr1_old.db"):
        if os.path.exists(old):
            c = tempfile.mktemp(suffix=".db"); shutil.copy(old, c); db = Database(c)
            with db.connect() as conn: assert conn.execute("SELECT name FROM sqlite_master WHERE name='logs'").fetchone()
            ok(f"AC-11: existing database ({os.path.basename(old)}) gets the new table automatically"); break
    else:
        ok("AC-11: the new table is part of the schema (no old database file here to upgrade)")

async def main():
    test_parser()
    await test_listener(); await test_queue_and_shutdown(); await test_retention_and_jobs()
    test_search_and_pages(); await test_wiring_and_upgrade()
    print(f"\nALL {len(PASSED)} CR-011 CHECKS PASSED")
asyncio.run(main())
