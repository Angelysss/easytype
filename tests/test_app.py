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
        self.pasted = []
        self.direct = []
        self.keys = []

    def paste(self, text):
        self.pasted.append(text)

    def capture_target(self):
        return 321

    def direct_input(self, target, delete_count, text):
        self.direct.append((target, delete_count, text))

    def direct_key(self, target, key):
        self.keys.append((target, key))


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
    assert 'id="aboutBackdrop"' in page_text
    assert "https://github.com/Angelysss/easytype" in page_text
    assert re.search(r'href="https://github.com/Angelysss/easytype"[^>]*>\s*GitHub\s*</a>', page_text)
    assert "项目地址" not in page_text
    assert "手机端“插入”的作用" in page_text
    assert re.search(
        r'class="topbar-title-row"[^>]*>.*EasyType.*id="connectionBadge"',
        page_text,
        re.DOTALL,
    )
    assert "正在连接服务" not in page_text
    assert "可信局域网模式" not in page_text
    assert "信任模式" in page_text
    assert 'id="revisionState"' not in page_text
    assert 'id="boardModeButton"' in page_text
    assert 'id="directModeButton"' in page_text
    assert 'id="boardTabs"' in page_text
    assert 'id="renameBoardButton"' in page_text
    assert 'id="deleteBoardButton"' not in page_text
    assert 'data-max-boards="8"' in page_text
    assert 'id="directCapture"' not in page_text
    assert 'id="directToggleButton"' not in page_text
    assert 'id="directBackspaceButton"' not in page_text
    assert 'id="directEnterButton"' not in page_text
    assert "EasyType v1.1.1" in page_text
    assert "http://192.168.1.10:5000" in page_text
    assert re.search(r'id="copyButton"[^>]*>\s*复制\s*</button>', page_text)
    assert 'id="remoteEnterButton"' not in page_text
    assert 'id="pasteButton"' not in page_text
    assert re.search(r'id="clearButton"[^>]*>\s*清空\s*</button>', page_text)
    assert re.search(
        r'class="editor-meta"[^>]*>.*id="charCount".*>\|<.*id="syncState"',
        page_text,
        re.DOTALL,
    )
    assert 'class="action-card"' not in page_text
    assert 'class="editor-action-note"' not in page_text
    assert info.status_code == 200
    assert info.get_json()["version"] == "1.1.1"
    assert info.get_json()["revision"] == 0
    assert info.get_json()["boardCount"] == 3
    assert info.get_json()["maxBoards"] == 8
    assert "workMode" not in info.get_json()
    assert page.headers["Content-Security-Policy"].startswith("default-src")

    local_addresses[0] = "192.168.1.25"
    refreshed_page = client.get("/").get_data(as_text=True)
    assert "http://192.168.1.25:5000" in refreshed_page
    assert "http://192.168.1.10:5000" not in refreshed_page


def test_static_assets_have_browser_executable_mime_types(client):
    javascript = client.get("/static/admin.js")
    editor_javascript = client.get("/static/editor.js")
    stylesheet = client.get("/static/styles.css")

    assert javascript.status_code == 200
    assert javascript.mimetype == "application/javascript"
    assert javascript.headers["X-Content-Type-Options"] == "nosniff"
    assert 'document.execCommand("copy")' in editor_javascript.get_data(as_text=True)
    assert "navigator.clipboard?.writeText" in editor_javascript.get_data(as_text=True)
    assert stylesheet.status_code == 200
    assert stylesheet.mimetype == "text/css"


def test_empty_shared_board_keeps_mobile_input_connection(client):
    editor_javascript = client.get("/static/editor.js").get_data(as_text=True)

    assert 'const EMPTY_INPUT_SENTINEL = "\\u200b"' in editor_javascript
    assert 'input.addEventListener("focus", keepEmptySharedInputEditable)' in (
        editor_javascript
    )
    assert "documentState.localText = sharedTextValue()" in editor_javascript
    assert "pendingBoardFocusId" not in editor_javascript
    assert "focusEditor" not in editor_javascript
    assert re.search(
        r'input\.blur\(\);\s+input\.value = "";',
        editor_javascript,
    )


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
    remote_editor_text = remote_editor.get_data(as_text=True)
    assert 'href="/admin"' not in remote_editor_text
    assert 'id="copyButton"' not in remote_editor_text
    assert re.search(
        r'id="remoteEnterButton"[^>]*>\s*回车\s*</button>',
        remote_editor_text,
    )
    assert re.search(r'id="pasteButton"[^>]*>\s*插入\s*</button>', remote_editor_text)
    assert re.search(r'id="clearButton"[^>]*>\s*清空\s*</button>', remote_editor_text)
    assert (
        remote_editor_text.index('id="pasteButton"')
        < remote_editor_text.index('id="remoteEnterButton"')
        < remote_editor_text.index('id="clearButton"')
    )
    assert 'id="directCapture"' in remote_editor_text
    assert 'id="directToggleButton"' in remote_editor_text
    assert 'id="directBackspaceButton"' in remote_editor_text
    assert 'id="directEnterButton"' in remote_editor_text


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
        "board-1",
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


