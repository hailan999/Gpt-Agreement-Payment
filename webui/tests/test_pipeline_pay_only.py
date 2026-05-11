import json
import sys
import types

import pipeline
from webui.backend.db import get_db


def _reset_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    db = get_db()
    db.clear_runtime_data()
    return db


def test_pay_only_selects_latest_registered_unpaid_account(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)

    for row in [
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
    ]:
        db.add_registered_account(row)
    db.add_pipeline_result({
        "registration": {"status": "ok", "email": "paid@example.com"},
        "payment": {"status": "succeeded", "email": "paid@example.com"},
    })
    db.add_pipeline_result({
        "registration": {"status": "ok", "email": "retry@example.com"},
        "payment": {"status": "error", "email": "retry@example.com", "error": "OTP timeout"},
    })

    selected = pipeline._select_recent_registered_account_for_pay_only()
    assert selected is not None
    assert selected["email"] == "retry@example.com"
    assert selected["session_token"] == "sess-retry"
    assert selected["access_token"] == "at-retry"


def test_pay_only_treats_already_paid_error_as_consumed(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)

    db.add_registered_account({"email": "older@example.com", "session_token": "sess-older", "access_token": ""})
    db.add_registered_account({"email": "latest@example.com", "session_token": "sess-latest", "access_token": ""})
    db.add_pipeline_result({
        "registration": {"status": "ok", "email": "latest@example.com"},
        "payment": {
            "status": "error",
            "email": "latest@example.com",
            "error": '生成 fresh checkout 失败: modern [400]: {"detail":"User is already paid"}',
        },
    })

    selected = pipeline._select_recent_registered_account_for_pay_only()
    assert selected is not None
    assert selected["email"] == "older@example.com"


def test_pay_only_success_without_rt_marks_un_oauthed_and_skips_cpa(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"

    db.add_registered_account({
        "email": "retry@example.com",
        "session_token": "sess-retry",
        "access_token": "at-retry",
        "device_id": "dev-retry",
    })
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

    def fake_pay(*args, **kwargs):
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "retry@example.com",
            },
        }

    def fake_cpa(email, sid, cpa_cfg, **kwargs):
        raise AssertionError("pay-only must not import CPA")

    monkeypatch.setattr(pipeline, "pay", fake_pay)
    monkeypatch.setattr(pipeline, "_cpa_import_after_team", fake_cpa)

    result = pipeline.pay_only(str(card_config), use_gopay=True)

    assert result["status"] == "succeeded"
    rows = get_db().iter_pipeline_results()
    assert "cpa_import" not in rows[-1]
    accounts = get_db().iter_registered_accounts()
    assert accounts[-1]["status"] == "UN_OAUTHED"


def test_pay_only_tells_card_to_skip_post_payment_rt(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text("{}", encoding="utf-8")

    db.add_registered_account({
        "email": "skip-rt@example.com",
        "session_token": "sess-skip",
        "access_token": "at-skip",
    })

    seen = {}

    def fake_pay(*args, **kwargs):
        seen["skip_post_payment_rt"] = kwargs.get("skip_post_payment_rt")
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "skip-rt@example.com",
            },
        }

    monkeypatch.setattr(pipeline, "pay", fake_pay)

    result = pipeline.pay_only(str(card_config))

    assert result["status"] == "succeeded"
    assert seen["skip_post_payment_rt"] is True


def test_pay_only_success_with_rt_marks_success(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text("{}", encoding="utf-8")

    db.add_registered_account({
        "email": "has-rt@example.com",
        "session_token": "sess-rt",
        "access_token": "at-rt",
        "device_id": "dev-rt",
    })

    def fake_pay(*args, **kwargs):
        row = get_db().find_latest_registered_account("has-rt@example.com")
        get_db().update_registered_account_refresh_token(row["id"], "rt_test")
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "has-rt@example.com",
            },
        }

    monkeypatch.setattr(pipeline, "pay", fake_pay)

    result = pipeline.pay_only(str(card_config))

    assert result["status"] == "succeeded"
    account = get_db().find_latest_registered_account("has-rt@example.com")
    assert account["status"] == "SUCCESS"
    assert account["refresh_token"] == "rt_test"


def test_pay_only_selects_only_initial_accounts(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)

    db.add_registered_account({
        "email": "failed@example.com",
        "session_token": "sess-failed",
        "access_token": "at-failed",
        "status": "FAILED",
    })
    db.add_registered_account({
        "email": "initial@example.com",
        "session_token": "sess-initial",
        "access_token": "at-initial",
        "status": "INITIAL",
    })

    selected = pipeline._select_recent_registered_account_for_pay_only()

    assert selected is not None
    assert selected["email"] == "initial@example.com"


