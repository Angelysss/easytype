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

from easytype_app import APP_VERSION, create_app, default_data_dir

try:
    import winreg
except ImportError:
    winreg = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None


logger = logging.getLogger(__name__)
FIREWALL_MARKER_NAME = "firewall.json"
FIREWALL_MARKER_VERSION = 3
GITHUB_URL = "https://github.com/Angelysss/easytype"
STARTUP_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REGISTRY_VALUE = "EasyType"


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


def startup_command(
    host: str,
    port: int,
    data_dir: Path | None = None,
) -> str:
    """Return the command saved for this EasyType installation at sign-in."""
    if getattr(sys, "frozen", False):
        arguments = [str(Path(sys.executable).resolve())]
    else:
        interpreter = Path(sys.executable).resolve()
        pythonw = interpreter.with_name("pythonw.exe")
        if pythonw.exists():
            interpreter = pythonw
        arguments = [
            str(interpreter),
            str(Path(__file__).resolve()),
        ]

    arguments.extend(["--host", host, "--port", str(port)])
    if data_dir is not None:
        arguments.extend(["--data-dir", str(Path(data_dir).resolve())])
    return subprocess.list2cmdline(arguments)


def _read_startup_command() -> str | None:
    if os.name != "nt" or winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            STARTUP_REGISTRY_PATH,
            0,
            winreg.KEY_READ,
        ) as key:
            value, value_type = winreg.QueryValueEx(
                key,
                STARTUP_REGISTRY_VALUE,
            )
    except FileNotFoundError:
        return None
    if value_type != winreg.REG_SZ or not isinstance(value, str):
        return None
    return value


def is_startup_enabled(command: str) -> bool:
    return _read_startup_command() == command


def set_startup_enabled(enabled: bool, command: str) -> None:
    if os.name != "nt" or winreg is None:
        raise OSError("开机自启动仅支持 Windows。")
    if enabled:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            STARTUP_REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                STARTUP_REGISTRY_VALUE,
                0,
                winreg.REG_SZ,
                command,
            )
        return

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            STARTUP_REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, STARTUP_REGISTRY_VALUE)
    except FileNotFoundError:
        pass


def _firewall_marker_matches(
    marker_path: Path,
    port: int,
) -> bool:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return marker == {
        "version": FIREWALL_MARKER_VERSION,
        "port": port,
    }


def _write_firewall_marker(
    marker_path: Path,
    port: int,
) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = marker_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "version": FIREWALL_MARKER_VERSION,
                "port": port,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.replace(temporary_path, marker_path)


def _firewall_program_paths() -> list[Path]:
    candidates = [Path(sys.executable).resolve()]
    if not getattr(sys, "frozen", False):
        base_executable = getattr(sys, "_base_executable", None)
        if base_executable:
            candidates.append(Path(base_executable).resolve())

    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique.setdefault(str(candidate).casefold(), candidate)
    return list(unique.values())


