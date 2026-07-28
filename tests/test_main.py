import sys
from pathlib import Path
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


def test_firewall_configuration_is_skipped_for_source_runs(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert main.ensure_windows_firewall_access(5000, tmp_path) is True
    assert not (tmp_path / main.FIREWALL_MARKER_NAME).exists()


def test_packaged_firewall_configuration_runs_once(monkeypatch, tmp_path):
    executable = tmp_path / "EasyType-1.1.0.exe"
    calls = []

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
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
        "version": 1,
        "executable": str(Path(sys.executable).resolve()),
        "port": 5000,
    }


def test_packaged_firewall_configuration_can_be_declined(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "EasyType.exe"))
    monkeypatch.setattr(main.os, "name", "nt")
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *_arguments, **_options: SimpleNamespace(returncode=1),
    )

    assert main.ensure_windows_firewall_access(5000, tmp_path) is False
    assert not (tmp_path / main.FIREWALL_MARKER_NAME).exists()
