from __future__ import annotations

import argparse
import ctypes
import logging
import os
import threading
import webbrowser
from pathlib import Path

from werkzeug.serving import make_server

from easytype_app import create_app

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None


logger = logging.getLogger(__name__)


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
