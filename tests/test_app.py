import hashlib
import json
import re

import pytest

import easytype_app
from easytype_app import (
    ACCESS_MODE_PAIRING,
    ACCESS_MODE_TRUSTED_LAN,
    COOKIE_NAME,
    TRUSTED_LAN_DEVICE_ID,
    create_app,
)


class FakeActions:
    def __init__(self):
        self.copied = []
        self.pasted = []

    def copy(self, text):
        self.copied.append(text)

    def paste(self, text):
        self.pasted.append(text)


class FakeWebSocket:
    def __init__(self):
        self.closed = False

    def close(self, **_kwargs):
        self.closed = True


@pytest.fixture
def app(tmp_path):
    actions = FakeActions()
    application = create_app(
        data_dir=tmp_path,
        action_backend=actions,
        testing=True,
    )
    application.extensions["test_actions"] = actions
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def remote_environ(address="192.168.1.40"):
    return {"REMOTE_ADDR": address}


def test_local_editor_and_info_are_available(client, monkeypatch):
    local_addresses = ["192.168.1.10"]
    monkeypatch.setattr(
        easytype_app,
        "get_local_ipv4_addresses",
        lambda: local_addresses.copy(),
    )
    page = client.get("/")
    info = client.get("/api/info")
    page_text = page.get_data(as_text=True)

    assert page.status_code == 200
    assert "共享文本板" in page_text
    assert 'href="/admin"' in page_text
    assert "未连接" in page_text
    assert 'id="aboutButton"' in page_text
    assert 'id="aboutPanel"' in page_text
    assert "https://github.com/Angelysss/easytype" in page_text
    assert "“插入”的作用" in page_text
    assert re.search(
        r'class="topbar-title-row"[^>]*>.*共享文本板.*id="connectionBadge"',
        page_text,
        re.DOTALL,
    )
    assert "正在连接服务" not in page_text
    assert "可信局域网模式" not in page_text
    assert "信任模式" in page_text
    assert 'id="revisionState"' not in page_text
    assert "EasyType v1.0.0" in page_text
    assert "http://192.168.1.10:5000" in page_text
    assert re.search(r'id="copyButton"[^>]*>\s*复制\s*</button>', page_text)
    assert re.search(r'id="pasteButton"[^>]*>\s*插入\s*</button>', page_text)
    assert re.search(r'id="clearButton"[^>]*>\s*清空\s*</button>', page_text)
    assert re.search(
        r'class="editor-meta"[^>]*>.*id="charCount".*>\|<.*id="syncState"',
        page_text,
        re.DOTALL,
    )
    assert 'class="action-card"' not in page_text
    assert 'class="editor-action-note"' not in page_text
    assert info.status_code == 200
    assert info.get_json()["version"] == "1.0.0"
    assert info.get_json()["revision"] == 0
    assert page.headers["Content-Security-Policy"].startswith("default-src")

    local_addresses[0] = "192.168.1.25"
    refreshed_page = client.get("/").get_data(as_text=True)
    assert "http://192.168.1.25:5000" in refreshed_page
    assert "http://192.168.1.10:5000" not in refreshed_page


def test_static_assets_have_browser_executable_mime_types(client):
    javascript = client.get("/static/admin.js")
    stylesheet = client.get("/static/styles.css")

    assert javascript.status_code == 200
    assert javascript.mimetype == "application/javascript"
    assert javascript.headers["X-Content-Type-Options"] == "nosniff"
    assert stylesheet.status_code == 200
    assert stylesheet.mimetype == "text/css"


def test_remote_device_must_pair_and_pairing_is_single_use(app, client):
    pairing = app.extensions["easytype_pairing"].create()

    unauthorized = client.get("/api/document", environ_overrides=remote_environ())
    paired = client.post(
        "/api/pair",
        data={"code": pairing.value, "deviceName": "客厅手机"},
        environ_overrides=remote_environ(),
    )

    assert unauthorized.status_code == 401
    assert paired.status_code == 302
    assert f"{COOKIE_NAME}=" in paired.headers["Set-Cookie"]
    assert "HttpOnly" in paired.headers["Set-Cookie"]
    assert "SameSite=Strict" in paired.headers["Set-Cookie"]

    authorized = client.get("/api/document", environ_overrides=remote_environ())
    reused = client.post(
        "/api/pair",
        data={"code": pairing.value, "deviceName": "第二台设备"},
        headers={"Origin": "http://localhost"},
        environ_overrides=remote_environ(),
    )
    assert authorized.status_code == 200
    assert reused.status_code == 400


