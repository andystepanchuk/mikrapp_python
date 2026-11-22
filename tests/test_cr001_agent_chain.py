"""
CR-001 - second-level agent chain: acceptance tests.

Run from the project folder:   python tests/test_cr001_agent_chain.py

No router or network is needed: the SSH layer and the RouterOS API server are faked.
Each section names the acceptance criterion (AC-n) from the change request it checks.
"""
import asyncio, os, sys, tempfile, time, types
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import asyncssh
except ImportError:                      # allow running without the real package installed
    asyncssh = types.ModuleType("asyncssh")
    class _Error(Exception): pass
    class _ChannelOpenError(_Error):
        def __init__(self, code, reason, lang="en-US"):
            super().__init__(reason); self.code, self.reason = code, reason
    asyncssh.Error, asyncssh.ChannelOpenError = _Error, _ChannelOpenError
    async def _connect(*a, **k): raise _Error("stub")
    asyncssh.connect = _connect
    sys.modules["asyncssh"] = asyncssh

import config
from db.connection import Database
from db.devices_repo import DeviceRepository
import mikrotik.tunnel as tunnel
import mikrotik.client as mc
import services.device_service as ds
import services.client_factory as cf
from mikrotik.tunnel import AgentChainError, AgentHopError, AgentForwardingError, forwarding_message
from mikrotik.routeros_api import RouterOsApiClient, _encode_word
from services.client_factory import ClientFactory

PASSED = []
def ok(name): PASSED.append(name); print(f"  PASS  {name}")
def new_repo():
    return DeviceRepository(Database(tempfile.mktemp(suffix=".db")))
def mk(repo, ip, name="", agent=None, kind="mikrotik", pw="pw"):
    return repo.create(1, ip, "admin" if pw else "", pw, "auto", 22, 8728, kind, agent, name)
def ids(devs): return [d.id for d in devs]

# =========================================================== chain resolution
async def test_resolution():
    print("Chain resolution")
    config.MAX_AGENT_DEPTH = 2
    r = new_repo(); f = ClientFactory(r)
    a1 = mk(r, "10.0.0.1", "A1"); a2 = mk(r, "10.1.0.1", "A2", a1)
    t = mk(r, "10.1.0.50", "T", a2, "generic", ""); direct = mk(r, "10.9.9.9", "D")
    assert await f.resolve_chain(r.get(direct)) == []
    ok("direct device -> empty chain")
    assert ids(await f.resolve_chain(r.get(a2))) == [a1]
    ok("AC-6 / AC-7: level-1 device -> chain [A1]")
    assert ids(await f.resolve_chain(r.get(t))) == [a1, a2]
    ok("two levels -> chain [A1, A2], outermost first")
    a3 = mk(r, "10.2.0.1", "A3", a2); deep = mk(r, "10.2.0.9", "X", a3)
    try: await f.resolve_chain(r.get(deep)); raise SystemExit("deep chain accepted")
    except AgentChainError as e: assert "too deep (max 2)" in str(e)
    ok("AC-5: three agents -> 'agent chain too deep (max 2)'")
    la = mk(r, "10.3.0.1", "LA"); lb = mk(r, "10.3.0.2", "LB", la)
    r.update_connection(la, 1, "10.3.0.1", "admin", "pw", "auto", 22, 8728, "LA", "mikrotik", lb)  # loop: LA<->LB
    lt = mk(r, "10.3.0.9", "LT", la)
    try: await asyncio.wait_for(f.resolve_chain(r.get(lt)), 2); raise SystemExit("loop accepted")
    except AgentChainError as e: assert "too deep" in str(e)
    ok("AC-5: a loop (A via B, B via A) stops at the depth limit, no hang")
    config.MAX_AGENT_DEPTH = 3
    assert len(await f.resolve_chain(r.get(deep))) == 3
    config.MAX_AGENT_DEPTH = 2
    ok("depth is configurable (MIKRAPP_MAX_AGENT_DEPTH)")
    ghost = mk(r, "10.4.0.1", "G", 9999)
    try: await f.resolve_chain(r.get(ghost)); raise SystemExit("missing agent accepted")
    except AgentChainError as e: assert "no longer exists" in str(e)
    nopw = mk(r, "10.5.0.1", "NoPw", pw=""); nt = mk(r, "10.5.0.2", "N", nopw)
    try: await f.resolve_chain(r.get(nt)); raise SystemExit("agent without credentials accepted")
    except AgentChainError as e: assert "no credentials" in str(e)
    ok("missing agent / agent without credentials -> clear error")

