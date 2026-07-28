import json
import ctypes

import pytest

from easytype_app import (
    ACCESS_MODE_PAIRING,
    ACCESS_MODE_TRUSTED_LAN,
    AccessSettingsStore,
    AuthStore,
    BoardLimitReached,
    BoardStore,
    DirectInputFailure,
    DirectInputManager,
    InvalidBoardName,
    InvalidDocument,
    LastBoardDeletion,
    MAX_BOARDS,
    PairingManager,
    RevisionConflict,
    SyncHub,
    WindowsActions,
)


def test_access_settings_store_defaults_to_pairing_and_persists(tmp_path):
    path = tmp_path / "settings.json"
    store = AccessSettingsStore(path)

    assert store.mode() == ACCESS_MODE_PAIRING
    assert store.set_mode(ACCESS_MODE_TRUSTED_LAN) == ACCESS_MODE_TRUSTED_LAN
    reloaded = AccessSettingsStore(path)
    assert reloaded.mode() == ACCESS_MODE_TRUSTED_LAN

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {
        "version": 3,
        "accessMode": ACCESS_MODE_TRUSTED_LAN,
    }

    with pytest.raises(ValueError):
        store.set_mode("unknown")


def test_board_store_creates_three_boards_and_persists_updates(tmp_path):
    path = tmp_path / "state.db"
    store = BoardStore(path)
    boards = store.list_boards()

    assert [board["name"] for board in boards] == ["板 1", "板 2", "板 3"]
    updated = store.update("board-1", 0, "手机和电脑共享的内容")

    assert updated["revision"] == 1
    assert BoardStore(path).snapshot("board-1") == updated


def test_board_store_migrates_legacy_document_to_board_one(tmp_path):
    legacy = tmp_path / "state.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "text": "旧版共享文本",
                "revision": 7,
                "updatedAt": "2026-01-01T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = BoardStore(tmp_path / "state.db", legacy_path=legacy)

    assert store.snapshot("board-1")["text"] == "旧版共享文本"
    assert store.snapshot("board-1")["revision"] == 7
    assert store.snapshot("board-2")["text"] == ""
    assert legacy.exists()


def test_board_store_rejects_stale_revision_without_losing_text(tmp_path):
    store = BoardStore(tmp_path / "state.db")
    store.update("board-1", 0, "电脑版本")

    with pytest.raises(RevisionConflict) as captured:
        store.update("board-1", 0, "手机的过期版本")

    assert captured.value.snapshot["text"] == "电脑版本"
    assert store.snapshot("board-1")["text"] == "电脑版本"


def test_board_store_rejects_oversized_text_and_allocates_numbers(tmp_path):
    store = BoardStore(tmp_path / "state.db")

    with pytest.raises(InvalidDocument):
        store.update("board-1", 0, "中" * 400_000)

    assert store.snapshot("board-1")["revision"] == 0
    created = store.create_board()
    assert created["id"].startswith("board-4-")
    assert created["name"] == "板 4"


def test_board_store_renames_board_and_persists_name(tmp_path):
    path = tmp_path / "state.db"
    store = BoardStore(path)

    renamed = store.rename_board("board-2", "  工作   提示词  ")

    assert renamed["name"] == "工作 提示词"
    assert renamed["revision"] == 0
    assert BoardStore(path).snapshot("board-2")["name"] == "工作 提示词"

    with pytest.raises(InvalidBoardName):
        store.rename_board("board-2", "   ")
    with pytest.raises(InvalidBoardName):
        store.rename_board("board-2", "名" * 25)


def test_board_store_limits_deletes_and_reuses_free_number(tmp_path):
    store = BoardStore(tmp_path / "state.db")
    while len(store.list_boards()) < MAX_BOARDS:
        store.create_board()

    with pytest.raises(BoardLimitReached):
        store.create_board()

    deleted = store.delete_board("board-2")
    assert deleted["deletedId"] == "board-2"
    assert deleted["fallback"]["name"] == "板 3"

    replacement = store.create_board()
    assert replacement["name"] == "板 2"
    assert replacement["id"].startswith("board-2-")
    assert replacement["id"] != "board-2"

    for board in list(store.list_boards())[1:]:
        store.delete_board(board["id"])
    with pytest.raises(LastBoardDeletion):
        store.delete_board(store.first_board_id())


def test_auth_store_persists_only_hash_and_supports_revocation(tmp_path):
    path = tmp_path / "auth.json"
    store = AuthStore(path)

    device, token = store.create_device("  我的   手机  ")

    assert device["name"] == "我的 手机"
    assert store.authenticate(token, touch=False)["id"] == device["id"]
    serialized = path.read_text(encoding="utf-8")
    assert token not in serialized
    assert "tokenHash" in serialized

    reloaded = AuthStore(path)
    assert reloaded.authenticate(token, touch=False)["name"] == "我的 手机"
    assert reloaded.revoke(device["id"]) is True
    assert reloaded.authenticate(token, touch=False) is None


