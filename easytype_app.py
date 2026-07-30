from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import logging
import os
import secrets
import socket
import sqlite3
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


APP_VERSION = "1.3.0"
COOKIE_NAME = "easytype_session"
COOKIE_MAX_AGE_SECONDS = 180 * 24 * 60 * 60
MAX_TEXT_BYTES = 1024 * 1024
MAX_BOARDS = 8
MAX_SOCKET_MESSAGE_BYTES = MAX_TEXT_BYTES + 64 * 1024
MAX_DIRECT_TEXT_BYTES = 64 * 1024
MAX_DIRECT_DELETE_COUNT = 4096
DIRECT_SESSION_IDLE_SECONDS = 3 * 60
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


class BoardNotFound(Exception):
    pass


class BoardLimitReached(Exception):
    pass


class LastBoardDeletion(Exception):
    pass


class InvalidBoardName(Exception):
    pass


class BoardStore:
    def __init__(self, path: Path, *, legacy_path: Path | None = None):
        self.path = path
        self.legacy_path = legacy_path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS boards (
                    id TEXT PRIMARY KEY,
                    number INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            count = connection.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
            if count:
                return

            legacy = self._load_legacy_document()
            now = utc_now()
            for number in range(1, 4):
                text = legacy["text"] if number == 1 else ""
                revision = legacy["revision"] if number == 1 else 0
                updated_at = legacy["updatedAt"] if number == 1 else now
                connection.execute(
                    """
                    INSERT INTO boards (id, number, name, text, revision, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"board-{number}",
                        number,
                        f"板 {number}",
                        text,
                        revision,
                        updated_at,
                    ),
                )

    def _load_legacy_document(self) -> dict[str, Any]:
        empty = {"text": "", "revision": 0, "updatedAt": utc_now()}
        if self.legacy_path is None or not self.legacy_path.exists():
            return empty
        try:
            payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            text = payload["text"]
            revision = payload["revision"]
            updated_at = payload["updatedAt"]
            self._validate_text(text)
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or not isinstance(updated_at, str)
            ):
                raise ValueError("invalid legacy document")
            return {
                "text": text,
                "revision": max(0, revision),
                "updatedAt": updated_at,
            }
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logging.getLogger(__name__).warning(
                "Saved document could not be migrated; creating empty boards."
            )
            return empty

    @staticmethod
    def _validate_text(text: Any) -> None:
        if not isinstance(text, str):
            raise InvalidDocument("Text must be a string.")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise InvalidDocument("Text exceeds the 1 MiB limit.")

    @staticmethod
    def _board_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "number": row["number"],
            "name": row["name"],
            "text": row["text"],
            "revision": row["revision"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _metadata_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "number": row["number"],
            "name": row["name"],
            "revision": row["revision"],
            "updatedAt": row["updated_at"],
        }

    def list_boards(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, number, name, revision, updated_at
                FROM boards
                ORDER BY number
                """
            ).fetchall()
            return [self._metadata_from_row(row) for row in rows]

    def first_board_id(self) -> str:
        boards = self.list_boards()
        if not boards:
            raise BoardNotFound("No boards are available.")
        return boards[0]["id"]

    def snapshot(self, board_id: Any = None) -> dict[str, Any]:
        if board_id is None:
            board_id = self.first_board_id()
        if not isinstance(board_id, str):
            raise BoardNotFound("Board does not exist.")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, number, name, text, revision, updated_at
                FROM boards
                WHERE id = ?
                """,
                (board_id,),
            ).fetchone()
            if row is None:
                raise BoardNotFound("Board does not exist.")
            return self._board_from_row(row)

    def create_board(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_numbers = {
                row[0]
                for row in connection.execute(
                    "SELECT number FROM boards ORDER BY number"
                ).fetchall()
            }
            if len(existing_numbers) >= MAX_BOARDS:
                raise BoardLimitReached("At most eight boards are allowed.")
            next_number = next(
                number
                for number in range(1, MAX_BOARDS + 1)
                if number not in existing_numbers
            )
            board_id = f"board-{next_number}-{uuid.uuid4().hex}"
            updated_at = utc_now()
            connection.execute(
                """
                INSERT INTO boards (id, number, name, text, revision, updated_at)
                VALUES (?, ?, ?, '', 0, ?)
                """,
                (board_id, next_number, f"板 {next_number}", updated_at),
            )
            return {
                "id": board_id,
                "number": next_number,
                "name": f"板 {next_number}",
                "text": "",
                "revision": 0,
                "updatedAt": updated_at,
            }

    def rename_board(self, board_id: Any, name: Any) -> dict[str, Any]:
        if not isinstance(board_id, str):
            raise BoardNotFound("Board does not exist.")
        if not isinstance(name, str):
            raise InvalidBoardName("Board name must be a string.")
        normalized_name = " ".join(name.split())
        if not normalized_name:
            raise InvalidBoardName("Board name cannot be empty.")
        if len(normalized_name) > 24:
            raise InvalidBoardName("Board name is too long.")

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, number, name, revision, updated_at
                FROM boards
                WHERE id = ?
                """,
                (board_id,),
            ).fetchone()
            if row is None:
                raise BoardNotFound("Board does not exist.")
            if row["name"] != normalized_name:
                connection.execute(
                    "UPDATE boards SET name = ? WHERE id = ?",
                    (normalized_name, board_id),
                )
            renamed = dict(row)
            renamed["name"] = normalized_name
            return renamed

    def delete_board(self, board_id: Any) -> dict[str, Any]:
        if not isinstance(board_id, str):
            raise BoardNotFound("Board does not exist.")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id, number, name, text, revision, updated_at
                FROM boards
                ORDER BY number
                """
            ).fetchall()
            if len(rows) <= 1:
                raise LastBoardDeletion("At least one board must remain.")
            deleted_index = next(
                (index for index, row in enumerate(rows) if row["id"] == board_id),
                None,
            )
            if deleted_index is None:
                raise BoardNotFound("Board does not exist.")
            connection.execute("DELETE FROM boards WHERE id = ?", (board_id,))
            remaining = rows[:deleted_index] + rows[deleted_index + 1 :]
            fallback_index = min(deleted_index, len(remaining) - 1)
            return {
                "deletedId": board_id,
                "fallback": self._board_from_row(remaining[fallback_index]),
            }

    def update(
        self,
        board_id: Any,
        base_revision: Any,
        text: Any,
    ) -> dict[str, Any]:
        self._validate_text(text)
        if not isinstance(board_id, str):
            raise BoardNotFound("Board does not exist.")
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            raise InvalidDocument("baseRevision must be an integer.")

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, number, name, text, revision, updated_at
                FROM boards
                WHERE id = ?
                """,
                (board_id,),
            ).fetchone()
            if row is None:
                raise BoardNotFound("Board does not exist.")
            current = self._board_from_row(row)
            if base_revision != current["revision"]:
                raise RevisionConflict(current)
            if text == current["text"]:
                return current

            revision = current["revision"] + 1
            updated_at = utc_now()
            connection.execute(
                """
                UPDATE boards
                SET text = ?, revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (text, revision, updated_at, board_id),
            )
            return {
                **current,
                "text": text,
                "revision": revision,
                "updatedAt": updated_at,
            }


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
                    "version": 3,
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

    @staticmethod
    def _foreground_window() -> int:
        if os.name != "nt":
            raise RuntimeError("Direct input is only available on Windows.")
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        target = user32.GetForegroundWindow()
        if target:
            return int(target)

        class GuiThreadInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        user32.GetGUIThreadInfo.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(GuiThreadInfo),
        )
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        thread_info = GuiThreadInfo()
        thread_info.cbSize = ctypes.sizeof(GuiThreadInfo)
        if user32.GetGUIThreadInfo(0, ctypes.byref(thread_info)):
            target = thread_info.hwndFocus or thread_info.hwndActive
            if target:
                return int(target)
        raise RuntimeError("No foreground window is available.")

    @classmethod
    def _focused_window(cls) -> int:
        """Return the focused child control when Windows exposes one."""
        import ctypes
        from ctypes import wintypes

        foreground = cls._foreground_window()
        try:
            user32 = ctypes.windll.user32

            class GuiThreadInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND),
                    ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND),
                    ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT),
                ]

            user32.GetWindowThreadProcessId.argtypes = (
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            )
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            thread_id = user32.GetWindowThreadProcessId(foreground, None)
            user32.GetGUIThreadInfo.argtypes = (
                wintypes.DWORD,
                ctypes.POINTER(GuiThreadInfo),
            )
            user32.GetGUIThreadInfo.restype = wintypes.BOOL
            thread_info = GuiThreadInfo()
            thread_info.cbSize = ctypes.sizeof(GuiThreadInfo)
            if thread_id and user32.GetGUIThreadInfo(
                thread_id,
                ctypes.byref(thread_info),
            ):
                focused = thread_info.hwndFocus or thread_info.hwndCaret
                if focused:
                    return int(focused)
        except (AttributeError, OSError, TypeError):
            pass
        return foreground

    @staticmethod
    def _standard_text_control(target: int) -> int:
        """Find a standard Edit/RichEdit control for fast text insertion."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        def class_name(window: int) -> str:
            buffer = ctypes.create_unicode_buffer(256)
            try:
                user32.GetClassNameW.argtypes = (
                    wintypes.HWND,
                    wintypes.LPWSTR,
                    ctypes.c_int,
                )
                user32.GetClassNameW.restype = ctypes.c_int
                if user32.GetClassNameW(window, buffer, len(buffer)):
                    return buffer.value
            except (AttributeError, OSError, TypeError):
                return ""
            return ""

        def supported(window: int) -> bool:
            value = class_name(window).casefold()
            return value == "edit" or value.startswith("richedit")

        if target and supported(target):
            return int(target)

        try:
            user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
            user32.GetAncestor.restype = wintypes.HWND
            root = user32.GetAncestor(target, 2) or target
            matches: list[int] = []
            enum_child_proc = ctypes.WINFUNCTYPE(
                wintypes.BOOL,
                wintypes.HWND,
                wintypes.LPARAM,
            )

            @enum_child_proc
            def collect(window: int, _parameter: int) -> bool:
                if supported(window):
                    matches.append(int(window))
                return True

            user32.EnumChildWindows.argtypes = (
                wintypes.HWND,
                enum_child_proc,
                wintypes.LPARAM,
            )
            user32.EnumChildWindows.restype = wintypes.BOOL
            user32.EnumChildWindows(root, collect, 0)
            return matches[0] if matches else 0
        except (AttributeError, OSError, TypeError):
            return 0

    @staticmethod
    def _replace_standard_text(control: int, text: str) -> bool:
        """Insert text synchronously without flooding the target input queue."""
        if not control or not text:
            return False
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        em_replace_sel = 0x00C2
        smto_block = 0x0001
        smto_abort_if_hung = 0x0002
        buffer = ctypes.create_unicode_buffer(text)
        result = wintypes.WPARAM()
        try:
            user32.SendMessageTimeoutW.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(wintypes.WPARAM),
            )
            user32.SendMessageTimeoutW.restype = wintypes.LPARAM
            completed = user32.SendMessageTimeoutW(
                control,
                em_replace_sel,
                1,
                ctypes.cast(buffer, ctypes.c_void_p).value,
                smto_block | smto_abort_if_hung,
                150,
                ctypes.byref(result),
            )
            return bool(completed)
        except (AttributeError, OSError, TypeError):
            return False

    def paste(self, text: str) -> None:
        import pyautogui
        import pyperclip

        with self._lock:
            pyperclip.copy(text)
            time.sleep(0.08)
            pyautogui.hotkey("ctrl", "v")

    def capture_target(self) -> int:
        try:
            return self._focused_window()
        except RuntimeError:
            # SendInput itself targets the current Windows foreground control.
            # Some desktop isolation layers do not expose the foreground HWND,
            # so keep direct input usable while retaining strict focus checks
            # whenever Windows provides a handle.
            return 0

    def direct_input(self, target: int, delete_count: int, text: str) -> None:
        if os.name != "nt":
            raise RuntimeError("Direct input is only available on Windows.")
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        try:
            current_target = self._focused_window()
        except RuntimeError:
            current_target = 0
        if target and current_target and current_target != int(target):
            raise DirectInputFailure(
                "focus_changed",
                "电脑当前窗口已变化，直输已暂停。",
            )

        if (
            delete_count == 0
            and text
            and self._replace_standard_text(
                self._standard_text_control(current_target),
                text,
            )
        ):
            return

        keyeventf_keyup = 0x0002
        keyeventf_unicode = 0x0004
        input_keyboard = 1
        vk_back = 0x08
        ulong_ptr = wintypes.WPARAM

        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class HardwareInput(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [
                ("mi", MouseInput),
                ("ki", KeyboardInput),
                ("hi", HardwareInput),
            ]

        class Input(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [
                ("type", wintypes.DWORD),
                ("value", InputUnion),
            ]

        inputs: list[Input] = []

        def append_key(
            virtual_key: int,
            scan_code: int,
            flags: int,
        ) -> None:
            inputs.append(
                Input(
                    type=input_keyboard,
                    value=InputUnion(
                        ki=KeyboardInput(
                            wVk=virtual_key,
                            wScan=scan_code,
                            dwFlags=flags,
                            time=0,
                            dwExtraInfo=0,
                        )
                    ),
                )
            )

        for _ in range(delete_count):
            append_key(vk_back, 0, 0)
            append_key(vk_back, 0, keyeventf_keyup)

        encoded = text.encode("utf-16-le")
        for index in range(0, len(encoded), 2):
            scan_code = int.from_bytes(encoded[index : index + 2], "little")
            append_key(0, scan_code, keyeventf_unicode)
            append_key(0, scan_code, keyeventf_unicode | keyeventf_keyup)

        if not inputs:
            return
        input_array = (Input * len(inputs))(*inputs)
        user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(Input),
            ctypes.c_int,
        )
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(
            len(input_array),
            input_array,
            ctypes.sizeof(Input),
        )
        if sent != len(input_array):
            raise RuntimeError("Windows did not accept all simulated input events.")

    def direct_key(self, target: int, key: str) -> None:
        if os.name != "nt":
            raise RuntimeError("Direct input is only available on Windows.")
        import ctypes
        from ctypes import wintypes

        virtual_keys = {
            "backspace": 0x08,
            "enter": 0x0D,
        }
        virtual_key = virtual_keys.get(key)
        if virtual_key is None:
            raise ValueError("Unsupported direct key.")

        try:
            current_target = self._focused_window()
        except RuntimeError:
            current_target = 0
        if target and current_target and current_target != int(target):
            raise DirectInputFailure(
                "focus_changed",
                "电脑当前窗口已变化，直输已暂停。",
            )

        user32 = ctypes.windll.user32
        user32.keybd_event.argtypes = (
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            wintypes.DWORD,
            wintypes.WPARAM,
        )
        user32.keybd_event.restype = None
        keyeventf_keyup = 0x0002
        user32.keybd_event(virtual_key, 0, 0, 0)
        user32.keybd_event(virtual_key, 0, keyeventf_keyup, 0)


class DirectInputFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DirectInputManager:
    def __init__(self, actions: Any):
        self.actions = actions
        self._lock = threading.RLock()
        self._session: dict[str, Any] | None = None

    def _purge_expired(self) -> None:
        if (
            self._session is not None
            and time.monotonic() - self._session["lastActivity"]
            > DIRECT_SESSION_IDLE_SECONDS
        ):
            self._session = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._purge_expired()
            if self._session is None:
                return {"active": False}
            return {
                "active": True,
                "deviceName": self._session["deviceName"],
                "startedAt": self._session["startedAt"],
            }

    def begin(
        self,
        *,
        connection_id: str,
        device_id: str,
        device_name: str,
    ) -> dict[str, Any]:
        with self._lock:
            self._purge_expired()
            if self._session is not None:
                if self._session["connectionId"] == connection_id:
                    return dict(self._session)
                raise DirectInputFailure(
                    "direct_busy",
                    f'{self._session["deviceName"]} 正在使用直输。',
                )
            try:
                target = self.actions.capture_target()
            except DirectInputFailure:
                raise
            except Exception as error:
                raise DirectInputFailure(
                    "target_unavailable",
                    "无法获取电脑当前窗口。",
                ) from error
            self._session = {
                "id": str(uuid.uuid4()),
                "connectionId": connection_id,
                "deviceId": device_id,
                "deviceName": device_name,
                "target": target,
                "lastSequence": 0,
                "lastActivity": time.monotonic(),
                "startedAt": utc_now(),
            }
            return dict(self._session)

    def apply(
        self,
        *,
        connection_id: str,
        session_id: Any,
        sequence: Any,
        delete_count: Any,
        text: Any,
    ) -> dict[str, Any]:
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise DirectInputFailure("invalid_sequence", "直输序号无效。")
        if (
            not isinstance(delete_count, int)
            or isinstance(delete_count, bool)
            or not 0 <= delete_count <= MAX_DIRECT_DELETE_COUNT
        ):
            raise DirectInputFailure("invalid_delete", "直输删除长度无效。")
        if not isinstance(text, str):
            raise DirectInputFailure("invalid_text", "直输内容无效。")
        if len(text.encode("utf-8")) > MAX_DIRECT_TEXT_BYTES:
            raise DirectInputFailure("direct_text_too_large", "单次直输内容过长。")

        with self._lock:
            self._purge_expired()
            session = self._session
            if (
                session is None
                or session["id"] != session_id
                or session["connectionId"] != connection_id
            ):
                raise DirectInputFailure("direct_inactive", "直输会话已结束。")
            if sequence <= session["lastSequence"]:
                return {
                    "sessionId": session["id"],
                    "sequence": sequence,
                    "duplicate": True,
                }
            if sequence != session["lastSequence"] + 1:
                raise DirectInputFailure("sequence_gap", "直输消息顺序不连续。")
            try:
                self.actions.direct_input(
                    session["target"],
                    delete_count,
                    text,
                )
            except DirectInputFailure:
                self._session = None
                raise
            except Exception as error:
                self._session = None
                raise DirectInputFailure(
                    "direct_input_failed",
                    "电脑模拟输入失败。",
                ) from error
            session["lastSequence"] = sequence
            session["lastActivity"] = time.monotonic()
            return {
                "sessionId": session["id"],
                "sequence": sequence,
                "duplicate": False,
            }

    def apply_key(
        self,
        *,
        connection_id: str,
        session_id: Any,
        request_id: Any,
        key: Any,
    ) -> dict[str, Any]:
        if key not in {"backspace", "enter"}:
            raise DirectInputFailure("invalid_direct_key", "直输按键无效。")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise DirectInputFailure("invalid_request_id", "直输请求标识无效。")

        with self._lock:
            self._purge_expired()
            session = self._session
            if (
                session is None
                or session["id"] != session_id
                or session["connectionId"] != connection_id
            ):
                raise DirectInputFailure("direct_inactive", "直输会话已结束。")
            try:
                self.actions.direct_key(session["target"], key)
            except DirectInputFailure:
                self._session = None
                raise
            except Exception as error:
                self._session = None
                raise DirectInputFailure(
                    "direct_key_failed",
                    "电脑模拟按键失败。",
                ) from error
            session["lastActivity"] = time.monotonic()
            return {
                "sessionId": session["id"],
                "requestId": request_id,
                "key": key,
            }

    def stop(
        self,
        *,
        connection_id: str | None = None,
        session_id: Any = None,
        force: bool = False,
    ) -> bool:
        with self._lock:
            if self._session is None:
                return False
            if not force:
                if self._session["connectionId"] != connection_id:
                    return False
                if session_id is not None and self._session["id"] != session_id:
                    return False
            self._session = None
            return True


class SyncHub:
    def __init__(self):
        self._lock = threading.RLock()
        self._connections: dict[str, dict[str, Any]] = {}

    def register(self, websocket: Any, device_id: str, board_id: str) -> str:
        connection_id = str(uuid.uuid4())
        with self._lock:
            self._connections[connection_id] = {
                "websocket": websocket,
                "deviceId": device_id,
                "boardId": board_id,
            }
        return connection_id

    def unregister(self, connection_id: str) -> None:
        with self._lock:
            self._connections.pop(connection_id, None)

    def select_board(self, connection_id: str, board_id: str) -> None:
        with self._lock:
            connection = self._connections.get(connection_id)
            if connection is not None:
                connection["boardId"] = board_id

    def replace_board(self, deleted_board_id: str, fallback_board_id: str) -> None:
        with self._lock:
            for connection in self._connections.values():
                if connection["boardId"] == deleted_board_id:
                    connection["boardId"] = fallback_board_id

    def broadcast(
        self,
        message: dict[str, Any],
        *,
        exclude: str | None = None,
        board_id: str | None = None,
    ) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            recipients = [
                (connection_id, connection["websocket"])
                for connection_id, connection in self._connections.items()
                if connection_id != exclude
                and (
                    board_id is None
                    or connection["boardId"] == board_id
                )
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
                (connection_id, connection["websocket"])
                for connection_id, connection in self._connections.items()
                if connection["deviceId"] == device_id
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
        for connection_id, connection in connections:
            try:
                connection["websocket"].close(
                    reason=1001,
                    message="EasyType is shutting down.",
                )
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
    network_repair_callback: Callable[[], bool] | None = None,
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
    board_store = BoardStore(
        resolved_data_dir / "state.db",
        legacy_path=resolved_data_dir / "state.json",
    )
    access_settings = AccessSettingsStore(resolved_data_dir / "settings.json")
    auth_store = AuthStore(resolved_data_dir / "auth.json")
    pairing_manager = PairingManager()
    sync_hub = SyncHub()
    actions = action_backend or WindowsActions()
    direct_input = DirectInputManager(actions)

    app.extensions["easytype_boards"] = board_store
    app.extensions["easytype_access_settings"] = access_settings
    app.extensions["easytype_auth"] = auth_store
    app.extensions["easytype_pairing"] = pairing_manager
    app.extensions["easytype_hub"] = sync_hub
    app.extensions["easytype_actions"] = actions
    app.extensions["easytype_direct_input"] = direct_input
    app.extensions["easytype_network_repair"] = network_repair_callback

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
            max_boards=MAX_BOARDS,
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
        snapshot = board_store.snapshot()
        return jsonify(
            {
                "ok": True,
                "version": APP_VERSION,
                "status": "online",
                "revision": snapshot["revision"],
                "updatedAt": snapshot["updatedAt"],
                "accessMode": access_settings.mode(),
                "boardCount": len(board_store.list_boards()),
                "maxBoards": MAX_BOARDS,
            }
        )

    @app.get("/api/document")
    @require_auth
    def api_document() -> Any:
        try:
            document = board_store.snapshot(request.args.get("boardId"))
        except BoardNotFound:
            return jsonify_error("board_not_found", "共享板不存在。", 404)
        return jsonify({"ok": True, "document": document})

    @app.get("/api/boards")
    @require_auth
    def api_boards() -> Any:
        return jsonify(
            {
                "ok": True,
                "boards": board_store.list_boards(),
                "maxBoards": MAX_BOARDS,
            }
        )

    @app.post("/api/boards")
    @require_auth
    @require_mutation_origin
    def create_board() -> Any:
        try:
            board = board_store.create_board()
        except BoardLimitReached:
            return jsonify_error(
                "board_limit_reached",
                f"最多只能创建 {MAX_BOARDS} 个共享板。",
                409,
            )
        sync_hub.broadcast({"type": "board_created", "board": board})
        return jsonify({"ok": True, "board": board}), 201

    @app.patch("/api/boards/<board_id>")
    @require_auth
    @require_mutation_origin
    def rename_board(board_id: str) -> Any:
        payload = request.get_json(silent=True)
        name = payload.get("name") if isinstance(payload, dict) else None
        try:
            board = board_store.rename_board(board_id, name)
        except BoardNotFound:
            return jsonify_error("board_not_found", "共享板不存在。", 404)
        except InvalidBoardName:
            return jsonify_error(
                "invalid_board_name",
                "名称不能为空，且最多为 24 个字符。",
                400,
            )
        sync_hub.broadcast({"type": "board_renamed", "board": board})
        return jsonify({"ok": True, "board": board})

    @app.delete("/api/boards/<board_id>")
    @require_auth
    @require_mutation_origin
    def delete_board(board_id: str) -> Any:
        try:
            result = board_store.delete_board(board_id)
        except BoardNotFound:
            return jsonify_error("board_not_found", "共享板不存在。", 404)
        except LastBoardDeletion:
            return jsonify_error(
                "last_board_required",
                "至少需要保留一个共享板。",
                409,
            )
        fallback = result["fallback"]
        sync_hub.replace_board(result["deletedId"], fallback["id"])
        sync_hub.broadcast(
            {
                "type": "board_deleted",
                "boardId": result["deletedId"],
                "fallback": fallback,
            }
        )
        return jsonify({"ok": True, **result})

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
            payload = request.get_json(silent=True)
            board_id = payload.get("boardId") if isinstance(payload, dict) else None
            actions.paste(board_store.snapshot(board_id)["text"])
            return jsonify({"ok": True})
        except BoardNotFound:
            return jsonify_error("board_not_found", "共享板不存在。", 404)
        except Exception:
            app.logger.exception("Paste action failed.")
            return jsonify_error("paste_failed", "插入到电脑当前窗口失败。", 500)

    @app.post("/api/actions/enter")
    @require_auth
    @require_mutation_origin
    def press_enter() -> Any:
        if _is_loopback_request():
            return jsonify_error(
                "remote_only",
                "回车功能仅在手机端可用。",
                403,
            )
        try:
            actions.direct_key(0, "enter")
            return jsonify({"ok": True})
        except Exception:
            app.logger.exception("Remote Enter action failed.")
            return jsonify_error("enter_failed", "发送电脑回车失败。", 500)

    @app.post("/api/actions/key")
    @require_auth
    @require_mutation_origin
    def press_remote_key() -> Any:
        if _is_loopback_request():
            return jsonify_error(
                "remote_only",
                "电脑按键功能仅在手机端可用。",
                403,
            )
        payload = request.get_json(silent=True)
        key = payload.get("key") if isinstance(payload, dict) else None
        if key not in {"backspace", "enter"}:
            return jsonify_error("invalid_key", "电脑按键无效。", 400)
        try:
            actions.direct_key(0, key)
            return jsonify({"ok": True, "key": key})
        except Exception:
            app.logger.exception("Remote key action failed.")
            return jsonify_error("key_failed", "发送电脑按键失败。", 500)

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

    @app.post("/api/admin/network-access/repair")
    @require_local
    @require_mutation_origin
    def repair_network_access() -> Any:
        repair = app.extensions.get("easytype_network_repair")
        if repair is None:
            return jsonify_error(
                "network_repair_unavailable",
                "当前启动方式不支持自动修复网络访问。",
                503,
            )
        try:
            repaired = bool(repair())
        except Exception:
            app.logger.exception("Network access repair failed.")
            repaired = False
        if not repaired:
            return jsonify_error(
                "network_repair_failed",
                "网络授权未完成，请确认 Windows 管理员提示后重试。",
                500,
            )
        return jsonify({"ok": True})

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

        first_board_id = board_store.first_board_id()
        connection_id = sync_hub.register(
            websocket,
            device["id"],
            first_board_id,
        )
        try:
            websocket.send(
                json.dumps(
                    {
                        "type": "snapshot",
                        "boards": board_store.list_boards(),
                        "document": board_store.snapshot(first_board_id),
                        "direct": direct_input.status(),
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

                if message_type == "select_board":
                    try:
                        document = board_store.snapshot(message.get("boardId"))
                    except BoardNotFound:
                        send_socket_error(
                            websocket,
                            "board_not_found",
                            "共享板不存在。",
                        )
                        continue
                    sync_hub.select_board(connection_id, document["id"])
                    websocket.send(
                        json.dumps(
                            {
                                "type": "board_snapshot",
                                "document": document,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    continue

                if message_type == "create_board":
                    try:
                        board = board_store.create_board()
                    except BoardLimitReached:
                        send_socket_error(
                            websocket,
                            "board_limit_reached",
                            f"最多只能创建 {MAX_BOARDS} 个共享板。",
                        )
                        continue
                    sync_hub.broadcast(
                        {
                            "type": "board_created",
                            "board": board,
                            "sourceId": str(message.get("clientId") or ""),
                        }
                    )
                    continue

                if message_type == "rename_board":
                    try:
                        board = board_store.rename_board(
                            message.get("boardId"),
                            message.get("name"),
                        )
                    except BoardNotFound:
                        send_socket_error(
                            websocket,
                            "board_not_found",
                            "共享板不存在。",
                        )
                        continue
                    except InvalidBoardName:
                        send_socket_error(
                            websocket,
                            "invalid_board_name",
                            "名称不能为空，且最多为 24 个字符。",
                        )
                        continue
                    sync_hub.broadcast(
                        {
                            "type": "board_renamed",
                            "board": board,
                            "sourceId": str(message.get("clientId") or ""),
                        }
                    )
                    continue

                if message_type == "delete_board":
                    try:
                        result = board_store.delete_board(message.get("boardId"))
                    except BoardNotFound:
                        send_socket_error(
                            websocket,
                            "board_not_found",
                            "共享板不存在。",
                        )
                        continue
                    except LastBoardDeletion:
                        send_socket_error(
                            websocket,
                            "last_board_required",
                            "至少需要保留一个共享板。",
                        )
                        continue
                    fallback = result["fallback"]
                    sync_hub.replace_board(result["deletedId"], fallback["id"])
                    sync_hub.broadcast(
                        {
                            "type": "board_deleted",
                            "boardId": result["deletedId"],
                            "fallback": fallback,
                            "sourceId": str(message.get("clientId") or ""),
                        }
                    )
                    continue

                if message_type == "direct_start":
                    if device["id"] == "local":
                        send_socket_error(
                            websocket,
                            "remote_only",
                            "请在手机网页上开启直输。",
                        )
                        continue
                    try:
                        session = direct_input.begin(
                            connection_id=connection_id,
                            device_id=device["id"],
                            device_name=device["name"],
                        )
                    except DirectInputFailure as error:
                        send_socket_error(websocket, error.code, error.message)
                        continue
                    websocket.send(
                        json.dumps(
                            {
                                "type": "direct_started",
                                "sessionId": session["id"],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    sync_hub.broadcast(
                        {
                            "type": "direct_status",
                            "direct": direct_input.status(),
                        }
                    )
                    continue

                if message_type == "direct_stop":
                    stopped = direct_input.stop(
                        connection_id=connection_id,
                        session_id=message.get("sessionId"),
                    )
                    websocket.send(
                        json.dumps(
                            {
                                "type": "direct_stopped",
                                "stopped": stopped,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    if stopped:
                        sync_hub.broadcast(
                            {
                                "type": "direct_status",
                                "direct": direct_input.status(),
                            }
                        )
                    continue

                if message_type == "direct_input":
                    try:
                        acknowledgement = direct_input.apply(
                            connection_id=connection_id,
                            session_id=message.get("sessionId"),
                            sequence=message.get("sequence"),
                            delete_count=message.get("deleteCount"),
                            text=message.get("text"),
                        )
                    except DirectInputFailure as error:
                        send_socket_error(websocket, error.code, error.message)
                        if error.code in {
                            "focus_changed",
                            "direct_input_failed",
                            "direct_inactive",
                        }:
                            sync_hub.broadcast(
                                {
                                    "type": "direct_status",
                                    "direct": direct_input.status(),
                                }
                            )
                        continue
                    websocket.send(
                        json.dumps(
                            {
                                "type": "direct_ack",
                                **acknowledgement,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    continue

                if message_type == "direct_key":
                    try:
                        acknowledgement = direct_input.apply_key(
                            connection_id=connection_id,
                            session_id=message.get("sessionId"),
                            request_id=message.get("requestId"),
                            key=message.get("key"),
                        )
                    except DirectInputFailure as error:
                        send_socket_error(websocket, error.code, error.message)
                        if error.code in {
                            "focus_changed",
                            "direct_key_failed",
                            "direct_inactive",
                        }:
                            sync_hub.broadcast(
                                {
                                    "type": "direct_status",
                                    "direct": direct_input.status(),
                                }
                            )
                        continue
                    websocket.send(
                        json.dumps(
                            {
                                "type": "direct_key_ack",
                                **acknowledgement,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    continue

                if message_type != "update":
                    send_socket_error(websocket, "unknown_message", "未知消息类型。")
                    continue

                try:
                    updated = board_store.update(
                        message.get("boardId"),
                        message.get("baseRevision"),
                        message.get("text"),
                    )
                except BoardNotFound:
                    send_socket_error(
                        websocket,
                        "board_not_found",
                        "共享板不存在。",
                    )
                    continue
                except RevisionConflict as conflict:
                    websocket.send(
                        json.dumps(
                            {
                                "type": "conflict",
                                "boardId": conflict.snapshot["id"],
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
                            "boardId": updated["id"],
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
                    board_id=updated["id"],
                )
                sync_hub.broadcast(
                    {
                        "type": "board_updated",
                        "board": {
                            key: updated[key]
                            for key in (
                                "id",
                                "number",
                                "name",
                                "revision",
                                "updatedAt",
                            )
                        },
                        "sourceId": str(message.get("clientId") or ""),
                    },
                    exclude=connection_id,
                )
        except Exception as error:
            app.logger.info(
                "WebSocket connection ended (%s).",
                type(error).__name__,
            )
        finally:
            direct_stopped = direct_input.stop(connection_id=connection_id)
            sync_hub.unregister(connection_id)
            if direct_stopped:
                sync_hub.broadcast(
                    {
                        "type": "direct_status",
                        "direct": direct_input.status(),
                    }
                )

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
