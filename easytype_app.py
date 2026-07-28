from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_sock import Sock


APP_VERSION = "1.0.1"
COOKIE_NAME = "easytype_session"
COOKIE_MAX_AGE_SECONDS = 180 * 24 * 60 * 60
MAX_TEXT_BYTES = 1024 * 1024
MAX_SOCKET_MESSAGE_BYTES = MAX_TEXT_BYTES + 64 * 1024
PAIRING_CODE_TTL_SECONDS = 5 * 60
ACCESS_MODE_PAIRING = "pairing"
ACCESS_MODE_TRUSTED_LAN = "trusted_lan"
ACCESS_MODES = frozenset({ACCESS_MODE_PAIRING, ACCESS_MODE_TRUSTED_LAN})
TRUSTED_LAN_DEVICE_ID = "trusted-lan"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_data_dir() -> Path:
    configured = os.environ.get("EASYTYPE_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "EasyType"
    return Path.home() / ".easytype"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


class RevisionConflict(Exception):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__("The document has changed on another device.")
        self.snapshot = snapshot


class InvalidDocument(Exception):
    pass


class DocumentStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "text": "",
            "revision": 0,
            "updatedAt": utc_now(),
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            text = payload["text"]
            revision = payload["revision"]
            updated_at = payload["updatedAt"]
            if not isinstance(text, str) or not isinstance(revision, int):
                raise ValueError("invalid document fields")
            if not isinstance(updated_at, str):
                raise ValueError("invalid document timestamp")
            self._validate_text(text)
            self._state = {
                "text": text,
                "revision": max(0, revision),
                "updatedAt": updated_at,
            }
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logging.getLogger(__name__).warning(
                "Saved document could not be loaded; starting with an empty document."
            )

    @staticmethod
    def _validate_text(text: Any) -> None:
        if not isinstance(text, str):
            raise InvalidDocument("Text must be a string.")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise InvalidDocument("Text exceeds the 1 MiB limit.")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def update(self, base_revision: Any, text: Any) -> dict[str, Any]:
        self._validate_text(text)
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            raise InvalidDocument("baseRevision must be an integer.")

        with self._lock:
            if base_revision != self._state["revision"]:
                raise RevisionConflict(dict(self._state))
            if text == self._state["text"]:
                return dict(self._state)

            self._state = {
                "text": text,
                "revision": self._state["revision"] + 1,
                "updatedAt": utc_now(),
            }
            _write_json_atomic(
                self.path,
                {
                    "version": 1,
                    **self._state,
                },
            )
            return dict(self._state)


class AccessSettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._mode = ACCESS_MODE_PAIRING
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            mode = payload.get("accessMode")
            if mode not in ACCESS_MODES:
                raise ValueError("invalid access mode")
            self._mode = mode
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logging.getLogger(__name__).warning(
                "Saved access settings could not be loaded; using pairing mode."
            )

    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: Any) -> str:
        if mode not in ACCESS_MODES:
            raise ValueError("invalid access mode")
        with self._lock:
            if mode == self._mode:
                return self._mode
            self._mode = mode
            _write_json_atomic(
                self.path,
                {
                    "version": 1,
                    "accessMode": self._mode,
                },
            )
            return self._mode


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._devices: dict[str, dict[str, Any]] = {}
        self._last_persisted_touch: dict[str, float] = {}
        self._load()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            devices = payload.get("devices", [])
            if not isinstance(devices, list):
                raise ValueError("invalid device list")
            loaded: dict[str, dict[str, Any]] = {}
            for device in devices:
                if not isinstance(device, dict):
                    continue
                device_id = device.get("id")
                token_hash = device.get("tokenHash")
                name = device.get("name")
                if all(isinstance(item, str) and item for item in (device_id, token_hash, name)):
                    loaded[device_id] = {
                        "id": device_id,
                        "name": name[:80],
                        "tokenHash": token_hash,
                        "createdAt": str(device.get("createdAt") or utc_now()),
                        "lastSeen": str(device.get("lastSeen") or utc_now()),
                    }
            self._devices = loaded
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logging.getLogger(__name__).warning(
                "Saved device authorizations could not be loaded."
            )

    def _persist(self) -> None:
        _write_json_atomic(
            self.path,
            {
                "version": 1,
                "devices": list(self._devices.values()),
            },
        )

    def create_device(self, name: str) -> tuple[dict[str, Any], str]:
        cleaned_name = " ".join(name.strip().split())[:80] or "我的设备"
        token = secrets.token_urlsafe(32)
        now = utc_now()
        device = {
            "id": str(uuid.uuid4()),
            "name": cleaned_name,
            "tokenHash": self._token_hash(token),
            "createdAt": now,
            "lastSeen": now,
        }
        with self._lock:
            self._devices[device["id"]] = device
            self._persist()
        return self._public_device(device), token

    def authenticate(self, token: str | None, *, touch: bool = True) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = self._token_hash(token)
        with self._lock:
            matched = next(
                (
                    device
                    for device in self._devices.values()
                    if secrets.compare_digest(device["tokenHash"], token_hash)
                ),
                None,
            )
            if matched is None:
                return None
            if touch:
                self._touch_locked(matched)
            return self._public_device(matched)

    def _touch_locked(self, device: dict[str, Any]) -> None:
        now_monotonic = time.monotonic()
        last_write = self._last_persisted_touch.get(device["id"], 0)
        if now_monotonic - last_write < 60:
            return
        device["lastSeen"] = utc_now()
        self._last_persisted_touch[device["id"]] = now_monotonic
        self._persist()

    def touch(self, device_id: str) -> bool:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return False
            self._touch_locked(device)
            return True

    def is_active(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._devices

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._public_device(device)
                for device in sorted(
                    self._devices.values(),
                    key=lambda item: item["createdAt"],
                )
            ]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            removed = self._devices.pop(device_id, None)
            self._last_persisted_touch.pop(device_id, None)
            if removed is None:
                return False
            self._persist()
            return True

    @staticmethod
    def _public_device(device: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": device["id"],
            "name": device["name"],
            "createdAt": device["createdAt"],
            "lastSeen": device["lastSeen"],
        }


