import json
import sys
import types
from pathlib import Path

import pipeline


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_pay_only_selects_latest_registered_unpaid_account(tmp_path, monkeypatch):
    registered = tmp_path / "registered_accounts.jsonl"
    pipeline_batch = tmp_path / "pipeline_batch.jsonl"
    card_results = tmp_path / "results.jsonl"

    monkeypatch.setattr(pipeline, "REGISTERED_ACCOUNTS_FILE", registered)
    monkeypatch.setattr(pipeline, "RESULTS_FILE", pipeline_batch)
    monkeypatch.setattr(pipeline, "CARD_RESULTS_FILE", card_results)

    _write_jsonl(registered, [
        {
            "ts": "2026-05-03T01:00:00+00:00",
            "email": "paid@example.com",
            "session_token": "sess-paid",
            "access_token": "at-paid",
            "device_id": "dev-paid",
        },
        {
            "ts": "2026-05-03T02:00:00+00:00",
            "email": "retry@example.com",
            "session_token": "sess-retry",
            "access_token": "at-retry",
            "device_id": "dev-retry",
        },
        {
            "ts": "2026-05-03T03:00:00+00:00",
            "email": "no-auth@example.com",
            "session_token": "",
            "access_token": "",
            "device_id": "dev-noauth",
        },
    ])
    _write_jsonl(pipeline_batch, [
        {
            "registration": {"status": "ok", "email": "paid@example.com"},
            "payment": {"status": "succeeded", "email": "paid@example.com"},
        },
        {
            "registration": {"status": "ok", "email": "retry@example.com"},
            "payment": {"status": "error", "email": "retry@example.com", "error": "OTP timeout"},
        },
    ])
    _write_jsonl(card_results, [])

    selected = pipeline._select_recent_registered_account_for_pay_only()
    assert selected is not None
    assert selected["email"] == "retry@example.com"
    assert selected["session_token"] == "sess-retry"
    assert selected["access_token"] == "at-retry"


def test_pay_only_treats_already_paid_error_as_consumed(tmp_path, monkeypatch):
    registered = tmp_path / "registered_accounts.jsonl"
    pipeline_batch = tmp_path / "pipeline_batch.jsonl"
    card_results = tmp_path / "results.jsonl"

    monkeypatch.setattr(pipeline, "REGISTERED_ACCOUNTS_FILE", registered)
    monkeypatch.setattr(pipeline, "RESULTS_FILE", pipeline_batch)
    monkeypatch.setattr(pipeline, "CARD_RESULTS_FILE", card_results)

    _write_jsonl(registered, [
        {"email": "older@example.com", "session_token": "sess-older", "access_token": ""},
        {"email": "latest@example.com", "session_token": "sess-latest", "access_token": ""},
    ])
    _write_jsonl(pipeline_batch, [
        {
            "registration": {"status": "ok", "email": "latest@example.com"},
            "payment": {
                "status": "error",
                "email": "latest@example.com",
                "error": '生成 fresh checkout 失败: modern [400]: {"detail":"User is already paid"}',
            },
        },
    ])
    _write_jsonl(card_results, [])

    selected = pipeline._select_recent_registered_account_for_pay_only()
    assert selected is not None
    assert selected["email"] == "older@example.com"


def test_pay_only_success_imports_cpa_with_plus_tag(tmp_path, monkeypatch):
    registered = tmp_path / "registered_accounts.jsonl"
    pipeline_batch = tmp_path / "pipeline_batch.jsonl"
    card_results = tmp_path / "results.jsonl"
    card_config = tmp_path / "config.paypal.json"

    monkeypatch.setattr(pipeline, "REGISTERED_ACCOUNTS_FILE", registered)
    monkeypatch.setattr(pipeline, "RESULTS_FILE", pipeline_batch)
    monkeypatch.setattr(pipeline, "CARD_RESULTS_FILE", card_results)

    _write_jsonl(registered, [
        {
            "email": "retry@example.com",
            "session_token": "sess-retry",
            "access_token": "at-retry",
            "device_id": "dev-retry",
        },
    ])
    _write_jsonl(pipeline_batch, [])
    _write_jsonl(card_results, [])
    card_config.write_text(json.dumps({
        "fresh_checkout": {"plan": {"plan_name": "chatgptplusplan"}},
        "cpa": {
            "enabled": True,
            "base_url": "https://cpa.example.com",
            "admin_key": "adm",
            "oauth_client_id": "app_test",
            "plan_tag": "team",
        },
    }), encoding="utf-8")

    calls = []

    def fake_pay(*args, **kwargs):
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "retry@example.com",
            },
        }

    def fake_cpa(email, sid, cpa_cfg, **kwargs):
        calls.append((email, sid, cpa_cfg, kwargs))
        return "ok"

    monkeypatch.setattr(pipeline, "pay", fake_pay)
    monkeypatch.setattr(pipeline, "_cpa_import_after_team", fake_cpa)

    result = pipeline.pay_only(str(card_config), use_gopay=True)

    assert result["status"] == "succeeded"
    assert calls
    email, sid, cpa_cfg, kwargs = calls[0]
    assert email == "retry@example.com"
    assert sid == "cs_test"
    assert cpa_cfg["plan_tag"] == "plus"
    record = json.loads(pipeline_batch.read_text(encoding="utf-8").strip())
    assert record["cpa_import"] == "ok"


def test_cpa_import_falls_back_to_access_token_without_refresh_token(tmp_path, monkeypatch):
    registered = tmp_path / "registered_accounts.jsonl"
    monkeypatch.setattr(pipeline, "REGISTERED_ACCOUNTS_FILE", registered)
    monkeypatch.setattr(
        pipeline,
        "_load_registered_accounts",
        lambda: [
            {
                "email": "fallback@example.com",
                "access_token": "eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdF8xMjMifSwiZXhwIjoyNTM0MDk0NDAwfQ.sig",
            }
        ],
    )
    monkeypatch.setattr(pipeline, "_find_latest_refresh_token_for_email", lambda *args, **kwargs: "")

    fake_calls = []

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.proxies = {}
            self.trust_env = False

        def post(self, url, params=None, json=None, headers=None, timeout=None):
            fake_calls.append({"url": url, "params": params, "json": json, "headers": headers, "timeout": timeout})
            return FakeResponse()

    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.Session = lambda impersonate=None: FakeSession()
    fake_pkg = types.ModuleType("curl_cffi")
    fake_pkg.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_pkg)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    status = pipeline._cpa_import_after_team(
        "fallback@example.com",
        "cs_test",
        {
            "enabled": True,
            "base_url": "https://cpa.example.com",
            "admin_key": "secret-admin-key",
            "oauth_client_id": "app_test_client",
            "plan_tag": "team",
            "free_plan_tag": "free",
        },
    )

    assert status == "ok"
    assert fake_calls
    body = fake_calls[0]["json"]
    assert body["email"] == "fallback@example.com"
    assert body["access_token"].startswith("eyJhbGciOiJub25lIn0.")
    assert body["refresh_token"] == ""
    assert body["account_id"] == "acct_123"