def test_pay_only_ignores_cardholder_email_when_selecting_account(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text(json.dumps({
        "cards": [{"email": "cardholder@example.com"}],
    }), encoding="utf-8")

    db.add_registered_account({
        "email": "initial@example.com",
        "session_token": "sess-initial",
        "access_token": "at-initial",
        "status": "INITIAL",
    })

    calls = []

    def fake_pay(*args, **kwargs):
        calls.append(kwargs)
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "initial@example.com",
            },
        }

    monkeypatch.setattr(pipeline, "pay", fake_pay)

    result = pipeline.pay_only(str(card_config))

    assert result["status"] == "succeeded"
    assert calls[0]["session_token"] == "sess-initial"
    assert calls[0]["access_token"] == "at-initial"


def test_pay_only_prefers_registered_account_proxy(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text(json.dumps({
        "proxy": "http://config-user:config-pass@proxy.example:80",
        "proxies": {
            "enabled": True,
            "rotation": "random",
            "list": ["http://pool-user:pool-pass@proxy.example:80"],
        },
    }), encoding="utf-8")

    account_proxy = "http://acct-user:acct-pass@proxy.example:80"
    db.add_registered_account({
        "email": "initial@example.com",
        "session_token": "sess-initial",
        "access_token": "at-initial",
        "proxy_add": account_proxy,
        "status": "INITIAL",
    })

    pay_config_paths = []

    def fake_fingerprint(card_cfg, proxy_url=""):
        assert proxy_url == account_proxy
        return {"proxy": proxy_url}

    def fake_pay(card_config_path, **kwargs):
        pay_config_paths.append(card_config_path)
        used_cfg = json.loads(open(card_config_path, encoding="utf-8").read())
        assert used_cfg["proxy"] == account_proxy
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "initial@example.com",
            },
        }

    monkeypatch.setattr(pipeline, "_register_pipeline_start_fingerprint", fake_fingerprint)
    monkeypatch.setattr(pipeline, "pay", fake_pay)

    result = pipeline.pay_only(str(card_config), proxy_url="http://random-user:random-pass@proxy.example:80")

    assert result["status"] == "succeeded"
    assert result["proxy"] == account_proxy
    assert result["stripe_fingerprint"] == {"proxy": account_proxy}
    assert pay_config_paths


def test_pay_only_marks_no_trial_when_due_nonzero(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text("{}", encoding="utf-8")

    db.add_registered_account({
        "email": "trial@example.com",
        "session_token": "sess-trial",
        "access_token": "at-trial",
        "status": "INITIAL",
    })

    def fake_pay(*args, **kwargs):
        return {
            "status": "no_trial",
            "raw": {
                "chatgpt_email": "trial@example.com",
                "total_summary": {"due": 34900000, "subtotal": 34900000, "total": 34900000},
            },
        }

    monkeypatch.setattr(pipeline, "pay", fake_pay)

    result = pipeline.pay_only(str(card_config))

    assert result["status"] == "no_trial"
    accounts = get_db().iter_registered_accounts()
    assert accounts[-1]["status"] == "NO_TRIAL"
    rows = get_db().iter_pipeline_results()
    assert rows[-1]["payment"]["status"] == "no_trial"
    assert rows[-1]["payment"]["error"] == "total_summary.due is not 0"


def test_pay_only_preserves_un_oauthed_after_payment_success(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text("{}", encoding="utf-8")

    db.add_registered_account({
        "email": "oauth-failed@example.com",
        "session_token": "sess-oauth",
        "access_token": "at-oauth",
        "status": "INITIAL",
    })

    def fake_pay(*args, **kwargs):
        row = get_db().find_latest_registered_account("oauth-failed@example.com")
        get_db().update_registered_account_status(row["id"], "UN_OAUTHED")
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "oauth-failed@example.com",
            },
        }

    monkeypatch.setattr(pipeline, "pay", fake_pay)

    result = pipeline.pay_only(str(card_config))

    assert result["status"] == "succeeded"
    accounts = get_db().iter_registered_accounts()
    assert accounts[-1]["status"] == "UN_OAUTHED"


