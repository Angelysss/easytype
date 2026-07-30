import base64
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

import main


class FakeRegistryKey:
    def __init__(self, registry):
        self.registry = registry

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, *_args):
        return FakeRegistryKey(self)

    def CreateKeyEx(self, *_args):
        return FakeRegistryKey(self)

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, _key, name, _reserved, value_type, value):
        self.values[name] = (value, value_type)

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_configured_port_uses_environment(monkeypatch):
    monkeypatch.setenv("EASYTYPE_PORT", "6123")

    assert main.configured_port() == 6123


@pytest.mark.parametrize("value", ["not-a-port", "0", "65536"])
def test_configured_port_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("EASYTYPE_PORT", value)

    with pytest.raises(ValueError):
        main.configured_port()


def test_source_startup_command_uses_pythonw_and_current_options(
    monkeypatch,
    tmp_path,
):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python = scripts / "python.exe"
    pythonw = scripts / "pythonw.exe"
    source = tmp_path / "main.py"
    python.touch()
    pythonw.touch()
    source.touch()
    data_dir = tmp_path / "EasyType Data"
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(main, "__file__", str(source))

    command = main.startup_command("0.0.0.0", 6123, data_dir)

    assert command == subprocess.list2cmdline(
        [
            str(pythonw.resolve()),
            str(source.resolve()),
            "--host",
            "0.0.0.0",
            "--port",
            "6123",
            "--data-dir",
            str(data_dir.resolve()),
        ]
    )


def test_frozen_startup_command_uses_only_executable(monkeypatch, tmp_path):
    executable = tmp_path / "EasyType-1.3.0.exe"
    executable.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    command = main.startup_command("0.0.0.0", 5000)

    assert command == subprocess.list2cmdline(
        [
            str(executable.resolve()),
            "--host",
            "0.0.0.0",
            "--port",
            "5000",
        ]
    )


def test_startup_registry_toggle_only_changes_easytype_value(monkeypatch):
    registry = FakeRegistry()
    registry.values["OtherApp"] = ("other.exe", registry.REG_SZ)
    monkeypatch.setattr(main, "winreg", registry)
    monkeypatch.setattr(main.os, "name", "nt")
    command = r'"C:\EasyType\EasyType.exe" --port 5000'

    assert main.is_startup_enabled(command) is False
    main.set_startup_enabled(True, command)
    assert main.is_startup_enabled(command) is True
    assert registry.values["OtherApp"] == ("other.exe", registry.REG_SZ)

    main.set_startup_enabled(False, command)
    assert main.is_startup_enabled(command) is False
    assert registry.values["OtherApp"] == ("other.exe", registry.REG_SZ)


def test_tray_image_has_transparent_background_and_blue_badge():
    image = main.create_tray_image()

    assert image.mode == "RGBA"
    assert image.size == (64, 64)
    assert image.getpixel((0, 0)) == (0, 0, 0, 0)
    assert image.getpixel((6, 32))[2] > image.getpixel((6, 32))[0]
    assert image.getpixel((21, 18)) == (255, 255, 255, 255)


def test_tray_menu_controls_startup_and_opens_links(monkeypatch):
    registry = FakeRegistry()
    opened_urls = []
    server = SimpleNamespace(
        shutdown_calls=0,
        shutdown=lambda: setattr(
            server,
            "shutdown_calls",
            server.shutdown_calls + 1,
        ),
    )
    icon = SimpleNamespace(
        menu_updates=0,
        stop_calls=0,
        update_menu=lambda: setattr(
            icon,
            "menu_updates",
            icon.menu_updates + 1,
        ),
        stop=lambda: setattr(icon, "stop_calls", icon.stop_calls + 1),
    )
    monkeypatch.setattr(main, "winreg", registry)
    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setattr(
        main.webbrowser,
        "open",
        lambda url: opened_urls.append(url),
    )
    command = r'"C:\EasyType\EasyType.exe" --port 5000'

    menu = main.create_tray_menu(
        "http://127.0.0.1:5000/",
        server,
        command,
    )
    items = list(menu)

    assert [item.text for item in items] == [
        "打开共享文本板",
        "- - - -",
        "开机自启动",
        "EasyType v1.3.0",
        "GitHub",
        "- - - -",
        "退出",
    ]
    assert items[2].checked is False
    assert items[3].enabled is False

    items[2](icon)
    assert items[2].checked is True
    assert icon.menu_updates == 1
    items[0](icon)
    items[4](icon)
    assert opened_urls == [
        "http://127.0.0.1:5000/",
        main.GITHUB_URL,
    ]

    items[2](icon)
    assert items[2].checked is False
    assert icon.menu_updates == 2
    items[6](icon)
    assert server.shutdown_calls == 1
    assert icon.stop_calls == 1