@dataclass(frozen=True)
class PairingCode:
    value: str
    expires_at: float

    @property
    def expires_at_iso(self) -> str:
        return datetime.fromtimestamp(self.expires_at, UTC).isoformat(timespec="seconds")


class PairingManager:
    def __init__(self, ttl_seconds: int = PAIRING_CODE_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._codes: dict[str, float] = {}

    def _purge(self) -> None:
        now = time.time()
        expired = [code for code, expiry in self._codes.items() if expiry <= now]
        for code in expired:
            self._codes.pop(code, None)

    def create(self) -> PairingCode:
        with self._lock:
            self._purge()
            value = secrets.token_urlsafe(24)
            expires_at = time.time() + self.ttl_seconds
            self._codes[value] = expires_at
            return PairingCode(value=value, expires_at=expires_at)

    def get(self, value: str | None) -> PairingCode | None:
        if not value:
            return None
        with self._lock:
            self._purge()
            expires_at = self._codes.get(value)
            if expires_at is None:
                return None
            return PairingCode(value=value, expires_at=expires_at)

    def consume(self, value: str | None) -> bool:
        if not value:
            return False
        with self._lock:
            self._purge()
            return self._codes.pop(value, None) is not None


class WindowsActions:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def paste(self, text: str) -> None:
        import pyautogui
        import pyperclip

        with self._lock:
            pyperclip.copy(text)
            time.sleep(0.08)
            pyautogui.hotkey("ctrl", "v")


class SyncHub:
    def __init__(self):
        self._lock = threading.RLock()
        self._connections: dict[str, tuple[Any, str]] = {}

    def register(self, websocket: Any, device_id: str) -> str:
        connection_id = str(uuid.uuid4())
        with self._lock:
            self._connections[connection_id] = (websocket, device_id)
        return connection_id

    def unregister(self, connection_id: str) -> None:
        with self._lock:
            self._connections.pop(connection_id, None)

    def broadcast(self, message: dict[str, Any], *, exclude: str | None = None) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            recipients = [
                (connection_id, websocket)
                for connection_id, (websocket, _device_id) in self._connections.items()
                if connection_id != exclude
            ]
        stale: list[str] = []
        for connection_id, websocket in recipients:
            try:
                websocket.send(encoded)
            except Exception:
                stale.append(connection_id)
        for connection_id in stale:
            self.unregister(connection_id)

    def revoke_device(self, device_id: str) -> None:
        with self._lock:
            matches = [
                (connection_id, websocket)
                for connection_id, (websocket, current_device_id) in self._connections.items()
                if current_device_id == device_id
            ]
        for connection_id, websocket in matches:
            try:
                websocket.close(reason=1008, message="Device authorization revoked.")
            except Exception:
                pass
            self.unregister(connection_id)

    def close_all(self) -> None:
        with self._lock:
            connections = list(self._connections.items())
        for connection_id, (websocket, _device_id) in connections:
            try:
                websocket.close(reason=1001, message="EasyType is shutting down.")
            except Exception:
                pass
            self.unregister(connection_id)


def _client_ip() -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    raw = request.remote_addr
    if not raw:
        return None
    try:
        parsed = ipaddress.ip_address(raw)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            return parsed.ipv4_mapped
        return parsed
    except ValueError:
        return None


def _is_loopback_request() -> bool:
    client = _client_ip()
    return bool(client and client.is_loopback)


def _is_private_request() -> bool:
    client = _client_ip()
    return bool(
        client
        and (client.is_private or client.is_loopback or client.is_link_local)
    )


def _is_same_origin() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return _is_loopback_request()
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.netloc.casefold() == request.host.casefold()
        )
    except ValueError:
        return False