def test_insert_is_remote_only_and_uses_current_shared_text(app):
    app.extensions["easytype_boards"].update(
        "board-2",
        0,
        "手机写好的 Prompt",
    )
    actions = app.extensions["test_actions"]
    _device, token = app.extensions["easytype_auth"].create_device("手机")
    remote_client = app.test_client()
    remote_client.set_cookie(COOKIE_NAME, token)

    local = app.test_client().post(
        "/api/actions/paste",
        json={},
        headers={"Origin": "http://localhost"},
    )
    missing_origin = remote_client.post(
        "/api/actions/paste",
        json={},
        environ_overrides=remote_environ(),
    )
    wrong_origin = remote_client.post(
        "/api/actions/paste",
        json={},
        headers={"Origin": "http://wrong.example"},
        environ_overrides=remote_environ(),
    )
    inserted = remote_client.post(
        "/api/actions/paste",
        json={"boardId": "board-2", "text": "不能由请求覆盖"},
        headers={"Origin": "http://localhost"},
        environ_overrides=remote_environ(),
    )

    assert local.status_code == 403
    assert local.get_json()["error"]["code"] == "remote_only"
    assert missing_origin.status_code == 403
    assert wrong_origin.status_code == 403
    assert inserted.status_code == 200
    assert actions.pasted == ["手机写好的 Prompt"]


def test_remote_enter_is_protected_and_sends_windows_enter(app):
    actions = app.extensions["test_actions"]
    _device, token = app.extensions["easytype_auth"].create_device("手机")
    remote_client = app.test_client()
    remote_client.set_cookie(COOKIE_NAME, token)

    local = app.test_client().post(
        "/api/actions/enter",
        json={},
        headers={"Origin": "http://localhost"},
    )
    missing_origin = remote_client.post(
        "/api/actions/enter",
        json={},
        environ_overrides=remote_environ(),
    )
    pressed = remote_client.post(
        "/api/actions/enter",
        json={},
        headers={"Origin": "http://localhost"},
        environ_overrides=remote_environ(),
    )

    assert local.status_code == 403
    assert local.get_json()["error"]["code"] == "remote_only"
    assert missing_origin.status_code == 403
    assert pressed.status_code == 200
    assert actions.keys == [(0, "enter")]


def test_inactive_direct_keys_send_to_current_windows_target(app):
    actions = app.extensions["test_actions"]
    _device, token = app.extensions["easytype_auth"].create_device("手机")
    remote_client = app.test_client()
    remote_client.set_cookie(COOKIE_NAME, token)
    headers = {"Origin": "http://localhost"}

    backspace = remote_client.post(
        "/api/actions/key",
        json={"key": "backspace"},
        headers=headers,
        environ_overrides=remote_environ(),
    )
    enter = remote_client.post(
        "/api/actions/key",
        json={"key": "enter"},
        headers=headers,
        environ_overrides=remote_environ(),
    )
    invalid = remote_client.post(
        "/api/actions/key",
        json={"key": "tab"},
        headers=headers,
        environ_overrides=remote_environ(),
    )

    assert backspace.status_code == 200
    assert enter.status_code == 200
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_key"
    assert actions.keys == [(0, "backspace"), (0, "enter")]


def test_boards_api_creates_sequential_board_for_trusted_device(app, client):
    switched = client.put(
        "/api/admin/access-mode",
        json={"mode": ACCESS_MODE_TRUSTED_LAN},
        headers={"Origin": "http://localhost"},
    )
    remote_client = app.test_client()
    listed = remote_client.get(
        "/api/boards",
        environ_overrides=remote_environ(),
    )
    created = remote_client.post(
        "/api/boards",
        json={},
        headers={"Origin": "http://localhost"},
        environ_overrides=remote_environ(),
    )

    assert switched.status_code == 200
    assert [board["name"] for board in listed.get_json()["boards"]] == [
        "板 1",
        "板 2",
        "板 3",
    ]
    assert created.status_code == 201
    assert created.get_json()["board"]["name"] == "板 4"
    assert created.get_json()["board"]["id"].startswith("board-4-")


def test_boards_api_limits_and_deletes_current_board(app, client):
    headers = {"Origin": "http://localhost"}
    created = []
    for _ in range(5):
        response = client.post("/api/boards", json={}, headers=headers)
        assert response.status_code == 201
        created.append(response.get_json()["board"])

    limited = client.post("/api/boards", json={}, headers=headers)
    assert limited.status_code == 409
    assert limited.get_json()["error"]["code"] == "board_limit_reached"

    deleted = client.delete("/api/boards/board-2", headers=headers)
    assert deleted.status_code == 200
    assert deleted.get_json()["deletedId"] == "board-2"
    assert deleted.get_json()["fallback"]["name"] == "板 3"

    replacement = client.post("/api/boards", json={}, headers=headers)
    assert replacement.status_code == 201
    assert replacement.get_json()["board"]["name"] == "板 2"
    assert replacement.get_json()["board"]["id"].startswith("board-2-")


def test_boards_api_renames_board_and_validates_name(app, client):
    headers = {"Origin": "http://localhost"}

    renamed = client.patch(
        "/api/boards/board-1",
        json={"name": "常用 Prompt"},
        headers=headers,
    )
    invalid = client.patch(
        "/api/boards/board-1",
        json={"name": " "},
        headers=headers,
    )

    assert renamed.status_code == 200
    assert renamed.get_json()["board"]["name"] == "常用 Prompt"
    assert client.get("/api/boards").get_json()["boards"][0]["name"] == "常用 Prompt"
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_board_name"


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
    assert client.post("/api/actions/copy", json={}).status_code == 404
