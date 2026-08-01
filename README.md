# EasyType

用手机为 Windows 电脑补上共享文本板和语音输入。

EasyType 运行在电脑上，手机和电脑通过家庭 Wi‑Fi 访问同一网页。正式版提供单文件 EXE，无需安装 Python、uv 或其他依赖。

当前版本：**v1.4.0** · [下载 EXE](https://github.com/Angelysss/easytype/releases/latest)

## 主要功能

- 多个实时共享板：默认 3 个，最多 8 个，支持新建、重命名和删除
- 手机与电脑双向同步，断线重连并保留未发送内容
- 冲突时手动选择版本，不会静默覆盖
- 手机一键插入、回车和清空，插入后可自动清空
- 直输模式：把手机键盘或语音输入实时发送到 Windows 当前窗口
- Markdown 纯文本编辑辅助，自动延续列表、任务列表和引用标记
- 配对模式与信任模式，适配不同的家庭网络习惯
- 文本和设置本地持久保存，重启后自动恢复
- 系统托盘、开机自启动和内置网络修复

## 快速开始

1. 从 [Releases](https://github.com/Angelysss/easytype/releases) 下载最新版 `EasyType-<版本>.exe`。
2. 双击运行；首次配置网络访问时允许 Windows 管理员确认。
3. 点击系统托盘中的 EasyType 图标打开电脑页面。
4. 在页面底部查看访问地址，例如 `http://192.168.0.12:5000`。
5. 手机连接同一 Wi‑Fi，并用浏览器打开该地址。

如果以后无法访问，可在电脑页面的“访问设置”中点击“修复网络访问”。

## 使用方式

### 共享板

手机和电脑可以停留在不同的共享板。停止输入约 200 ms 后自动同步；其他板收到更新时会显示黄色提示点。

- 右上角弧线：进入或退出沉浸式编辑
- 手机“插入”：把当前共享文本粘贴到电脑焦点窗口
- 手机“回车”：向电脑发送回车键
- “关于”：设置 Markdown 辅助和插入后自动清空

### 直输

1. 在电脑上点选 ChatGPT、记事本等目标输入框。
2. 手机切换到“直输”。
3. 点击波浪图标，使用手机键盘或语音输入。
4. 再次点击波浪图标停止。

同一时间只允许一台设备直输。退格和回车在未开启直输时也可以使用。

## 访问模式

访问模式由运行 EasyType 的电脑统一决定，可在电脑页面切换。

| 模式 | 说明 |
| --- | --- |
| 配对模式 | 浏览器首次访问需要扫码授权；凭证有效期为 180 天，可单独撤销 |
| 信任模式 | 同一家庭局域网内无需配对，打开地址即可使用 |

配对凭证属于浏览器 Cookie。更换浏览器、清除 Cookie 或使用无痕模式后需要重新配对。

## 从源码运行

需要 Windows 10/11、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```powershell
git clone https://github.com/Angelysss/easytype.git
cd easytype
uv sync
uv run python main.py
```

运行测试和构建 EXE：

```powershell
uv sync --all-groups
uv run pytest
.\build-exe.ps1
```

构建结果位于 `dist\EasyType-<版本>.exe`。

## 配置与数据

| 环境变量 | 默认值 |
| --- | --- |
| `EASYTYPE_PORT` | `5000` |
| `EASYTYPE_DATA_DIR` | `%LOCALAPPDATA%\EasyType` |

共享板、授权设备和设置都保存在 `%LOCALAPPDATA%\EasyType`，不会上传到云端。

## 安全说明

EasyType 仅面向可信家庭局域网，使用局域网 HTTP：

- 不要映射端口到公网
- 不要用它传输密码、验证码或密钥
- 普通权限运行的 EasyType 无法向管理员权限程序模拟输入
- 当前 EXE 未使用商业代码签名，Windows 可能显示“未知发布者”

## License

[MIT License](LICENSE)
