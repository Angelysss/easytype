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
        assert receive_type(first, "snapshot")["document"]["revision"] == 0
        assert receive_type(second, "snapshot")["document"]["revision"] == 0

        first.send(json.dumps({
            "type": "update",
            "clientId": "computer",
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
            "baseRevision": 0,
            "text": "手机基于过期版本输入",
        }))
        conflict = receive_type(second, "conflict")

        assert conflict["document"]["revision"] == 1
        assert conflict["document"]["text"] == "电脑输入的第一版"
        assert app.extensions["easytype_document"].snapshot()["text"] == "电脑输入的第一版"
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
