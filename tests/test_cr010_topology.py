"""
CR-010 - manual topology links with port traffic: acceptance tests.

Run from the project folder:   python tests/test_cr010_topology.py

No router is needed. `node` is used to run the real static/topology.js against a small fake page
(skipped when node is not installed).
"""
import asyncio, json, os, shutil, subprocess, sys, tempfile, types
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT); os.chdir(ROOT)
os.environ.update(MIKRAPP_DB=tempfile.mktemp(suffix=".db"), MIKRAPP_BACKUP_DIR=tempfile.mkdtemp(),
                  MIKRAPP_CHECK_LOG="off", MIKRAPP_SCHEDULER="off", MIKRAPP_SYSLOG="off")

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
        def __init__(self, *a, **k): self.routes = []
        def get(self, path="", **k):
            def deco(f): self.routes.append(("GET", path)); return f
            return deco
        def post(self, path="", **k):
            def deco(f): self.routes.append(("POST", path)); return f
            return deco
    class _App:
        def __init__(self, *a, **k): self.state = types.SimpleNamespace()
        def add_middleware(self, *a, **k): pass
        def mount(self, *a, **k): pass
        def include_router(self, *a, **k): pass
    _stub("fastapi", APIRouter=_Router, Request=object, Form=lambda *a, **k: None, FastAPI=_App)
    class _Resp:
        def __init__(self, content=None, status_code=200, *a, **k): self.content, self.status_code = content, status_code
    _stub("fastapi.responses", JSONResponse=_Resp, RedirectResponse=_Resp, FileResponse=_Resp)
    _stub("fastapi.staticfiles", StaticFiles=lambda **k: None)
    _stub("fastapi.templating", Jinja2Templates=lambda **k: None)
    _stub("starlette"); _stub("starlette.middleware")
    _stub("starlette.middleware.sessions", SessionMiddleware=object)
    _stub("starlette.middleware.base", BaseHTTPMiddleware=object)

import config
from db.connection import Database
from db.devices_repo import DeviceRepository
from db.sites_repo import SiteRepository
from db.topology_repo import TopologyRepository
import mikrotik.client as mc
import services.device_service as ds
import services.port_traffic_service as pts
from services.port_traffic_service import PortTrafficService
from services.topology_view import build_view, auto_positions, format_rate, NODE_W, NODE_H
from models import PortCounter
import routers.topology as rt

PASSED = []
def ok(name): PASSED.append(name); print(f"  PASS  {name}")
def new_env():
    db = Database(tempfile.mktemp(suffix=".db"))
    return db, DeviceRepository(db), TopologyRepository(db)
def mk(repo, ip, name="", agent=None, kind="mikrotik", pw="pw", status=None):
    d = repo.create(1, ip, "admin" if pw else "", pw, "auto", 22, 8728, kind, agent, name)
    if status == "online": repo.set_status(d, "online")
    if status == "offline": repo.set_status(d, "offline", "x")
    return d
def payload(resp):
    if hasattr(resp, "content") and not hasattr(resp, "body"): return resp.content
    return json.loads(resp.body)
def msg(resp):
    return unquote(location(resp) or "")
def location(resp):
    return getattr(resp, "content", None) if not hasattr(resp, "headers") else resp.headers["location"]
T0 = datetime(2026, 11, 30, 12, 0, 0, tzinfo=timezone.utc)
def iso(sec): return (T0 + timedelta(seconds=sec)).isoformat(timespec="milliseconds")

def fake_request(devs, topo, db, body=None):
    seen = {}
    class Templates:
        def TemplateResponse(self, name, ctx): seen.update(name=name, **ctx); return ctx
    state = types.SimpleNamespace(devices_repo=devs, topo_repo=topo, sites_repo=SiteRepository(db), templates=Templates())
    async def _json(): return body
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state), json=_json), seen

