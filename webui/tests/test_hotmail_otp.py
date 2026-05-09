import importlib.util
from pathlib import Path


def _load_card_module():
    root = Path(__file__).resolve().parents[2]
    path = root / "CTF-pay" / "card.py"
    spec = importlib.util.spec_from_file_location("ctf_pay_card_for_hotmail_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_hotmail_fetch_validates_token_before_saving_refresh_token(monkeypatch):
    card = _load_card_module()
    monkeypatch.setattr(
        card,
        "_MS_TOKEN_ENDPOINTS",
        [
            ("bad-token", "https://token.invalid/bad", {}),
            ("good-token", "https://token.invalid/good", {}),
        ],
    )

    def fake_post(url, data, timeout_s=45):
        if url.endswith("/bad"):
            return {"access_token": "bad-access", "refresh_token": "bad-next-rt"}
        return {"access_token": "good-access", "refresh_token": "good-next-rt"}

    def fake_fetch(access_token, mailboxes, top):
        if access_token == "bad-access":
            raise RuntimeError("graph: HTTP 401 Unauthorized")
        return [
            {
                "mailbox": "INBOX",
                "subject": "ChatGPT login code",
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                "bodyPreview": "Your code is 123456",
                "receivedTimestamp": 2_000_000,
            }
        ], "graph"

    saved = []
    monkeypatch.setattr(card, "_http_post_form_json", fake_post)
    monkeypatch.setattr(card, "_fetch_hotmail_messages", fake_fetch)
    monkeypatch.setattr(card, "_save_hotmail_refresh_token", lambda *args: saved.append(args))
    monkeypatch.setattr(card, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        card,
        "_resolve_hotmail_account_cfg",
        lambda target_email, mail_cfg: {
            "email": target_email,
            "client_id": "client-id",
            "refresh_token": "initial-rt",
            "mailboxes": ["INBOX"],
            "top": 5,
            "sender_filters": ["openai"],
            "subject_filters": ["code"],
            "exclude_codes": [],
        },
    )

    code = card._fetch_hotmail_openai_login_otp("user@hotmail.com", {}, timeout=1, issued_after=1)

    assert code == "123456"
    assert len(saved) == 1
    assert saved[0][1] == "good-next-rt"
    assert saved[0][2]["token_endpoint"] == "good-token"


def test_hotmail_live_token_uses_imap_when_rest_rejects_token(monkeypatch):
    card = _load_card_module()
    monkeypatch.setattr(card, "_MS_TOKEN_ENDPOINTS", [("live", "https://token.invalid/live", {})])
    monkeypatch.setattr(
        card,
        "_http_post_form_json",
        lambda *args, **kwargs: {"access_token": "live-access", "refresh_token": "live-next-rt"},
    )
    monkeypatch.setattr(
        card,
        "_fetch_hotmail_messages",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("graph: HTTP 401 Unauthorized")),
    )
    monkeypatch.setattr(
        card,
        "_fetch_hotmail_imap_messages",
        lambda email, token, mailboxes, top: [
            {
                "mailbox": "INBOX",
                "subject": "ChatGPT login code",
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                "bodyPreview": "Your code is 654321",
                "receivedTimestamp": 2_000_000,
            }
        ],
    )

    messages, transport, token_payload = card._fetch_hotmail_messages_with_refresh_token(
        {
            "email": "user@hotmail.com",
            "client_id": "client-id",
            "refresh_token": "initial-rt",
            "mailboxes": ["INBOX"],
            "top": 5,
        }
    )

    assert transport == "imap"
    assert token_payload["next_refresh_token"] == "live-next-rt"
    assert messages[0]["bodyPreview"].endswith("654321")


def test_hotmail_extracts_code_from_html_body_content():
    card = _load_card_module()
    code, _message = card._select_hotmail_code(
        [
            {
                "subject": "Your temporary ChatGPT verification code",
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                "bodyPreview": "OpenAI message",
                "body": {"content": "<html><body>code&nbsp;is&nbsp;<b>789012</b></body></html>"},
                "receivedTimestamp": 2_000_000,
            }
        ],
        issued_after=1,
        sender_filters=["openai"],
        subject_filters=["code"],
        exclude_codes=[],
    )

    assert code == "789012"


def test_hotmail_html_to_text_unescapes_entities():
    card = _load_card_module()

    assert card._html_to_text("<p>code&nbsp;is&nbsp;<b>345678</b></p>") == "code is 345678"
