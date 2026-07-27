import sys

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