# ------------------------------------------------------------------ links (AC-1..5)
def test_links():
    print("Links as objects")
    db, devs, topo = new_env(); req, _ = fake_request(devs, topo, db)
    a, b, c = mk(devs, "10.0.0.1", "A"), mk(devs, "10.0.0.2", "B"), mk(devs, "10.0.0.3", "C")
    r = rt.add_link(req, a, " eth3 ", b, "eth1"); assert location(r) == "/topology"
    (l,) = topo.list_links(); assert (l.a_device_id, l.a_port, l.b_device_id, l.b_port) == (a, "eth3", b, "eth1")
    ok("AC-2: a link A.eth3 -> B.eth1 is saved (port names trimmed) and survives")
    r = rt.add_link(req, a, "", b, ""); assert len(topo.list_links()) == 2
    r = rt.add_link(req, a, "eth9", b, "eth8")
    view, _ = rt._view(req.app.state, 0)
    slots = sorted((x["slot"], x["slots"]) for x in view["links"])
    assert slots == [(0, 3), (1, 3), (2, 3)], slots
    ok("AC-4: several links between the same two devices are allowed and get separate slots (drawn side by side)")
    r = rt.add_link(req, a, "eth1", a, "eth2")
    assert "cannot be linked to itself" in msg(r) and len(topo.list_links()) == 3
    r = rt.add_link(req, 0, "", b, ""); assert "Choose both" in msg(r)
    r = rt.add_link(req, a, "", 9999, ""); assert "not found" in msg(r) and len(topo.list_links()) == 3
    ok("AC-5: a link to itself (and a missing or unknown device) is refused with a clear message")
    rt.add_link(req, a, "x" * 200, c, ""); assert len(topo.list_links()[-1].a_port) == 64
    first = topo.list_links()[0]
    rt.edit_link(first.id, req, c, "eth5", b, "eth2")
    e = topo.get_link(first.id); assert (e.a_device_id, e.a_port, e.b_device_id, e.b_port) == (c, "eth5", b, "eth2")
    assert "not found" in msg(rt.edit_link(99999, req, a, "", b, ""))
    n = len(topo.list_links()); rt.delete_link(first.id, req); assert len(topo.list_links()) == n - 1
    ok("AC-3: links can be edited and deleted")
    topo.save_positions({b: (10, 10)}); topo.upsert_counter(PortCounter(a, "eth3", 1, 2, iso(0), None, None, True))
    devs.delete(a)
    assert all(l.a_device_id != a and l.b_device_id != a for l in topo.list_links())
    assert topo.get_counter(a, "eth3") is None and b in topo.positions()
    ok("AC-3: deleting a device deletes its links and its readings; other devices keep their positions")

# ------------------------------------------------------------------ page (AC-1)
def test_page():
    print("The page")
    db, devs, topo = new_env(); req, seen = fake_request(devs, topo, db)
    a = mk(devs, "10.0.0.1", "A", status="online"); b = mk(devs, "10.0.0.2", "B", status="online")
    c = mk(devs, "10.0.0.9", "Printer", kind="generic", pw="", agent=a, status="online")
    view, _ = rt._view(req.app.state, 0)
    assert view["links"] == [] and [(x["from"], x["to"]) for x in view["agent_lines"]] == [(a, c)]
    names = [(m, p) for m, p in getattr(rt.router, "routes", [])]
    assert not any("scan" in p for _, p in names)
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    class R: pass
    html = env.get_template("topology.html").render(request=R(), active="topology", topo=view, devices=devs.list_all(),
                                                    sites=[], site_filter=0, error="")
    assert "scan" not in html.lower() and "Refresh" not in html and "No links yet" in html
    ok("AC-1: no scan button or route, no guessed lines; with no links only your devices (and dotted agent lines) are shown")
    rt.add_link(req, a, "eth3<img src=x onerror=alert(1)>", b, "")
    devs.update_connection(b, 1, "10.0.0.2", "admin", "pw", "auto", 22, 8728, "</script><script>alert(2)</script>", "mikrotik", None)
    view, _ = rt._view(req.app.state, 0)
    html = env.get_template("topology.html").render(request=R(), active="topology", topo=view, devices=devs.list_all(),
                                                    sites=[], site_filter=0, error="<b>x</b>")
    data = html.split('id="topo-data">')[1].split("</script>")[0]
    assert "</script><script>alert" not in html and "<img src=x" not in html and "&lt;b&gt;" in html
    assert json.loads(data)["links"][0]["a"]["port"].startswith("eth3<img")
    ok("device and port names cannot break out of the page (JSON data and HTML are both escaped)")