def test_pairing_rejects_cross_origin(app, client):
    pairing = app.extensions["easytype_pairing"].create()

    response = client.post(
        "/api/pair",
        data={"code": pairing.value, "deviceName": "手机"},
        headers={"Origin": "http://evil.example"},
        environ_overrides=remote_environ(),
    )

    assert response.status_code == 403
    assert app.extensions["easytype_pairing"].get(pairing.value) is not None


def test_pairing_uses_fetch_metadata_when_origin_is_opaque(app, client):
    allowed_pairing = app.extensions["easytype_pairing"].create()
    denied_pairing = app.extensions["easytype_pairing"].create()

    allowed = client.post(
        "/api/pair",
        data={"code": allowed_pairing.value, "deviceName": "扫码浏览器"},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        environ_overrides=remote_environ(),
    )
    denied = client.post(
        "/api/pair",
        data={"code": denied_pairing.value, "deviceName": "跨站页面"},
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        environ_overrides=remote_environ(),
    )

    assert allowed.status_code == 302
    assert denied.status_code == 403
    assert app.extensions["easytype_pairing"].get(denied_pairing.value) is not None


def test_public_network_address_is_denied(client):
    response = client.get("/", environ_overrides=remote_environ("8.8.8.8"))

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "network_denied"


def test_admin_is_loopback_only(app, client):
    local = client.get("/admin")
    remote = client.get("/admin", environ_overrides=remote_environ())

    assert local.status_code == 200
    assert "本机局域网地址" not in local.get_data(as_text=True)
    assert remote.status_code == 403

    _device, token = app.extensions["easytype_auth"].create_device("手机")
    remote_client = app.test_client()
    remote_client.set_cookie(COOKIE_NAME, token)
    remote_editor = remote_client.get("/", environ_overrides=remote_environ())

    assert remote_editor.status_code == 200
    assert 'href="/admin"' not in remote_editor.get_data(as_text=True)


def test_local_access_mode_switch_controls_unpaired_lan_access(app, client):
    remote_client = app.test_client()
    before = remote_client.get(
        "/api/document",
        environ_overrides=remote_environ(),
    )

    trusted = client.put(
        "/api/admin/access-mode",
        json={"mode": ACCESS_MODE_TRUSTED_LAN},
        headers={"Origin": "http://localhost"},
    )
    during = remote_client.get(
        "/api/document",
        environ_overrides=remote_environ(),
    )
    local_home = client.get("/").get_data(as_text=True)
    admin = client.get("/admin")

    assert before.status_code == 401
    assert trusted.status_code == 200
    assert during.status_code == 200
    assert trusted.get_json()["mode"] == ACCESS_MODE_TRUSTED_LAN
    assert "访问设置 · 信任模式" in local_home
    assert 'id="deviceManagementLink"' in local_home
    assert admin.status_code == 302

    paired_device, token = app.extensions["easytype_auth"].create_device("已配对手机")
    paired_client = app.test_client()
    paired_client.set_cookie(COOKIE_NAME, token)
    trusted_socket = FakeWebSocket()
    app.extensions["easytype_hub"].register(
        trusted_socket,
        TRUSTED_LAN_DEVICE_ID,
    )

    pairing = client.put(
        "/api/admin/access-mode",
        json={"mode": ACCESS_MODE_PAIRING},
        headers={"Origin": "http://localhost"},
    )
    after = remote_client.get(
        "/api/document",
        environ_overrides=remote_environ(),
    )
    paired_after = paired_client.get(
        "/api/document",
        environ_overrides=remote_environ(),
    )

    assert paired_device["name"] == "已配对手机"
    assert pairing.status_code == 200
    assert after.status_code == 401
    assert paired_after.status_code == 200
    assert trusted_socket.closed is True


