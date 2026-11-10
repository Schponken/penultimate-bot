"""The dashboard only answers to its own address and never to another website."""
import http.client
import json
import socket

from penultimate.dashboard import Dash, host_ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(port, method="GET", path="/", host=None, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    data = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read().decode())
    c.close()
    return out


def test_host_names():
    assert host_ok("127.0.0.1:8787") and host_ok("localhost:8787") and host_ok("[::1]:8787")
    assert host_ok("penultimate") and host_ok("100.101.102.103") and host_ok("penultimate.tail1234.ts.net")
    assert not host_ok("evil.example:8787") and not host_ok("") and not host_ok("127.0.0.1.evil.example")
    assert host_ok("bot.home", ("bot.home",))


def test_another_sites_name_is_refused_and_pages_cannot_be_framed():
    d = Dash(free_port())
    d.start()
    try:
        status, headers, body = call(d.port)
        assert status == 200 and headers["X-Frame-Options"] == "DENY" and d.token in body
        status, _, body = call(d.port, host="evil.example")              # a hostile domain pointed at this computer
        assert status == 403 and d.token not in body
        assert call(d.port, path="/api/state", host="evil.example")[0] == 403
        ok = {"cmd": "risk_off", "on": False, "token": d.token}
        assert call(d.port, "POST", "/api/cmd", headers={"Origin": "http://evil.example"}, body=ok)[0] == 403
        assert call(d.port, "POST", "/api/cmd", body={**ok, "token": "wrong"})[0] == 403
        # right address, right token, same origin: accepted (the engine is not running in this test)
        status, _, body = call(d.port, "POST", "/api/cmd", headers={"Origin": f"http://127.0.0.1:{d.port}"}, body=ok)
        assert status == 400 and "restarting" in body
    finally:
        d.httpd.shutdown()
        d.httpd.server_close()


def test_saved_keys_are_readable_by_the_owner_only(tmp_path, monkeypatch):
    import os
    import stat
    from penultimate import broker_live
    monkeypatch.setattr(broker_live, "KEYS_FILE", tmp_path / "data" / "keys.json")
    pem = "-----BEGIN EC PRIVATE KEY-----\n" + "A" * 64 + "\n-----END EC PRIVATE KEY-----\n"
    old = os.umask(0)                                    # even with the most permissive default
    try:
        broker_live.save_keys(json.dumps({"name": "organizations/x/apiKeys/y", "privateKey": pem}), "")
    finally:
        os.umask(old)
    mode = stat.S_IMODE(os.stat(tmp_path / "data" / "keys.json").st_mode)
    assert mode == 0o600 and stat.S_IMODE(os.stat(tmp_path / "data").st_mode) == 0o700
    assert broker_live.load_keys()["apiKey"] == "organizations/x/apiKeys/y"


def test_the_updater_only_accepts_a_plain_repository_name(tmp_path):
    from penultimate.updater import Updater
    assert Updater({"update": {"repo": "Owner/repo"}}, tmp_path, tmp_path).enabled
    for bad in ("Owner/repo/../../x", "https://evil.example/x", "Owner repo", "a/b?c=d"):
        assert not Updater({"update": {"repo": bad}}, tmp_path, tmp_path).enabled