# ------------------------------------------------------------------ rates (AC-6..9, 14)
def test_rates():
    print("Port traffic")
    db, devs, topo = new_env(); svc = PortTrafficService(topo)
    a = mk(devs, "10.0.0.1", "A", status="online"); b = mk(devs, "10.0.0.2", "B", status="online")
    topo.add_link(a, "eth3", b, "eth1")
    def view(at, **kw): return build_view(devs.list_all(), topo.list_links(), topo.positions(), topo.counters(), 60, now=T0 + timedelta(seconds=at))["links"][0]["traffic"]
    svc.record_sync(a, ["eth3"], {"eth3": {"rx": 1_000_000, "tx": 500_000}}, iso(0))
    assert topo.get_counter(a, "eth3").rx_bps is None
    assert view(1)["text"] == "\u2193 \u2013 \u2191 \u2013"
    ok("AC-7: the first reading has nothing to compare with -> no number")
    svc.record_sync(a, ["eth3"], {"eth3": {"rx": 1_000_000 + 7_500_000, "tx": 500_000 + 1_875_000}}, iso(60))
    c = topo.get_counter(a, "eth3"); assert abs(c.rx_bps - 1_000_000) < 1 and abs(c.tx_bps - 250_000) < 1
    assert view(61)["text"] == "\u2193 1.0 Mbps \u2191 250.0 kbps"
    ok("AC-6: two readings 60 s apart give the average rate: 7.5 MB in 60 s = 1.0 Mbps down, 250 kbps up")
    svc.record_sync(a, ["eth3"], {"eth3": {"rx": 100, "tx": 100}}, iso(120))              # router restarted
    c = topo.get_counter(a, "eth3"); assert c.rx_bps is None and c.rx_bytes == 100
    assert view(121)["text"] == "\u2193 \u2013 \u2191 \u2013"
    svc.record_sync(a, ["eth3"], {"eth3": {"rx": 100 + 600_000, "tx": 100 + 300_000}}, iso(180))
    assert abs(topo.get_counter(a, "eth3").rx_bps - 80_000) < 1
    ok("AC-7: a counter that went down (restart) gives no number for that interval - no spike - and the new value is the baseline")
    before = topo.get_counter(a, "eth3")
    svc.record_sync(a, ["eth3"], {"eth3": {"rx": 10**9, "tx": 10**9}}, iso(180.5))         # a manual check 0.5 s later
    assert topo.get_counter(a, "eth3").read_at == before.read_at
    ok("two readings less than 2 s apart: the second is ignored (too noisy)")
    assert view(180 + 61)["text"] == "\u2193 80.0 kbps \u2191 40.0 kbps"
    assert view(180 + 181)["text"] == "\u2193 \u2013 \u2191 \u2013"
    ok("AC-8: a reading older than three intervals (3 x 60 s) is no longer shown")
    devs.set_status(a, "offline", "x"); assert view(181)["text"] == "\u2193 \u2013 \u2191 \u2013"; devs.set_status(a, "online")
    ok("AC-8: end A offline -> no number, not an old one")
    svc.record_sync(a, ["eth3"], {}, iso(240))
    assert view(241)["text"] == "port not found"
    ok("AC-14: a port the router does not have -> 'port not found'")
    svc.record_sync(a, ["eth3"], {"eth3": {"rx": 5, "tx": 5}}, iso(300)); svc.record_sync(a, ["eth3"], {"eth3": {"rx": 5 + 60, "tx": 5 + 120}}, iso(360))
    assert topo.get_counter(a, "eth3").found and abs(topo.get_counter(a, "eth3").tx_bps - 16) < 0.1
    ok("a port that appears later is picked up again")
    g = mk(devs, "10.0.0.7", "Ping", kind="generic", pw="")
    topo.add_link(g, "eth1", b, ""); topo.add_link(a, "", b, "")
    links = build_view(devs.list_all(), topo.list_links(), {}, topo.counters(), 60, now=T0)["links"]
    avail = {(l["a"]["id"], l["a"]["port"]): l["traffic"]["available"] for l in links}
    assert avail[(g, "eth1")] is False and avail[(a, "")] is False and avail[(a, "eth3")] is True
    assert all(l["traffic"]["text"] == "" for l in links if not l["traffic"]["available"])
    ok("AC-9: end A a ping-only host, or no port set -> the line is drawn without numbers")
    for bps, text in ((0, "0 bps"), (999, "999 bps"), (1500, "1.5 kbps"), (12_300_000, "12.3 Mbps"), (2_500_000_000, "2.50 Gbps")):
        assert format_rate(bps) == text, (bps, format_rate(bps))
    ok("rates are written in bps, kbps, Mbps or Gbps")