# =========================================================== nested tunnel
async def test_tunnel():
    print("Nested tunnel (open_agent_chain)")
    class FakeSsh:
        log, fail = [], {}
        def __init__(self, host, username, password, port=22, timeout=8.0, tunnel=None):
            self.host, self.tunnel, self.connection = host, tunnel, f"conn:{host}"
        async def connect(self):
            FakeSsh.log.append(("connect", self.host, self.tunnel))
            if FakeSsh.fail.get(self.host): raise FakeSsh.fail[self.host]
        async def close(self): FakeSsh.log.append(("close", self.host))
    tunnel.MikrotikSshClient = FakeSsh
    r = new_repo(); a1 = r.get(mk(r, "10.0.0.1", "A1")); a2 = r.get(mk(r, "10.1.0.1", "A2", a1.id))

    async with tunnel.open_agent_chain([a1, a2]) as conn:
        assert conn == "conn:10.1.0.1", "must yield the INNERMOST connection"
    assert FakeSsh.log == [("connect", "10.0.0.1", None), ("connect", "10.1.0.1", "conn:10.0.0.1"),
                           ("close", "10.1.0.1"), ("close", "10.0.0.1")], FakeSsh.log
    ok("A2's SSH runs INSIDE A1's session; closed in reverse order")

    FakeSsh.log.clear(); FakeSsh.fail = {"10.0.0.1": OSError("timed out")}
    try:
        async with tunnel.open_agent_chain([a1, a2]): pass
    except AgentHopError as e: msg = str(e)
    assert "agent A1 unreachable" in msg and not any(x[0] == "connect" and x[1] == "10.1.0.1" for x in FakeSsh.log)
    ok("AC-2: A1 down -> error names A1, A2 is never attempted")

    FakeSsh.log.clear(); FakeSsh.fail = {"10.1.0.1": asyncssh.ChannelOpenError(1, "Administratively prohibited")}
    try:
        async with tunnel.open_agent_chain([a1, a2]): pass
    except AgentForwardingError as e: msg = str(e)
    assert "agent A1 refused to forward" in msg and "forwarding-enabled=local" in msg
    assert ("close", "10.0.0.1") in FakeSsh.log, "A1 session must be cleaned up"
    ok("A1 forwarding disabled -> message names A1 and the fix; session cleaned up")

    FakeSsh.fail = {"10.1.0.1": OSError("no route")}
    try:
        async with tunnel.open_agent_chain([a1, a2]): pass
    except AgentHopError as e: msg = str(e)
    assert "agent A2 unreachable through A1" in msg
    ok("A2 unreachable -> message says 'A2 ... through A1'")
    FakeSsh.fail = {}

# =========================================================== transport vs fake RouterOS
async def read_len(rd):
    f = (await rd.readexactly(1))[0]
    if f & 0x80 == 0: return f
    return ((f & ~0xC0) << 8) + (await rd.readexactly(1))[0]
async def read_sentence(rd):
    w = []
    while True:
        n = await read_len(rd)
        if n == 0: return w
        w.append((await rd.readexactly(n)).decode())