def test_access_mode_can_only_be_changed_locally_from_same_origin(client):
    remote = client.put(
        "/api/admin/access-mode",
        json={"mode": ACCESS_MODE_TRUSTED_LAN},
        headers={"Origin": "http://192.168.1.10:5000"},
        environ_overrides=remote_environ(),
    )
    cross_origin = client.put(
        "/api/admin/access-mode",
        json={"mode": ACCESS_MODE_TRUSTED_LAN},
        headers={"Origin": "http://evil.example"},
    )
    invalid = client.put(
        "/api/admin/access-mode",
        json={"mode": "unknown"},
        headers={"Origin": "http://localhost"},
    )

    assert remote.status_code == 403
    assert cross_origin.status_code == 403
    assert invalid.status_code == 400


def test_trusted_lan_mode_shows_one_phone_address_and_qr(client, monkeypatch):
    monkeypatch.setattr(
        easytype_app,
        "get_local_ipv4_addresses",
        lambda: ["192.168.1.10", "172.22.0.1"],
    )
    switched = client.put(
        "/api/admin/access-mode",
        json={"mode": ACCESS_MODE_TRUSTED_LAN},
        headers={"Origin": "http://localhost"},
    )
    page = client.get("/").get_data(as_text=True)
    qr = client.get("/api/admin/trusted-lan-qr")

    assert switched.status_code == 200
    assert "http://192.168.1.10:5000" in page
    assert "172.22.0.1" not in page
    assert qr.status_code == 200
    assert qr.mimetype == "image/png"


def test_copy_and_paste_use_only_the_current_document(app, client):
    app.extensions["easytype_document"].update(0, "只复制服务端文本")
    actions = app.extensions["test_actions"]

    copied = client.post(
        "/api/actions/copy",
        json={"text": "攻击者提供的文本"},
        headers={"Origin": "http://localhost"},
    )
    pasted = client.post(
        "/api/actions/paste",
        json={},
        headers={"Origin": "http://localhost"},
    )

    assert copied.status_code == 200
    assert pasted.status_code == 200
    assert actions.copied == ["只复制服务端文本"]
    assert actions.pasted == ["只复制服务端文本"]


def test_remote_origin_is_required_for_mutating_action(app):
    auth = app.extensions["easytype_auth"]
    _device, token = auth.create_device("手机")
    client = app.test_client()
    client.set_cookie(COOKIE_NAME, token)

    missing = client.post(
        "/api/actions/copy",
        json={},
        environ_overrides=remote_environ(),
    )
    wrong = client.post(
        "/api/actions/copy",
        json={},
        headers={"Origin": "http://wrong.example"},
        environ_overrides=remote_environ(),
    )
    correct = client.post(
        "/api/actions/copy",
        json={},
        headers={"Origin": "http://localhost"},
        environ_overrides=remote_environ(),
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert correct.status_code == 200


def test_device_revocation_invalidates_cookie(app):
    auth = app.extensions["easytype_auth"]
    device, token = auth.create_device("待撤销设备")
    remote_client = app.test_client()
    remote_client.set_cookie(COOKIE_NAME, token)

    before = remote_client.get(
        "/api/document",
        environ_overrides=remote_environ(),
    )
    revoked = app.test_client().delete(
        f"/api/admin/devices/{device['id']}",
        json={},
        headers={"Origin": "http://localhost"},
    )
    after = remote_client.get(
        "/api/document",
        environ_overrides=remote_environ(),
    )

    assert before.status_code == 200
    assert revoked.status_code == 200
    assert after.status_code == 401


def test_pairing_qr_and_device_list_do_not_expose_tokens(app, client, monkeypatch):
    monkeypatch.setattr(easytype_app, "get_local_ipv4_addresses", lambda: ["192.168.1.10"])
    created = client.post(
        "/api/admin/pairing-codes",
        json={},
        headers={"Origin": "http://localhost"},
    )
    payload = created.get_json()

    assert created.status_code == 200
    assert payload["pairUrl"].startswith("http://192.168.1.10:5000/pair?code=")
    qr = client.get(payload["qrUrl"])
    assert qr.status_code == 200
    assert qr.mimetype == "image/png"

    device, token = app.extensions["easytype_auth"].create_device("测试设备")
    devices = client.get("/api/admin/devices").get_json()["devices"]
    serialized = json.dumps(devices, ensure_ascii=False)
    assert device["id"] in serialized
    assert token not in serialized
    assert hashlib.sha256(token.encode()).hexdigest() not in serialized


def test_legacy_remote_control_routes_are_removed(client):
    assert client.post("/type", json={"text": "unsafe"}).status_code == 404
    assert client.post("/key", json={"key": "enter"}).status_code == 404