# ------------------------------------------------------------------ state (AC-10)
def test_state():
    print("Line state")
    db, devs, topo = new_env()
    a, b, c = mk(devs, "10.0.0.1", "A", status="online"), mk(devs, "10.0.0.2", "B", status="online"), mk(devs, "10.0.0.3", "C", status="offline")
    topo.add_link(a, "", b, ""); topo.add_link(a, "", c, "")
    links = {(l["a"]["id"], l["b"]["id"]): l["state"] for l in build_view(devs.list_all(), topo.list_links(), {}, {}, 60)["links"]}
    assert links == {(a, b): "ok", (a, c): "down"}
    devs.set_status(a, "offline", "x")
    assert {l["state"] for l in build_view(devs.list_all(), topo.list_links(), {}, {}, 60)["links"]} == {"down"}
    ok("AC-10: a link is red/dashed ('down') as soon as one of its ends is offline")

# ------------------------------------------------------------------ positions (AC-12)
async def test_positions():
    print("Positions")
    db, devs, topo = new_env(); req, _ = fake_request(devs, topo, db)
    ids = [mk(devs, f"10.0.0.{i}", f"D{i}") for i in range(1, 8)]
    rt._view(req.app.state, 0)
    saved = topo.positions(); assert set(saved) == set(ids)
    boxes = list(saved.values())
    assert all(abs(x1 - x2) >= NODE_W or abs(y1 - y2) >= NODE_H for i, (x1, y1) in enumerate(boxes) for (x2, y2) in boxes[i + 1:])
    ok("every device gets its own free spot (no two devices on top of each other), saved at first display")
    resp = await rt.api_position(fake_request(devs, topo, db, {"device_id": ids[0], "x": 777, "y": 333})[0])
    assert topo.positions()[ids[0]] == (777, 333)
    await rt.api_position(fake_request(devs, topo, db, {"device_id": ids[1], "x": 99999, "y": -5})[0])
    assert topo.positions()[ids[1]] == (5000, 0)
    assert payload(await rt.api_position(fake_request(devs, topo, db, {"device_id": 4242, "x": 1, "y": 1})[0])).get("error")
    assert payload(await rt.api_position(fake_request(devs, topo, db, {"nope": 1})[0])).get("error")
    ok("a dragged position is saved (clamped to the canvas); bad requests and unknown devices are refused")
    before = dict(topo.positions()); new = mk(devs, "10.0.0.50", "New")
    rt._view(req.app.state, 0)
    after = topo.positions()
    assert all(after[i] == before[i] for i in before) and new in after and after[new] not in before.values()
    devs.delete(ids[3]); rt._view(req.app.state, 0)
    assert all(topo.positions()[i] == before[i] for i in before if i != ids[3])
    ok("AC-12: adding or removing devices never moves the others")
    rt.api_reset_layout(req); assert topo.positions() == {}
    rt._view(req.app.state, 0); assert len(topo.positions()) == len(devs.list_all())
    ok("AC-12: 'Reset layout' clears the positions and the automatic arrangement is assigned again")
    assert auto_positions(devs.list_all(), {}) == auto_positions(devs.list_all(), {})