def sentence(*w): return b"".join(_encode_word(x) for x in w) + b"\x00"
async def fake_routeros(rd, wr):
    try:
        while True:
            s = await read_sentence(rd); cmd = s[0]
            args = dict(x[1:].split("=", 1) for x in s[1:] if x.startswith("="))
            if cmd == "/login": wr.write(sentence("!done"))
            elif cmd == "/ping":
                alive = args["address"] != "10.1.0.99"
                wr.write(sentence("!re", "=sent=3", f"=received={3 if alive else 0}", "=avg-rtt=2ms")); wr.write(sentence("!done"))
            elif cmd == "/system/resource/print":
                wr.write(sentence("!re", "=version=7.15", "=board-name=hAP", "=cpu-load=9", "=total-memory=1000", "=free-memory=750", "=uptime=3d")); wr.write(sentence("!done"))
            elif cmd == "/system/identity/print": wr.write(sentence("!re", "=name=deep-rtr")); wr.write(sentence("!done"))
            else: wr.write(sentence("!done"))
            await wr.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError): pass

async def test_transport():
    print("Transport through two agents (fake RouterOS API server)")
    server = await asyncio.start_server(fake_routeros, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    r = new_repo(); a1 = r.get(mk(r, "10.0.0.1", "A1")); a2 = r.get(mk(r, "10.1.0.1", "A2", a1.id))
    target = r.get(mk(r, "10.1.0.50", "T", a2.id))
    seen = {"chains": [], "opens": [], "refuse": None}
    class FakeTunnel:
        async def open_connection(self, host, p):
            seen["opens"].append((host, p))
            if seen["refuse"]: raise seen["refuse"]
            return await asyncio.open_connection("127.0.0.1", port)
    @asynccontextmanager
    async def fake_chain(agents):
        seen["chains"].append([a.display_name for a in agents]); yield FakeTunnel()
    mc.open_agent_chain = fake_chain

    data = await mc.MikrotikClient(target, via=[a1, a2]).collect_metrics()
    assert seen["chains"][-1] == ["A1", "A2"], seen["chains"]
    assert seen["opens"][-1] == ("10.1.0.50", 8728), "innermost agent must be asked for target:api_port"
    assert data["identity"] == "deep-rtr" and data["ram_usage"] == 25.0
    ok("AC-3: metrics read through chain [A1, A2]; target:8728 requested on the innermost session")

    ping = await mc.MikrotikClient(a2, via=[a1]).ping("10.1.0.50")      # pinger = A2, reached through A1
    assert seen["chains"][-1] == ["A1"] and ping["received"] == 3
    dead = await mc.MikrotikClient(a2, via=[a1]).ping("10.1.0.99")
    assert dead["received"] == 0
    ok("AC-1: the ping is run BY A2 (connection to A2 goes through A1 only)")

    seen["refuse"] = asyncssh.ChannelOpenError(1, "Administratively prohibited")
    try: await mc.MikrotikClient(target, via=[a1, a2]).collect_metrics(); raise SystemExit("not refused")
    except AgentForwardingError as e: msg = str(e)
    assert "agent A2 refused to forward" in msg and "A1" not in msg
    ok("AC-4: forwarding disabled on A2 only -> error names A2 (not A1)")
    seen["refuse"] = asyncssh.ChannelOpenError(2, "Connection refused")
    try: await mc.MikrotikClient(target, via=[a1, a2]).collect_metrics(); raise SystemExit("not refused")
    except AgentHopError as e: assert "agent A2 could not connect to 10.1.0.50:8728" in str(e)
    ok("closed API port is NOT misreported as 'forwarding disabled'")
    server.close()

# =========================================================== DeviceService (AC-1..AC-8)
async def test_service():
    print("DeviceService through a chain")
    config.MAX_AGENT_DEPTH = 2; config.AGENT_CONCURRENCY = 4
    r = new_repo()
    a1 = mk(r, "10.0.0.1", "A1"); a2 = mk(r, "10.1.0.1", "A2", a1)
    host = mk(r, "10.1.0.50", "Printer", a2, "generic", ""); dead = mk(r, "10.1.0.99", "OldPC", a2, "generic", "")
    m = mk(r, "10.1.0.7", "M", a2); l1host = mk(r, "10.0.0.50", "L1Host", a1, "generic", "")
    DOWN, NOFWD, calls = set(), set(), []
    active, peak = {}, {}
    class FakeClient:
        def __init__(self, device, via=None): self.device, self.via = device, list(via or [])
        async def ping(self, addr, count=3, attempts=None):
            chain = self.via + [self.device]
            calls.append(("ping", self.device.id, ids(self.via), addr))
            for a in chain: active[a.id] = active.get(a.id, 0) + 1; peak[a.id] = max(peak.get(a.id, 0), active[a.id])
            try:
                await asyncio.sleep(0.2)
                for i, hop in enumerate(chain):
                    if hop.id in DOWN:
                        if i < len(chain) - 1 or self.via: raise AgentHopError(f"agent {hop.display_name} unreachable: timed out")
                        raise OSError("timed out")
                    if i < len(chain) - 1 and hop.id in NOFWD: raise AgentForwardingError(forwarding_message(hop))
                return {"sent": 3, "received": 0 if addr == "10.1.0.99" else 3, "avg_ms": 4.5}
            finally:
                for a in chain: active[a.id] -= 1
        async def collect_metrics(self, extra_commands=False, attempts=None):
            calls.append(("metrics", self.device.id, ids(self.via)))
            if self.via and self.via[-1].id in NOFWD: raise AgentForwardingError(forwarding_message(self.via[-1]))
            return {"identity": f"id{self.device.id}", "cpu_usage": 7.0, "ram_usage": 30.0, "uptime": "1d"}
    ds.MikrotikClient = FakeClient
    svc = ds.DeviceService(r); get = r.get

    await svc.check_device(get(host))
    assert calls[-1] == ("ping", a2, [a1], "10.1.0.50"), calls[-1]
    assert get(host).status == "online" and get(host).latency_ms == 4.5
    ok("AC-1: generic host behind A2 -> pinged by A2 (via A1), online with latency")

    await svc.check_device(get(dead)); assert get(dead).status == "offline" and "no ping reply via agent A2" in get(dead).last_error
    ok("no reply -> offline, reason names A2")

    DOWN.add(a1); await svc.check_device(get(host)); await svc.check_device(get(a2))
    assert get(host).status == "offline" and "agent A1 unreachable" in get(host).last_error
    assert get(a2).status == "offline" and "agent A1 unreachable" in get(a2).last_error
    ok("AC-2: A1 unreachable -> host AND A2 offline, reason names A1"); DOWN.clear()

    calls.clear(); await svc.check_device(get(m))
    assert ("metrics", m, [a1, a2]) in calls and get(m).status == "online" and get(m).identity == f"id{m}"
    ok("AC-3: MikroTik behind A2 -> metrics via chain [A1, A2]")

    NOFWD.add(a2); await svc.check_device(get(m))
    assert get(m).status == "online" and "agent A2 refused to forward" in get(m).last_error
    ok("AC-4: forwarding off on A2 -> online (pings), last error names A2 and the fix"); NOFWD.clear()
    NOFWD.add(a1); await svc.check_device(get(m))
    assert get(m).status == "offline" and "agent A1 refused to forward" in get(m).last_error
    ok("forwarding off on A1 -> offline, last error names A1"); NOFWD.clear()

    calls.clear(); await svc.check_device(get(a2))
    assert calls[0] == ("ping", a1, [], "10.1.0.1") and ("metrics", a2, [a1]) in calls and get(a2).status == "online"
    ok("AC-6: A2 itself is checked through A1 exactly like a level-1 device")

    calls.clear(); await svc.check_device(get(l1host))
    assert calls[-1] == ("ping", a1, [], "10.0.0.50") and get(l1host).status == "online"
    ok("AC-7: level-1 behaviour unchanged (A1 pings, direct)")

    a3 = mk(r, "10.2.0.1", "A3", a2); deep = mk(r, "10.2.0.9", "X", a3, "generic", "")
    await asyncio.wait_for(svc.check_device(get(deep)), 3)
    assert get(deep).status == "offline" and "agent chain too deep (max 2)" in get(deep).last_error
    ok("AC-5: too-deep chain -> offline 'agent chain too deep (max 2)'")

    many = [mk(r, f"10.1.1.{i}", f"h{i}", a2, "generic", "") for i in range(12)]
    peak.clear(); t = time.time(); await svc.check_many([get(i) for i in many]); el = time.time() - t
    assert peak[a1] <= 4 and peak[a2] <= 4, peak
    assert 0.5 < el < 1.3, el
    ok(f"AC-8: 12 hosts behind A2 -> peak {peak[a1]} on A1 and {peak[a2]} on A2 (limit 4), {el:.2f}s")

# =========================================================== repo / UI / topology / backup / commands
async def test_rest():
    print("Candidates, display path, topology, backup, commands")
    config.MAX_AGENT_DEPTH = 2
    r = new_repo()
    a1 = mk(r, "10.0.0.1", "A1"); a2 = mk(r, "10.1.0.1", "A2", a1)
    t = mk(r, "10.1.0.50", "T", a2, "generic", ""); m = mk(r, "10.1.0.7", "M", a2); gen = mk(r, "10.9.0.1", "G", None, "generic", "")
    cands = {d.id for d in r.list_agent_candidates()}
    assert a2 in cands and a1 in cands and m in cands and gen not in cands
    ok("AC-9: any MikroTik with credentials is offered as agent (also one that has an agent); generic hosts are not")
    try:
        import routers.devices as rd
        assert rd._clean_agent(a2, a2) is None and rd._clean_agent(a1, a2) == a1
        ok("AC-9: a device can never be its own agent")
    except ImportError: print("  SKIP  router check (fastapi not installed here)")
    assert r.get(t).agent_path == "A1 \u2192 A2" and r.get(a2).agent_path == "A1" and r.get(a1).agent_path == ""
    assert {d.id: d.agent_path for d in r.list_all()}[t] == "A1 \u2192 A2"
    ok("AC-9: lists/pages show the full path 'A1 \u2192 A2'")

    import services.topology_service as ts, services.backup_service as bs
    from db.topology_repo import TopologyRepository
    from db.backups_repo import BackupRepository
    class FakeClient:
        def __init__(self, d, via=None): self.d, self.via = d, list(via or [])
        async def collect_metrics(self, extra_commands=False, attempts=None): return {"identity": f"id{self.d.id}"}
        async def get_neighbors(self): return []
        async def export_config(self): return "# export via " + ",".join(str(x.id) for x in self.via)
        async def run_commands(self, cmds): return "ran via " + ",".join(str(x.id) for x in self.via)
    cf.MikrotikClient = FakeClient           # the real ClientFactory now builds fakes, but WITH the real chains
    res = await ts.TopologyService(r, TopologyRepository(r.db)).scan()
    pairs = {(l["from"], l["to"], l["label"]) for l in res["links"]}
    assert (a1, a2, "agent") in pairs and (a2, t, "agent") in pairs and (a2, m, "agent") in pairs, pairs
    ok("AC-10: topology draws A1-A2 and A2-T links labelled 'agent'")
    b = await bs.BackupService(r, BackupRepository(r.db)).backup_device(r.get(m))
    assert b["ok"]; assert open(os.path.join(config.BACKUP_DIR, str(m), b["filename"])).read() == f"# export via {a1},{a2}"
    ok("AC-11: backup of M goes through the chain [A1, A2]")
    ds.MikrotikClient = FakeClient
    out = await ds.DeviceService(r).run_commands([r.get(m), r.get(t)], ["/x"])
    assert out[m] == f"ran via {a1},{a2}" and "skipped" in out[t]
    ok("AC-11: command templates run through the chain (generic hosts skipped)")

async def main():
    config.BACKUP_DIR = tempfile.mkdtemp()
    for fn in (test_resolution, test_tunnel, test_transport, test_service, test_rest): await fn()
    print(f"\nALL {len(PASSED)} CR-001 CHECKS PASSED")
asyncio.run(main())
