from __future__ import annotations

import types

import notify_receiver as nr


def test_notify_receiver_syncs_whatsapp_gopay_otp(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DATA_DIR", str(tmp_path))
    nr._ARGS = types.SimpleNamespace(
        no_webui_otp_sync=False,
        webui_otp_state_key="wa_state",
        max_content_chars=20000,
    )
    payload = {
        "source": "android_notification",
        "app": "Notify Relay Bridge",
        "from": "com.whatsapp",
        "package": "com.whatsapp",
        "notification": {
            "title": "GoPay",
            "text": "551226 is your verification code. For your security, do not share this code.",
            "big_text": "",
            "sub_text": "",
            "summary_text": "",
            "text_lines": [],
        },
        "post_time": 1777892769,
        "ts": 1777892769,
        "notification_key": "test-key",
        "text": "GoPay\n551226 is your verification code. For your security, do not share this code.",
    }
    record = {
        "received_ts": 1777892768.9480677,
        "source": "android_notification",
        "content": nr.combined_content(payload),
        "payload": payload,
    }

    assert nr.sync_webui_gopay_otp(record) == "551226"

    from webui.backend.db import get_db

    state = get_db().get_runtime_json("wa_state", {})
    assert state["latest"]["otp"] == "551226"
    assert state["latest"]["source"] == "android_notification"


def test_notify_receiver_ignores_non_whatsapp_notifications():
    nr._ARGS = types.SimpleNamespace(
        no_webui_otp_sync=False,
        webui_otp_state_key="wa_state",
        max_content_chars=20000,
    )
    record = {
        "received_ts": 1777892768.9480677,
        "source": "android_notification",
        "content": "Not Selected\n551226 Bytes/s",
        "payload": {"package": "com.sdkdns.app", "text": "Not Selected\n551226 Bytes/s"},
    }

    assert nr.sync_webui_gopay_otp(record) == ""