# ------------------------------------------------------------------ reading counters during checks (AC-6, 13, 14, 15)
async def test_checks():
    print("Counters read during checks")
    db, devs, topo = new_env(); clock = [iso(0)]
    pts.utc_now_iso = lambda: clock[0]
    svc = ds.DeviceService(devs, topology_repo=topo, traffic=PortTrafficService(topo))
    a1 = mk(devs, "10.1.0.1", "A1"); a2 = mk(devs, "10.2.0.1", "A2", a1); e = mk(devs, "10.2.0.50", "E", a2)
    other = mk(devs, "10.9.0.1", "Other"); lonely = mk(devs, "10.9.0.2", "Lonely")
    topo.add_link(e, "ether2", other, "ether1"); topo.add_link(lonely, "", other, "")
    calls, counters = [], {"rx": 1000, "tx": 2000}
    class FakeClient:
        def __init__(self, device, via=None): self.device, self.via = device, list(via or [])
        async def ping(self, addr, count=3, attempts=None): return {"sent": 3, "received": 3, "avg_ms": 2.0}
        async def is_reachable(self, attempts=None): return True
        async def collect_metrics(self, extra_commands=False, attempts=None, ports=None):
            calls.append((self.device.id, ports, [v.id for v in self.via]))
            data = {"identity": "x", "cpu_usage": 1.0, "ram_usage": 1.0, "uptime": "1d"}
            if ports: data["port_counters"] = {p: dict(counters) for p in ports if p != "missing"}
            return data
    ds.MikrotikClient = FakeClient
    await svc.check_device(devs.get(e)); await svc.check_device(devs.get(other)); await svc.check_device(devs.get(lonely))
    by = {c[0]: c for c in calls}
    assert by[e][1] == ["ether2"] and by[e][2] == [a1, a2], by[e]
    ok("AC-13: counters of a device behind two agents are read through the chain [A1, A2]")
    assert by[other][1] is None and by[lonely][1] is None, "devices that are not end A of a port must not have counters read"
    ok("AC-15: a device that is not end A of a link (or has no port set) is checked without reading counters")
    assert topo.get_counter(e, "ether2").rx_bytes == 1000
    counters.update(rx=1000 + 3_750_000, tx=2000 + 750_000); clock[0] = iso(60)
    await svc.check_device(devs.get(e))
    c = topo.get_counter(e, "ether2"); assert abs(c.rx_bps - 500_000) < 1 and abs(c.tx_bps - 100_000) < 1
    assert "data" not in str(devs.get(e).last_error) and devs.get(e).status == "online"
    ok("AC-6: two real checks 60 s apart store the rate (3.75 MB -> 500 kbps down, 750 kB -> 100 kbps up)")
    topo.add_link(e, "missing", other, ""); await svc.check_device(devs.get(e))
    assert topo.get_counter(e, "missing").found is False and devs.get(e).status == "online"
    ok("AC-14: a port the router lacks is recorded as 'not found' and the check itself still succeeds")

    class Down(FakeClient):
        async def collect_metrics(self, extra_commands=False, attempts=None, ports=None): raise OSError("timed out")
        async def is_reachable(self, attempts=None): return False
    ds.MikrotikClient = Down
    clock[0] = iso(120); before = topo.get_counter(e, "ether2")
    await svc.check_device(devs.get(e))
    assert topo.get_counter(e, "ether2").read_at == before.read_at
    ok("a failed check changes no reading (it just ages)")
    topo.add_link(other, "ether7", lonely, ""); ds.MikrotikClient = FakeClient
    clock[0] = iso(200); await svc.check_device(devs.get(other))
    assert any(c[0] == other and c[1] == ["ether7"] and c[2] == [] for c in calls)
    ok("a directly reached device is read directly")

def test_parsing():
    rows = [{"name": "ether1", "rx-byte": "123456", "tx-byte": "654321", "running": "true"},
            {"name": "ether2", "rx-byte": "1"}, {"name": "bridge", "rx-byte": "5", "tx-byte": "6"}, {"name": "ether3", "rx-byte": "9", "tx-byte": "9"}]
    got = mc._counters_from_rows(rows, ["ether1", "ether2", "ether9", "bridge"])
    assert got == {"ether1": {"rx": 123456, "tx": 654321}, "bridge": {"rx": 5, "tx": 6}}, got
    terse = "Flags: D - DYNAMIC; R - RUNNING\n 0 R name=ether1 rx-byte=100 tx-byte=200 rx-packet=1\n 1   name=ether2 rx-byte=7 tx-byte=8\n"
    assert mc._counters_from_rows(mc._parse_terse(terse), ["ether1", "ether2"]) == {"ether1": {"rx": 100, "tx": 200}, "ether2": {"rx": 7, "tx": 8}}
    ok("the router's interface list (API rows and SSH terse text) is read for exactly the requested ports")