def test_free_backfill_rt_uses_latest_un_oauthed_accounts_and_saves_rt(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    cardw_config = tmp_path / "config.paypal-proxy.json"
    card_config.write_text(json.dumps({"cpa": {"enabled": True}}), encoding="utf-8")
    cardw_config.write_text(json.dumps({
        "mail": {
            "hotmail": {
                "client_id": "client-from-cardw",
                "refresh_token": "rt-from-cardw",
            },
        },
    }), encoding="utf-8")

    db.add_registered_account({
        "email": "old@example.com",
        "password": "pw-old",
        "status": "UN_OAUTHED",
    })
    db.add_registered_account({
        "email": "old@example.com",
        "password": "pw-new",
        "status": "SUCCESS",
    })
    db.add_registered_account({
        "email": "initial@example.com",
        "password": "pw-initial",
        "status": "INITIAL",
    })
    db.add_registered_account({
        "email": "needs@hotmail.com",
        "password": "pw-hot",
        "status": "UN_OAUTHED",
        "hot_client": "client-from-db",
        "hot_rt": "hotmail-rt-from-db",
    })

    calls = []
    cpa_calls = []

    def fake_exchange(email, password, mail_cfg, proxy_url):
        calls.append((email, password, mail_cfg, proxy_url))
        assert mail_cfg["hotmail"]["client_id"] == "client-from-cardw"
        return "codex-refresh-token", ""

    monkeypatch.setattr(pipeline, "_ensure_gost_alive", lambda cfg: None)
    monkeypatch.setattr(pipeline, "_exchange_rt_with_classification", fake_exchange)
    monkeypatch.setattr(
        pipeline,
        "_cpa_import_after_team",
        lambda *args, **kwargs: cpa_calls.append((args, kwargs)) or "ok",
    )
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_args, **_kwargs: None)

    pipeline.free_backfill_rt_loop(str(card_config), cardw_config_path=str(cardw_config))

    assert [call[0] for call in calls] == ["needs@hotmail.com"]
    account = get_db().find_latest_registered_account("needs@hotmail.com")
    assert account["refresh_token"] == "codex-refresh-token"
    assert account["status"] == "SUCCESS"
    assert get_db().load_oauth_status_map()["needs@hotmail.com"]["status"] == "succeeded"
    assert cpa_calls
    assert cpa_calls[0][1]["unlink_gopay"] is False


def test_free_backfill_rt_marks_add_phone_status(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text(json.dumps({"cpa": {"enabled": False}}), encoding="utf-8")

    db.add_registered_account({
        "email": "phone-required@hotmail.com",
        "password": "pw-hot",
        "status": "UN_OAUTHED",
    })

    monkeypatch.setattr(pipeline, "_ensure_gost_alive", lambda cfg: None)
    monkeypatch.setattr(
        pipeline,
        "_exchange_rt_with_classification",
        lambda *args, **kwargs: ("", "add_phone_blocked"),
    )
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_args, **_kwargs: None)

    pipeline.free_backfill_rt_loop(str(card_config))

    account = get_db().find_latest_registered_account("phone-required@hotmail.com")
    assert account["status"] == "ADD_PHONE"
    oauth = get_db().load_oauth_status_map()["phone-required@hotmail.com"]
    assert oauth["status"] == "transient_failed"
    assert oauth["fail_reason"] == "add_phone_blocked"


def test_pay_only_preserves_add_phone_over_success(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    card_config = tmp_path / "config.paypal.json"
    card_config.write_text("{}", encoding="utf-8")

    db.add_registered_account({
        "email": "add-phone@example.com",
        "session_token": "sess-phone",
        "access_token": "at-phone",
        "status": "INITIAL",
    })

    def fake_pay(*args, **kwargs):
        row = get_db().find_latest_registered_account("add-phone@example.com")
        get_db().update_registered_account_status(row["id"], "ADD_PHONE")
        return {
            "status": "succeeded",
            "raw": {
                "session_id": "cs_test",
                "chatgpt_email": "add-phone@example.com",
            },
        }

    monkeypatch.setattr(pipeline, "pay", fake_pay)

    result = pipeline.pay_only(str(card_config))

    assert result["status"] == "succeeded"
    account = get_db().find_latest_registered_account("add-phone@example.com")
    assert account["status"] == "ADD_PHONE"


def test_batch_full_pipeline_preserves_gopay_flag(monkeypatch):
    calls = []

    monkeypatch.setattr(pipeline, "_read_card_cfg", lambda path: {})
    monkeypatch.setattr(pipeline, "_load_cardw_path_from_card_cfg", lambda cfg, path: "cardw.json")
    monkeypatch.setattr(
        pipeline,
        "_build_domain_pool_from_cardw",
        lambda *args, **kwargs: types.SimpleNamespace(domains=[], provisioner=None),
    )
    monkeypatch.setattr(pipeline, "_build_team_client_from_card_cfg", lambda cfg: None)
    monkeypatch.setattr(pipeline, "_build_proxy_pool_from_card_cfg", lambda cfg: types.SimpleNamespace(proxies=[]))

    def fake_pipeline(card_config_path, **kwargs):
        calls.append(kwargs)
        return {
            "registration": {"email": "ok@example.com"},
            "payment": {"status": "succeeded", "email": "ok@example.com"},
        }

    monkeypatch.setattr(pipeline, "pipeline", fake_pipeline)

    pipeline.batch("config.json", 1, delay=0, workers=1, use_gopay=True, gopay_otp_file="otp.txt")

    assert calls
    assert calls[0]["use_gopay"] is True
    assert calls[0]["gopay_otp_file"] == "otp.txt"


def test_cpa_import_falls_back_to_access_token_without_refresh_token(tmp_path, monkeypatch):
    db = _reset_db(tmp_path, monkeypatch)
    db.add_registered_account({
        "email": "fallback@example.com",
        "access_token": "eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjdF8xMjMifSwiZXhwIjoyNTM0MDk0NDAwfQ.sig",
    })
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