def ensure_windows_firewall_access(
    port: int,
    data_dir: Path | None = None,
    *,
    host: str = "0.0.0.0",
    force: bool = False,
) -> bool:
    """Allow the EasyType port from the local subnet on any network profile."""
    if (
        os.name != "nt"
        or host in {"127.0.0.1", "::1", "localhost"}
    ):
        return True

    resolved_data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    marker_path = resolved_data_dir / FIREWALL_MARKER_NAME
    if not force and _firewall_marker_matches(marker_path, port):
        return True

    escaped_programs = ",\n".join(
        "    '" + str(program).replace("'", "''") + "'"
        for program in _firewall_program_paths()
    )
    elevated_script = f"""
$ErrorActionPreference = 'Stop'
$easyTypePort = {port}
$displayName = "EasyType LAN TCP $easyTypePort"
$easyTypePrograms = @(
{escaped_programs}
)

foreach (
    $blockRule in @(
        Get-NetFirewallRule `
            -Enabled True `
            -Direction Inbound `
            -Action Block `
            -ErrorAction SilentlyContinue
    )
) {{
    $blockedProgram = (
        Get-NetFirewallApplicationFilter `
            -AssociatedNetFirewallRule $blockRule `
            -ErrorAction SilentlyContinue
    ).Program
    foreach ($easyTypeProgram in $easyTypePrograms) {{
        if (
            $blockedProgram -and
            [string]::Equals(
                $blockedProgram,
                $easyTypeProgram,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {{
            Remove-NetFirewallRule -Name $blockRule.Name -ErrorAction SilentlyContinue
            break
        }}
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
    -Profile Any `
    -RemoteAddress LocalSubnet | Out-Null
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
        _write_firewall_marker(marker_path, port)
    except OSError:
        logger.warning("Firewall access was configured, but its marker could not be saved.")
    return True


class EasyTypeServer(threading.Thread):
    def __init__(self, host: str, port: int, data_dir: Path | None = None):
        super().__init__(daemon=True)
        self.app = create_app(
            port=port,
            data_dir=data_dir,
            network_repair_callback=lambda: ensure_windows_firewall_access(
                port,
                data_dir,
                host=host,
                force=True,
            ),
        )
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
    image = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (4, 4, 59, 59),
        radius=17,
        fill=(37, 99, 235, 255),
        outline=(96, 165, 250, 255),
        width=2,
    )
    draw.rounded_rectangle(
        (8, 8, 55, 30),
        radius=13,
        fill=(59, 130, 246, 255),
    )
    glyph_color = (255, 255, 255, 255)
    draw.rounded_rectangle((18, 16, 23, 48), radius=2, fill=glyph_color)
    draw.rounded_rectangle((20, 16, 46, 21), radius=2, fill=glyph_color)
    draw.rounded_rectangle((20, 29, 41, 34), radius=2, fill=glyph_color)
    draw.rounded_rectangle((20, 43, 46, 48), radius=2, fill=glyph_color)
    return image


def create_tray_menu(
    editor_url: str,
    server: EasyTypeServer,
    current_startup_command: str,
) -> pystray.Menu:
    if pystray is None:
        raise RuntimeError("pystray is not installed.")

    def open_editor(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        webbrowser.open(editor_url)

    def toggle_startup(icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        try:
            set_startup_enabled(
                not is_startup_enabled(current_startup_command),
                current_startup_command,
            )
        except OSError as error:
            show_message("EasyType 开机自启动", f"设置失败：{error}")
        finally:
            icon.update_menu()

    def open_github(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        webbrowser.open(GITHUB_URL)

    def exit_app(icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        logger.info("EasyType is shutting down.")
        server.shutdown()
        icon.stop()

    return pystray.Menu(
        pystray.MenuItem("打开共享文本板", open_editor, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "开机自启动",
            toggle_startup,
            checked=lambda _item: is_startup_enabled(
                current_startup_command,
            ),
        ),
        pystray.MenuItem(f"EasyType v{APP_VERSION}", None, enabled=False),
        pystray.MenuItem("GitHub", open_github),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", exit_app),
    )


def run_with_tray(host: str, port: int, data_dir: Path | None = None) -> None:
    if pystray is None or Image is None:
        show_message(
            "EasyType",
            "缺少 pystray 或 Pillow，将以前台模式启动。请执行 uv sync 补齐依赖。",
        )
        app = create_app(
            port=port,
            data_dir=data_dir,
            network_repair_callback=lambda: ensure_windows_firewall_access(
                port,
                data_dir,
                host=host,
                force=True,
            ),
        )
        app.run(host=host, port=port, debug=False, threaded=True)
        return

    try:
        server = EasyTypeServer(host, port, data_dir)
    except OSError:
        show_message("EasyType", f"端口 {port} 已被占用，可能已有实例正在运行。")
        return

    server.start()
    editor_url = f"http://127.0.0.1:{port}/"
    current_startup_command = startup_command(host, port, data_dir)
    logger.info("EasyType started on port %s.", port)
    menu = create_tray_menu(
        editor_url,
        server,
        current_startup_command,
    )
    icon = pystray.Icon(
        "EasyType",
        create_tray_image(),
        f"EasyType v{APP_VERSION}",
        menu,
    )
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
        create_app(
            port=port,
            data_dir=args.data_dir,
            network_repair_callback=lambda: ensure_windows_firewall_access(
                port,
                args.data_dir,
                host=args.host,
                force=True,
            ),
        ).run(
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
