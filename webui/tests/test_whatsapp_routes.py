def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "hunter2hunter2"})
    client.post("/api/login", json={"username": "admin", "password": "hunter2hunter2"})


def test_whatsapp_status_requires_auth(client):
    r = client.get("/api/whatsapp/status")
    assert r.status_code == 401


def test_whatsapp_status_authed(client, monkeypatch):
    _login(client)

    from webui.backend import wa_relay
    monkeypatch.setattr(wa_relay, "status", lambda: {"running": False, "status": "stopped"})

    r = client.get("/api/whatsapp/status")
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"


def test_whatsapp_start_calls_relay(client, monkeypatch):
    _login(client)

    from webui.backend import wa_relay
    calls = []

    def fake_start(mode="qr", pairing_phone=""):
        calls.append((mode, pairing_phone))
        return {"running": True, "status": "awaiting_qr_scan"}

    monkeypatch.setattr(wa_relay, "start", fake_start)

    r = client.post("/api/whatsapp/start", json={"mode": "qr"})
    assert r.status_code == 200
    assert r.json()["running"] is True
    assert calls == [("qr", "")]


def test_whatsapp_start_error_returns_400(client, monkeypatch):
    _login(client)

    from webui.backend import wa_relay
    monkeypatch.setattr(wa_relay, "start", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))

    r = client.post("/api/whatsapp/start", json={"mode": "qr"})
    assert r.status_code == 400
    assert "boom" in r.json()["detail"]