# ------------------------------------------------------------------ the page's JavaScript (AC-11)
def test_javascript():
    print("JavaScript")
    if not shutil.which("node"): print("  SKIP  JavaScript checks (node not installed)"); return
    js = open("static/topology.js", encoding="utf-8").read()
    script = r"""
const vm = require('vm'); const src = process.argv[1]; const assert = require('assert');
const state = (over = {}) => ({
  config: { interval: 60, site: 0 },
  nodes: [ {id:1,name:'A',ip:'1',status:'online',kind:'mikrotik',info:'CPU 1%',x:100,y:100}, {id:2,name:'B',ip:'2',status:'online',kind:'mikrotik',info:'CPU 2%',x:500,y:100} ],
  links: [ {id:7,a:{id:1,name:'A',port:'eth3'},b:{id:2,name:'B',port:'eth1'},state:'ok',slot:0,slots:1,traffic:{available:true,text:'\u2193 1.0 Mbps \u2191 2.0 Mbps'}} ],
  agent_lines: [ {from:1,to:2} ], ...over });
function el(tag) { const e = { tag, children: [], attrs: {}, style: {}, dataset: {}, handlers: {}, className: '', textContent: '' };
  e.append = (...c) => c.forEach(x => e.children.push(x)); e.appendChild = (c) => { e.children.push(c); return c; };
  e.setAttribute = (k, v) => { e.attrs[k] = String(v); }; e.getAttribute = (k) => e.attrs[k];
  e.addEventListener = (t, f) => { (e.handlers[t] = e.handlers[t] || []).push(f); }; e.closest = () => null; return e; }
function page(initial) {
  const ids = { 'topo-data': el('script'), topoStage: el('div'), topoCanvas: el('div'), topoSvg: el('svg'), topoNodes: el('div'),
                'topo-fit-btn': el('button'), 'topo-reset-layout-btn': el('button') };
  ids['topo-data'].textContent = JSON.stringify(initial);
  const win = {}; const intervals = []; const posts = []; let reloads = 0; let nextState = initial;
  const ctx = { console, JSON, Math, Object, parseInt, encodeURIComponent, confirm: () => true,
    document: { getElementById: (i) => ids[i], createElement: el, createElementNS: (ns, t) => el(t) },
    window: { addEventListener: (t, f) => { (win[t] = win[t] || []).push(f); } },
    setInterval: (f, ms) => { intervals.push({ f, ms }); },
    location: { reload: () => { reloads++; } },
    fetch: async (url, opts) => { if (opts && opts.method === 'POST') { posts.push({ url, body: JSON.parse(opts.body || 'null') }); return { ok: true }; }
                                   return { ok: true, json: async () => nextState }; } };
  vm.createContext(ctx); vm.runInContext(src, ctx);
  return { ids, win, intervals, posts, ctx, reloads: () => reloads, serve: (s) => { nextState = s; } };
}
(async () => {
  // --- pure helpers
  const p = page(state());                                   // also proves the script loads and renders
  const diff = (a, b) => vm.runInContext(`topoDiff(${JSON.stringify(a)}, ${JSON.stringify(b)})`, p.ctx);
  const s1 = state(); let s2 = state(); s2.links[0].traffic.text = '\u2193 5.0 Mbps \u2191 6.0 Mbps'; s2.nodes[1].status = 'offline'; s2.links[0].state = 'down';
  let d = diff(s1, s2);
  assert.strictEqual(d.reload, false); assert.strictEqual(JSON.stringify(d.nodes.map(n => n.id)), '[2]'); assert.strictEqual(d.links[0].state, 'down');
  assert.strictEqual(diff(s1, state()).nodes.length + diff(s1, state()).links.length, 0, 'identical state = nothing to do');
  let s3 = state(); s3.nodes[0].x = 999;                      assert.strictEqual(diff(s1, s3).reload, false, 'positions are not a reload reason');
  s3 = state(); s3.nodes.push({id:3,name:'C',ip:'3',status:'online',kind:'mikrotik',info:'',x:0,y:0}); assert.strictEqual(diff(s1, s3).reload, true, 'a new device needs a reload');
  s3 = state(); s3.links[0].b.port = 'eth9';                  assert.strictEqual(diff(s1, s3).reload, true, 'an edited link needs a reload');
  s3 = state(); s3.links = [];                                assert.strictEqual(diff(s1, s3).reload, true, 'a deleted link needs a reload');
  const line = (slot, slots) => vm.runInContext(`topoLinkLine({x:0,y:0},{x:400,y:0},${slot},${slots})`, p.ctx);
  assert.strictEqual(line(0, 1).y1, 42); assert.strictEqual(line(0, 2).y1 + line(1, 2).y1, 84); assert.notStrictEqual(line(0, 2).y1, line(1, 2).y1);
  // --- the live page: draw, drag, update in place
  const live = page(state()); const nodesLayer = live.ids.topoNodes, svg = live.ids.topoSvg;
  assert.strictEqual(nodesLayer.children.length, 2); assert.strictEqual(svg.children.length, 2);    // 1 agent line + 1 link
  const traffic = () => svg.children[1].children[3];
  assert.strictEqual(traffic().textContent, '\u2193 1.0 Mbps \u2191 2.0 Mbps');
  assert.strictEqual(live.intervals.length, 1); assert.strictEqual(live.intervals[0].ms, 60000);
  const nodeA = nodesLayer.children[0];
  nodeA.handlers.mousedown[0]({ preventDefault(){}, stopPropagation(){}, clientX: 200, clientY: 200 });
  live.win.mousemove.forEach(f => f({ clientX: 260, clientY: 230 }));
  assert.strictEqual(nodeA.style.left, '160px'); assert.strictEqual(nodeA.style.top, '130px');
  await Promise.all(live.win.mouseup.map(f => f()));
  assert.strictEqual(JSON.stringify(live.posts), JSON.stringify([{ url: '/topology/api/position', body: { device_id: 1, x: 160, y: 130 } }]));
  live.ids.topoStage.handlers.wheel[0]({ preventDefault(){}, deltaY: -200 });                      // zoom in
  const view = live.ids.topoCanvas.style.transform; assert.ok(view.includes('scale(1.2)'), view);
  // a check round finishes: new numbers and a device goes offline; the server still says A is at (100,100)
  const next = state(); next.links[0].traffic.text = '\u2193 9.9 Mbps \u2191 8.8 Mbps'; next.links[0].state = 'down'; next.nodes[1].status = 'offline'; next.nodes[1].info = 'offline';
  live.serve(next); await live.intervals[0].f();
  assert.strictEqual(live.reloads(), 0, 'no reload');
  assert.strictEqual(traffic().textContent, '\u2193 9.9 Mbps \u2191 8.8 Mbps');
  assert.strictEqual(svg.children[1].attrs['class'], 'topo-link down');
  assert.strictEqual(nodesLayer.children[1].className, 'node-card offline');
  assert.strictEqual(nodeA.style.left, '160px'); assert.strictEqual(nodeA.style.top, '130px');         // dragged position stays
  assert.strictEqual(live.ids.topoCanvas.style.transform, view);                                       // zoom and pan stay
  live.serve(state({ nodes: next.nodes.concat([{id:3,name:'C',ip:'3',status:'online',kind:'mikrotik',info:'',x:0,y:0}]) }));
  await live.intervals[0].f(); assert.strictEqual(live.reloads(), 1, 'a new device reloads the page once');
  console.log('js ok');
})().catch(e => { console.error('JS FAIL:', e.stack || e.message); process.exit(1); });
"""
    out = subprocess.run(["node", "-e", script, js], capture_output=True, text=True)
    assert out.returncode == 0 and "js ok" in out.stdout, out.stderr + out.stdout
    ok("AC-11: the real topology.js draws the diagram; after a check round numbers and colours change IN PLACE - no reload, dragged positions and zoom stay; only a new/removed device or link reloads")
    ok("dragging saves the position; several links between one pair are drawn side by side")