def _is_safe_pairing_source() -> bool:
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        return _is_same_origin()

    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site:
        return fetch_site.casefold() in {"same-origin", "none"}

    # The random, single-use pairing code is also the CSRF token for this
    # one endpoint. Some mobile QR browsers omit both Origin and Fetch
    # Metadata on a regular form POST, so allow that case without weakening
    # the origin requirement for any authenticated mutation.
    return True


def get_local_ipv4_addresses() -> list[str]:
    candidates: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        candidates.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        candidates.extend(
            item[4][0]
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        )
    except OSError:
        pass

    addresses: list[str] = []
    for value in candidates:
        try:
            parsed = ipaddress.ip_address(value)
            if (
                isinstance(parsed, ipaddress.IPv4Address)
                and parsed.is_private
                and not parsed.is_loopback
                and value not in addresses
            ):
                addresses.append(value)
        except ValueError:
            continue
    return addresses


def bundled_resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            return Path(bundle_dir)
    return Path(__file__).resolve().parent


def create_app(
    *,
    data_dir: Path | None = None,
    port: int = 5000,
    action_backend: Any | None = None,
    testing: bool = False,
) -> Flask:
    resource_dir = bundled_resource_dir()
    app = Flask(
        __name__,
        static_folder=str(resource_dir / "static"),
        template_folder=str(resource_dir / "templates"),
    )
    app.config.update(
        TESTING=testing,
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=MAX_SOCKET_MESSAGE_BYTES,
        EASYTYPE_PORT=port,
    )
    app.json.ensure_ascii = False

    resolved_data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    document_store = DocumentStore(resolved_data_dir / "state.json")
    access_settings = AccessSettingsStore(resolved_data_dir / "settings.json")
    auth_store = AuthStore(resolved_data_dir / "auth.json")
    pairing_manager = PairingManager()
    sync_hub = SyncHub()
    actions = action_backend or WindowsActions()

    app.extensions["easytype_document"] = document_store
    app.extensions["easytype_access_settings"] = access_settings
    app.extensions["easytype_auth"] = auth_store
    app.extensions["easytype_pairing"] = pairing_manager
    app.extensions["easytype_hub"] = sync_hub
    app.extensions["easytype_actions"] = actions

    sock = Sock(app)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.before_request
    def restrict_network() -> Response | None:
        if not _is_private_request():
            return jsonify_error("network_denied", "仅允许从家庭局域网访问。", 403)
        return None

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        # Some Windows installations register .js as text/plain. Combined
        # with nosniff that makes browsers correctly refuse to execute our
        # local scripts, so enforce the web-standard static MIME types here.
        if request.path.startswith("/static/"):
            if request.path.endswith(".js"):
                response.headers["Content-Type"] = "application/javascript; charset=utf-8"
            elif request.path.endswith(".css"):
                response.headers["Content-Type"] = "text/css; charset=utf-8"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:; "
            "script-src 'self'; "
            "style-src 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        )
        return response

    def current_device(*, touch: bool = True) -> dict[str, Any] | None:
        if _is_loopback_request():
            return {
                "id": "local",
                "name": "这台电脑",
                "createdAt": "",
                "lastSeen": utc_now(),
            }
        paired_device = auth_store.authenticate(
            request.cookies.get(COOKIE_NAME),
            touch=touch,
        )
        if paired_device is not None:
            return paired_device
        if access_settings.mode() == ACCESS_MODE_TRUSTED_LAN:
            return {
                "id": TRUSTED_LAN_DEVICE_ID,
                "name": "可信局域网设备",
                "createdAt": "",
                "lastSeen": utc_now(),
            }
        return None

    def require_auth(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            device = current_device()
            if device is None:
                return jsonify_error("unauthorized", "请先在电脑端配对此设备。", 401)
            return view(*args, **kwargs)

        return wrapped

    def require_local(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not _is_loopback_request():
                return jsonify_error("local_only", "此功能只能在电脑本机使用。", 403)
            return view(*args, **kwargs)

        return wrapped

    def require_mutation_origin(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not _is_same_origin():
                return jsonify_error("origin_denied", "请求来源无效。", 403)
            return view(*args, **kwargs)

        return wrapped

    def require_pairing_mode(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if access_settings.mode() != ACCESS_MODE_PAIRING:
                if request.path.startswith("/api/"):
                    return jsonify_error(
                        "pairing_mode_required",
                        "请先在访问设置中切换到配对模式。",
                        409,
                    )
                return redirect(url_for("index"))
            return view(*args, **kwargs)

        return wrapped

    @app.get("/")
    def index() -> Any:
        if current_device() is None:
            return redirect(url_for("pair_device"))
        is_local = _is_loopback_request()
        addresses = get_local_ipv4_addresses() if is_local else []
        lan_url = f"http://{addresses[0]}:{port}" if addresses else None
        page_url = lan_url or f"{request.scheme}://{request.host}"
        return render_template(
            "index.html",
            max_text_bytes=MAX_TEXT_BYTES,
            is_local=is_local,
            access_mode=access_settings.mode(),
            lan_url=lan_url,
            page_url=page_url,
            app_version=APP_VERSION,
        )

    @app.get("/pair")
    def pair_device() -> Any:
        if current_device(touch=False) is not None:
            return redirect(url_for("index"))
        pairing_code = pairing_manager.get(request.args.get("code"))
        return render_template(
            "pair.html",
            pairing_code=pairing_code.value if pairing_code else None,
            expires_at=pairing_code.expires_at_iso if pairing_code else None,
        )

    @app.post("/api/pair")
    def complete_pairing() -> Any:
        if access_settings.mode() != ACCESS_MODE_PAIRING:
            return redirect(url_for("index"))
        if not _is_safe_pairing_source():
            return jsonify_error("origin_denied", "请求来源无效。", 403)
        code = request.form.get("code")
        device_name = request.form.get("deviceName", "")
        if not pairing_manager.consume(code):
            return (
                render_template(
                    "pair.html",
                    pairing_code=None,
                    expires_at=None,
                    error="配对链接无效、已使用或已过期，请在电脑端重新生成。",
                ),
                400,
            )
        _device, token = auth_store.create_device(device_name)
        response = make_response(redirect(url_for("index")))
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            secure=False,
            samesite="Strict",
            path="/",
        )
        return response

    @app.get("/api/info")
    @require_auth
    def api_info() -> Any:
        snapshot = document_store.snapshot()
        return jsonify(
            {
                "ok": True,
                "version": APP_VERSION,
                "status": "online",
                "revision": snapshot["revision"],
                "updatedAt": snapshot["updatedAt"],
                "accessMode": access_settings.mode(),
            }
        )

    @app.get("/api/document")
    @require_auth
    def api_document() -> Any:
        return jsonify({"ok": True, "document": document_store.snapshot()})

    @app.post("/api/actions/paste")
    @require_auth
    @require_mutation_origin
    def paste_document() -> Any:
        if _is_loopback_request():
            return jsonify_error(
                "remote_only",
                "插入功能仅在手机端可用。",
                403,
            )
        try:
            actions.paste(document_store.snapshot()["text"])
            return jsonify({"ok": True})
        except Exception:
            app.logger.exception("Paste action failed.")
            return jsonify_error("paste_failed", "插入到电脑当前窗口失败。", 500)

    @app.get("/admin")
    @require_local
    @require_pairing_mode
    def admin() -> Any:
        return render_template("admin.html")

    @app.put("/api/admin/access-mode")
    @require_local
    @require_mutation_origin
    def update_access_mode() -> Any:
        payload = request.get_json(silent=True)
        mode = payload.get("mode") if isinstance(payload, dict) else None
        if mode not in ACCESS_MODES:
            return jsonify_error(
                "invalid_access_mode",
                "访问模式无效。",
                400,
            )
        previous_mode = access_settings.mode()
        access_settings.set_mode(mode)
        if (
            previous_mode == ACCESS_MODE_TRUSTED_LAN
            and mode == ACCESS_MODE_PAIRING
        ):
            sync_hub.revoke_device(TRUSTED_LAN_DEVICE_ID)
        return jsonify({"ok": True, "mode": mode})

    @app.get("/api/admin/trusted-lan-qr")
    @require_local
    def trusted_lan_qr() -> Any:
        addresses = get_local_ipv4_addresses()
        if not addresses:
            abort(404)
        import qrcode

        image = qrcode.make(f"http://{addresses[0]}:{port}")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return Response(output.getvalue(), mimetype="image/png")

    @app.post("/api/admin/pairing-codes")
    @require_local
    @require_pairing_mode
    @require_mutation_origin
    def create_pairing_code() -> Any:
        addresses = get_local_ipv4_addresses()
        if not addresses:
            return jsonify_error(
                "no_lan_address",
                "未找到可用的家庭局域网 IPv4 地址。",
                503,
            )
        pairing_code = pairing_manager.create()
        base_url = f"http://{addresses[0]}:{port}"
        pair_url = f"{base_url}/pair?code={pairing_code.value}"
        return jsonify(
            {
                "ok": True,
                "pairUrl": pair_url,
                "expiresAt": pairing_code.expires_at_iso,
                "qrUrl": url_for(
                    "pairing_qr",
                    code=pairing_code.value,
                    _external=False,
                ),
            }
        )

    @app.get("/api/admin/pairing-qr")
    @require_local
    @require_pairing_mode
    def pairing_qr() -> Any:
        pairing_code = pairing_manager.get(request.args.get("code"))
        addresses = get_local_ipv4_addresses()
        if pairing_code is None or not addresses:
            abort(404)
        import qrcode

        pair_url = (
            f"http://{addresses[0]}:{port}/pair?code={pairing_code.value}"
        )
        image = qrcode.make(pair_url)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return Response(output.getvalue(), mimetype="image/png")

    @app.get("/api/admin/devices")
    @require_local
    @require_pairing_mode
    def list_devices() -> Any:
        return jsonify({"ok": True, "devices": auth_store.list_devices()})

    @app.delete("/api/admin/devices/<device_id>")
    @require_local
    @require_pairing_mode
    @require_mutation_origin
    def revoke_device(device_id: str) -> Any:
        if not auth_store.revoke(device_id):
            return jsonify_error("not_found", "未找到该设备。", 404)
        sync_hub.revoke_device(device_id)
        return jsonify({"ok": True})

    @sock.route("/ws")
    def websocket_endpoint(websocket: Any) -> None:
        if not _is_private_request():
            websocket.close(reason=1008, message="Private network access required.")
            return
        if not _is_same_origin():
            websocket.close(reason=1008, message="Same-origin access required.")
            return
        device = current_device()
        if device is None:
            websocket.close(reason=1008, message="Pairing required.")
            return

        connection_id = sync_hub.register(websocket, device["id"])
        try:
            websocket.send(
                json.dumps(
                    {
                        "type": "snapshot",
                        "document": document_store.snapshot(),
                        "deviceId": device["id"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            while True:
                raw = websocket.receive()
                if raw is None:
                    break
                if not isinstance(raw, str):
                    send_socket_error(websocket, "invalid_message", "只支持文本消息。")
                    continue
                if len(raw.encode("utf-8")) > MAX_SOCKET_MESSAGE_BYTES:
                    send_socket_error(websocket, "message_too_large", "消息超过大小限制。")
                    continue
                if (
                    device["id"] == TRUSTED_LAN_DEVICE_ID
                    and access_settings.mode() != ACCESS_MODE_TRUSTED_LAN
                ):
                    websocket.close(reason=1008, message="Pairing is now required.")
                    break
                if (
                    device["id"] not in {"local", TRUSTED_LAN_DEVICE_ID}
                    and not auth_store.is_active(device["id"])
                ):
                    websocket.close(reason=1008, message="Device authorization revoked.")
                    break
                if device["id"] not in {"local", TRUSTED_LAN_DEVICE_ID}:
                    auth_store.touch(device["id"])

                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    send_socket_error(websocket, "invalid_json", "消息不是有效 JSON。")
                    continue
                if not isinstance(message, dict):
                    send_socket_error(websocket, "invalid_message", "消息格式无效。")
                    continue
                message_type = message.get("type")
                if message_type == "ping":
                    websocket.send('{"type":"pong"}')
                    continue
                if message_type != "update":
                    send_socket_error(websocket, "unknown_message", "未知消息类型。")
                    continue

                try:
                    updated = document_store.update(
                        message.get("baseRevision"),
                        message.get("text"),
                    )
                except RevisionConflict as conflict:
                    websocket.send(
                        json.dumps(
                            {
                                "type": "conflict",
                                "document": conflict.snapshot,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    continue
                except InvalidDocument as error:
                    send_socket_error(websocket, "invalid_document", str(error))
                    continue

                websocket.send(
                    json.dumps(
                        {
                            "type": "ack",
                            "revision": updated["revision"],
                            "updatedAt": updated["updatedAt"],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                sync_hub.broadcast(
                    {
                        "type": "update",
                        "document": updated,
                        "sourceId": str(message.get("clientId") or ""),
                    },
                    exclude=connection_id,
                )
        except Exception:
            app.logger.debug("WebSocket disconnected.", exc_info=True)
        finally:
            sync_hub.unregister(connection_id)

    @app.errorhandler(413)
    def request_too_large(_error: Any) -> Any:
        return jsonify_error("request_too_large", "请求超过大小限制。", 413)

    @app.errorhandler(404)
    def not_found(_error: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify_error("not_found", "接口不存在。", 404)
        return render_template("error.html", message="页面不存在。"), 404

    return app


def send_socket_error(websocket: Any, code: str, message: str) -> None:
    websocket.send(
        json.dumps(
            {
                "type": "error",
                "error": {
                    "code": code,
                    "message": message,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def jsonify_error(code: str, message: str, status: int) -> tuple[Response, int]:
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                },
            }
        ),
        status,
    )
