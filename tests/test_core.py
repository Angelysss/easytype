import json

import pytest

from easytype_app import (
    ACCESS_MODE_PAIRING,
    ACCESS_MODE_TRUSTED_LAN,
    AccessSettingsStore,
    AuthStore,
    DocumentStore,
    InvalidDocument,
    PairingManager,
    RevisionConflict,
    SyncHub,
)


def test_access_settings_store_defaults_to_pairing_and_persists(tmp_path):
    path = tmp_path / "settings.json"
    store = AccessSettingsStore(path)

    assert store.mode() == ACCESS_MODE_PAIRING
    assert store.set_mode(ACCESS_MODE_TRUSTED_LAN) == ACCESS_MODE_TRUSTED_LAN
    assert AccessSettingsStore(path).mode() == ACCESS_MODE_TRUSTED_LAN

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {
        "version": 1,
        "accessMode": ACCESS_MODE_TRUSTED_LAN,
    }

    with pytest.raises(ValueError):
        store.set_mode("unknown")


def test_document_store_persists_and_reloads(tmp_path):
    path = tmp_path / "state.json"
    store = DocumentStore(path)

    updated = store.update(0, "手机和电脑共享的内容")

    assert updated["revision"] == 1
    assert DocumentStore(path).snapshot() == updated
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["text"] == "手机和电脑共享的内容"
    assert saved["version"] == 1


def test_document_store_rejects_stale_revision_without_losing_text(tmp_path):
    store = DocumentStore(tmp_path / "state.json")
    store.update(0, "电脑版本")

    with pytest.raises(RevisionConflict) as captured:
        store.update(0, "手机的过期版本")

    assert captured.value.snapshot["text"] == "电脑版本"
    assert store.snapshot()["text"] == "电脑版本"


def test_document_store_rejects_oversized_text(tmp_path):
    store = DocumentStore(tmp_path / "state.json")

    with pytest.raises(InvalidDocument):
        store.update(0, "中" * 400_000)

    assert store.snapshot()["revision"] == 0


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
    first_id = hub.register(first, "device-one")
    hub.register(second, "device-two")

    hub.broadcast({"type": "update", "value": 1}, exclude=first_id)

    assert first.messages == []
    assert second.messages == [{"type": "update", "value": 1}]

    hub.revoke_device("device-two")
    assert second.closed is True