async def test_upgrade_and_wiring():
    print("Upgrade and the real application")
    for old in ("/tmp/old_format.db", "/tmp/cr1_old.db"):
        if os.path.exists(old):
            c = tempfile.mktemp(suffix=".db"); shutil.copy(old, c); db = Database(c)
            with db.connect() as conn:
                names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"topology_links", "topology_positions", "port_counters"} <= names
            ok(f"AC-16: existing database ({os.path.basename(old)}) gets the three new tables automatically"); break
    else:
        db = Database(tempfile.mktemp(suffix=".db"))
        with db.connect() as conn:
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"topology_links", "topology_positions", "port_counters"} <= names and "topology_cache" not in names
        ok("AC-16: the new tables are part of the schema (no old database file here to upgrade)")
    import app as appmod
    application = appmod.create_app(); st = application.state
    assert not hasattr(st, "topology_service") and st.device_service.topology_repo is st.topo_repo and st.device_service.traffic is not None
    ok("the real application has no scan service any more and wires port traffic into every check")

async def main():
    test_links(); test_page(); test_rates(); test_state(); await test_positions(); await test_checks(); test_parsing()
    test_javascript(); await test_upgrade_and_wiring()
    print(f"\nALL {len(PASSED)} CR-010 CHECKS PASSED")
asyncio.run(main())