def test_pairing_code_is_single_use_and_expires(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr("easytype_app.time.time", lambda: now[0])
    manager = PairingManager(ttl_seconds=10)

    first = manager.create()
    assert manager.get(first.value) is not None
    assert manager.consume(first.value) is True
    assert manager.consume(first.value) is False

    second = manager.create()
    now[0] += 11
    assert manager.get(second.value) is None


class FakeSocket:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, message):
        self.messages.append(json.loads(message))

    def close(self, **_kwargs):
        self.closed = True


def test_sync_hub_broadcast_and_immediate_device_revocation():
    hub = SyncHub()
    first = FakeSocket()
    second = FakeSocket()
    first_id = hub.register(first, "device-one", "board-1")
    hub.register(second, "device-two", "board-2")

    hub.broadcast({"type": "update", "value": 1}, exclude=first_id)

    assert first.messages == []
    assert second.messages == [{"type": "update", "value": 1}]

    hub.revoke_device("device-two")
    assert second.closed is True


class FakeDirectActions:
    def __init__(self):
        self.calls = []
        self.keys = []

    def capture_target(self):
        return 123

    def direct_input(self, target, delete_count, text):
        self.calls.append((target, delete_count, text))

    def direct_key(self, target, key):
        self.keys.append((target, key))


def test_direct_input_manager_deduplicates_and_serializes_operations():
    actions = FakeDirectActions()
    manager = DirectInputManager(actions)
    session = manager.begin(
        connection_id="connection-one",
        device_id="device-one",
        device_name="手机",
    )

    first = manager.apply(
        connection_id="connection-one",
        session_id=session["id"],
        sequence=1,
        delete_count=0,
        text="语音输入",
    )
    duplicate = manager.apply(
        connection_id="connection-one",
        session_id=session["id"],
        sequence=1,
        delete_count=0,
        text="不应再次输入",
    )

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert actions.calls == [(123, 0, "语音输入")]

    with pytest.raises(DirectInputFailure) as gap:
        manager.apply(
            connection_id="connection-one",
            session_id=session["id"],
            sequence=3,
            delete_count=0,
            text="跳号",
        )
    assert gap.value.code == "sequence_gap"
    key_acknowledgement = manager.apply_key(
        connection_id="connection-one",
        session_id=session["id"],
        request_id="key-one",
        key="enter",
    )
    assert key_acknowledgement["key"] == "enter"
    assert actions.keys == [(123, "enter")]
    assert manager.stop(connection_id="connection-one") is True


def test_direct_input_manager_allows_only_one_device_at_a_time():
    manager = DirectInputManager(FakeDirectActions())
    first = manager.begin(
        connection_id="connection-one",
        device_id="device-one",
        device_name="第一台手机",
    )

    with pytest.raises(DirectInputFailure) as busy:
        manager.begin(
            connection_id="connection-two",
            device_id="device-two",
            device_name="第二台手机",
        )

    assert first["id"]
    assert busy.value.code == "direct_busy"
    assert manager.stop(connection_id="connection-two") is False
    assert manager.stop(connection_id="connection-one") is True


@pytest.mark.skipif(not hasattr(ctypes, "windll"), reason="Windows only")
def test_windows_direct_input_uses_native_input_structure_size(monkeypatch):
    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    observed = {}

    def send_input(count, _inputs, structure_size):
        observed["count"] = count
        observed["structureSize"] = structure_size
        return count

    fake_user32 = type(
        "FakeUser32",
        (),
        {
            "GetForegroundWindow": FakeFunction(lambda: 123),
            "SendInput": FakeFunction(send_input),
        },
    )()
    monkeypatch.setattr(ctypes.windll, "user32", fake_user32)

    WindowsActions().direct_input(123, 0, "A")

    assert observed["count"] == 2
    assert observed["structureSize"] == (
        40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    )


@pytest.mark.skipif(not hasattr(ctypes, "windll"), reason="Windows only")
def test_windows_direct_input_uses_fast_standard_control_insertion(monkeypatch):
    actions = WindowsActions()
    observed = []
    monkeypatch.setattr(actions, "_focused_window", lambda: 123)
    monkeypatch.setattr(actions, "_standard_text_control", lambda target: 456)
    monkeypatch.setattr(
        actions,
        "_replace_standard_text",
        lambda control, text: observed.append((control, text)) or True,
    )

    actions.direct_input(123, 0, "记事本快速输入")

    assert observed == [(456, "记事本快速输入")]