def test_parse_args_supports_bounded_foreground_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "easytype",
            "--no-tray",
            "--host",
            "127.0.0.1",
            "--port",
            "5057",
            "--data-dir",
            str(tmp_path),
        ],
    )

    args = main.parse_args()

    assert args.no_tray is True
    assert args.host == "127.0.0.1"
    assert args.port == 5057
    assert args.data_dir == tmp_path


def test_firewall_configuration_supports_source_and_all_network_profiles(
    monkeypatch,
    tmp_path,
):
    calls = []
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        r"C:\EasyType\.venv\Scripts\python.exe",
    )
    monkeypatch.setattr(
        sys,
        "_base_executable",
        r"C:\Python\python.exe",
        raising=False,
    )
    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda arguments, **options: (
            calls.append((arguments, options))
            or SimpleNamespace(returncode=0)
        ),
    )

    assert main.ensure_windows_firewall_access(5000, tmp_path) is True
    assert len(calls) == 1
    launcher_script = calls[0][0][-1]
    encoded = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)", launcher_script)
    assert encoded is not None
    firewall_script = base64.b64decode(encoded.group(1)).decode("utf-16-le")
    assert "-Profile Any" in firewall_script
    assert "-RemoteAddress LocalSubnet" in firewall_script
    assert "-LocalPort $easyTypePort" in firewall_script
    assert "-Program" not in firewall_script
    assert r"C:\EasyType\.venv\Scripts\python.exe" in firewall_script
    assert r"C:\Python\python.exe" in firewall_script
    assert "-Action Block" in firewall_script
    assert "Remove-NetFirewallRule -Name $blockRule.Name" in firewall_script


def test_firewall_configuration_runs_once(monkeypatch, tmp_path):
    calls = []
    (tmp_path / main.FIREWALL_MARKER_NAME).write_text(
        '{"version":2,"port":5000}',
        encoding="utf-8",
    )

    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")

    def fake_run(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert main.ensure_windows_firewall_access(5000, tmp_path) is True
    assert main.ensure_windows_firewall_access(5000, tmp_path) is True
    assert len(calls) == 1
    assert calls[0][0][0].endswith("powershell.exe")
    marker = main.json.loads(
        (tmp_path / main.FIREWALL_MARKER_NAME).read_text(encoding="utf-8")
    )
    assert marker == {
        "version": main.FIREWALL_MARKER_VERSION,
        "port": 5000,
    }


def test_firewall_configuration_can_be_forced(monkeypatch, tmp_path):
    calls = []
    (tmp_path / main.FIREWALL_MARKER_NAME).write_text(
        f'{{"version":{main.FIREWALL_MARKER_VERSION},"port":5000}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda arguments, **options: (
            calls.append((arguments, options))
            or SimpleNamespace(returncode=0)
        ),
    )

    assert main.ensure_windows_firewall_access(
        5000,
        tmp_path,
        force=True,
    ) is True
    assert len(calls) == 1


def test_firewall_configuration_can_be_declined(monkeypatch, tmp_path):
    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *_arguments, **_options: SimpleNamespace(returncode=1),
    )

    assert main.ensure_windows_firewall_access(5000, tmp_path) is False
    assert not (tmp_path / main.FIREWALL_MARKER_NAME).exists()


def test_firewall_configuration_is_skipped_for_loopback(monkeypatch, tmp_path):
    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *_arguments, **_options: pytest.fail(
            "Loopback-only mode must not configure the firewall."
        ),
    )

    assert main.ensure_windows_firewall_access(
        5000,
        tmp_path,
        host="127.0.0.1",
    ) is True
    assert not (tmp_path / main.FIREWALL_MARKER_NAME).exists()
