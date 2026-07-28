import json
import os
import threading

import websocket
from werkzeug.serving import make_server

from easytype_app import create_app


def receive_type(connection, expected_type):
    for _attempt in range(5):
        message = json.loads(connection.recv())
        if message["type"] == expected_type:
            return message
    raise AssertionError(f"Did not receive WebSocket message type {expected_type!r}")


def test_two_clients_sync_and_stale_update_conflicts(tmp_path):
    app = create_app(data_dir=tmp_path, testing=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    url = f"ws://127.0.0.1:{port}/ws"

    previous_no_proxy = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    first = second = None
    try:
        first = websocket.create_connection(url, timeout=3)
        second = websocket.create_connection(url, timeout=3)
        first_snapshot = receive_type(first, "snapshot")
        second_snapshot = receive_type(second, "snapshot")
        assert first_snapshot["document"]["revision"] == 0
        assert second_snapshot["document"]["revision"] == 0
        assert [board["name"] for board in first_snapshot["boards"]] == [
            "板 1",
            "板 2",
            "板 3",
        ]
        assert "workMode" not in first_snapshot

        first.send(json.dumps({
            "type": "update",
            "clientId": "computer",
            "boardId": "board-1",
            "baseRevision": 0,
            "text": "电脑输入的第一版",
        }))
        acknowledgement = receive_type(first, "ack")
        broadcast = receive_type(second, "update")

        assert acknowledgement["revision"] == 1
        assert broadcast["document"]["text"] == "电脑输入的第一版"

        second.send(json.dumps({
            "type": "update",
            "clientId": "phone",
            "boardId": "board-1",
            "baseRevision": 0,
            "text": "手机基于过期版本输入",
        }))
        conflict = receive_type(second, "conflict")

        assert conflict["document"]["revision"] == 1
        assert conflict["document"]["text"] == "电脑输入的第一版"
        assert app.extensions["easytype_boards"].snapshot("board-1")["text"] == (
            "电脑输入的第一版"
        )

        first.send(json.dumps({
            "type": "create_board",
            "clientId": "computer",
        }))
        created = receive_type(first, "board_created")
        assert created["board"]["name"] == "板 4"
        board_four_id = created["board"]["id"]
        assert board_four_id.startswith("board-4-")
        assert receive_type(second, "board_created")["board"]["id"] == board_four_id

        first.send(json.dumps({
            "type": "rename_board",
            "boardId": board_four_id,
            "name": "语音草稿",
            "clientId": "computer",
        }))
        first_renamed = receive_type(first, "board_renamed")
        second_renamed = receive_type(second, "board_renamed")
        assert first_renamed["board"]["name"] == "语音草稿"
        assert first_renamed["sourceId"] == "computer"
        assert second_renamed["board"]["id"] == board_four_id

        second.send(json.dumps({
            "type": "select_board",
            "boardId": board_four_id,
        }))
        selected = receive_type(second, "board_snapshot")
        assert selected["document"]["id"] == board_four_id

        first.send(json.dumps({
            "type": "delete_board",
            "boardId": board_four_id,
            "clientId": "computer",
        }))
        first_deleted = receive_type(first, "board_deleted")
        second_deleted = receive_type(second, "board_deleted")
        assert first_deleted["boardId"] == board_four_id
        assert first_deleted["fallback"]["name"] == "板 3"
        assert second_deleted["fallback"]["id"] == first_deleted["fallback"]["id"]
    finally:
        for connection in (first, second):
            if connection is not None:
                connection.close()
        app.extensions["easytype_hub"].close_all()
        server.shutdown()
        thread.join(timeout=5)
        if previous_no_proxy is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = previous_no_proxy
