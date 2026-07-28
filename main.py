from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from werkzeug.serving import make_server

from easytype_app import create_app, default_data_dir

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None


logger = logging.getLogger(__name__)
FIREWALL_MARKER_NAME = "firewall.json"


def configured_port() -> int:
    raw = os.environ.get("EASYTYPE_PORT", "5000")
    try:
        port = int(raw)
    except ValueError as error:
        raise ValueError("EASYTYPE_PORT 必须是 1 到 65535 之间的整数。") from error
    if not 1 <= port <= 65535:
        raise ValueError("EASYTYPE_PORT 必须是 1 到 65535 之间的整数。")
    return port


def show_message(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        logger.warning("%s: %s", title, text)


def _firewall_marker_matches(
    marker_path: Path,
    executable: Path,
    port: int,
) -> bool:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return marker == {
        "version": 1,
        "executable": str(executable),
        "port": port,
    }


def _write_firewall_marker(
    marker_path: Path,
    executable: Path,
    port: int,
) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = marker_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "version": 1,
                "executable": str(executable),
                "port": port,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.replace(temporary_path, marker_path)


def ensure_windows_firewall_access(
    port: int,
    data_dir: Path | None = None,
    *,
    host: str = "0.0.0.0",
) -> bool:
    """Configure one private-LAN rule for a packaged EasyType executable."""
    if (
        os.name != "nt"
        or not getattr(sys, "frozen", False)
        or host in {"127.0.0.1", "::1", "localhost"}
    ):
        return True

    executable = Path(sys.executable).resolve()
    resolved_data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    marker_path = resolved_data_dir / FIREWALL_MARKER_NAME
    if _firewall_marker_matches(marker_path, executable, port):
        return True

    escaped_executable = str(executable).replace("'", "''")
    elevated_script = f"""
$ErrorActionPreference = 'Stop'
$easyTypeExecutable = '{escaped_executable}'
$easyTypePort = {port}
$displayName = "EasyType LAN TCP $easyTypePort"

Get-NetFirewallRule -Direction Inbound -Action Block -ErrorAction SilentlyContinue |
    ForEach-Object {{
        $application = Get-NetFirewallApplicationFilter `
            -AssociatedNetFirewallRule $_ `
            -ErrorAction SilentlyContinue
        if (
            $application.Program -and
            [string]::Equals(
                $application.Program,
                $easyTypeExecutable,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {{
            Remove-NetFirewallRule -Name $_.Name -ErrorAction SilentlyContinue
        }}
    }}

Get-NetFirewallRule -Group 'EasyType' -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName 'EasyType LAN TCP *' -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

New-NetFirewallRule `
    -DisplayName $displayName `
    -Group 'EasyType' `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $easyTypePort `
    -Profile Private `
    -RemoteAddress LocalSubnet `
    -Program $easyTypeExecutable | Out-Null
"""
    encoded_script = base64.b64encode(
        elevated_script.encode("utf-16-le")
    ).decode("ascii")
    windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = (
        windows_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    launcher_script = (
        "$ErrorActionPreference='Stop';"
        f"$arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {encoded_script}';"
        f"$process=Start-Process -FilePath '{str(powershell).replace("'", "''")}' "
        "-ArgumentList $arguments -Verb RunAs -Wait -PassThru;"
        "exit $process.ExitCode"
    )
    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                launcher_script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False

    try:
        _write_firewall_marker(marker_path, executable, port)
    except OSError:
        logger.warning("Firewall access was configured, but its marker could not be saved.")
    return True


class EasyTypeServer(threading.Thread):
    def __init__(self, host: str, port: int, data_dir: Path | None = None):
        super().__init__(daemon=True)
        self.app = create_app(port=port, data_dir=data_dir)
        self.server = make_server(host, port, self.app, threaded=True)

    def run(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.app.extensions["easytype_hub"].close_all()
        self.server.shutdown()
        self.join(timeout=5)


def create_tray_image() -> Image.Image:
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is not installed.")
    image = Image.new("RGB", (64, 64), color=(24, 24, 27))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((7, 7, 57, 57), radius=13, fill=(37, 99, 235))
    draw.rounded_rectangle(
        (17, 16, 47, 48),
        radius=5,
        outline=(255, 255, 255),
        width=4,
    )
    draw.line((23, 26, 41, 26), fill=(255, 255, 255), width=3)
    draw.line((23, 35, 41, 35), fill=(255, 255, 255), width=3)
    return image


def run_with_tray(host: str, port: int, data_dir: Path | None = None) -> None:
    if pystray is None or Image is None:
        show_message(
            "EasyType",
            "缺少 pystray 或 Pillow，将以前台模式启动。请执行 uv sync 补齐依赖。",
        )
        app = create_app(port=port, data_dir=data_dir)
        app.run(host=host, port=port, debug=False, threaded=True)
        return

    try:
        server = EasyTypeServer(host, port, data_dir)
    except OSError:
        show_message("EasyType", f"端口 {port} 已被占用，可能已有实例正在运行。")
        return

    server.start()
    editor_url = f"http://127.0.0.1:{port}/"
    logger.info("EasyType started on port %s.", port)

    def open_editor(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        webbrowser.open(editor_url)

    def exit_app(icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        logger.info("EasyType is shutting down.")
        server.shutdown()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开共享文本板", open_editor, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", exit_app),
    )
    icon = pystray.Icon("EasyType", create_tray_image(), "EasyType", menu)
    icon.run()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EasyType 双向共享文本板")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, help="监听端口，默认读取 EASYTYPE_PORT")
    parser.add_argument("--data-dir", type=Path, help="状态和设备授权数据目录")
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="以前台模式运行，不创建系统托盘图标",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        args = parse_args()
        port = args.port if args.port is not None else configured_port()
        if not 1 <= port <= 65535:
            raise ValueError("端口必须是 1 到 65535 之间的整数。")
    except ValueError as error:
        show_message("EasyType", str(error))
        return
    if not ensure_windows_firewall_access(
        port,
        args.data_dir,
        host=args.host,
    ):
        show_message(
            "EasyType 网络访问",
            "EasyType 需要一次 Windows 管理员确认，才能让同一 Wi-Fi 下的"
            "手机访问。\n\n当前未完成授权，本机网页仍可使用；重新启动 "
            "EasyType 后可以再次授权。",
        )
    if args.no_tray:
        create_app(port=port, data_dir=args.data_dir).run(
            host=args.host,
            port=port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
        return
    run_with_tray(host=args.host, port=port, data_dir=args.data_dir)


if __name__ == "__main__":
    main()
