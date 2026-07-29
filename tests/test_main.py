import base64
import re
import sys
from types import SimpleNamespace

import pytest

import main


def test_configured_port_uses_environment(monkeypatch):
    monkeypatch.setenv("EASYTYPE_PORT", "6123")

    assert main.configured_port() == 6123


@pytest.mark.parametrize("value", ["not-a-port", "0", "65536"])
def test_configured_port_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("EASYTYPE_PORT", value)

    with pytest.raises(ValueError):
        main.configured_port()


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
