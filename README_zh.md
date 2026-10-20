# Remote Computer Use for Codex

[English](README.md) | 简体中文

基于 OpenAI Computer Use 示例改造的 Codex 插件，将本地桌面输入换成 RDP 和 VNC，保留持久 Python 执行方式：变量可跨调用保留，一次调用能组合截图、操作和循环。每个具名连接拥有独立的 worker 和输入状态。

## 安装

安装 Codex、Python 3.12 和 `uv`，确保 `uv` 在 `PATH` 中。从 [Releases 页面](https://github.com/Napreth/RemoteComputerUse-for-Codex/releases)的 **Assets** 下载 `remote-computer-use-1.0.0.zip`。进入 Codex 客户端，点击侧边栏的 **Customize**，进入 **Plugins → 添加 → 上传插件压缩包**，选择下载的 ZIP，无需解压。从设置页进入 Plugins 可能没有上传选项。

Codex 通过 `uv` 和项目锁文件启动内置的 `remote_computer` MCP 服务。首次启动可能联网安装依赖；当前启动兼容性记录见[发布兼容性](docs/release-compatibility.md)。

## 配置

自行创建 `config.json`，插件不会自动创建或迁移配置文件：

| 平台 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\remote-computer-use\config.json` |
| Linux，已设置绝对路径的 `XDG_CONFIG_HOME` | `$XDG_CONFIG_HOME/remote-computer-use/config.json` |
| Linux，其他情况 | `$HOME/.remote-computer-use/config.json` |

```json
{
  "version": 1,
  "profiles": [
    {"name": "vm-dev", "type": "rdp", "host": "192.0.2.10", "username": "alice", "password": "<你的密码>"},
    {"name": "vm-test", "type": "vnc", "host": "192.0.2.11", "passwdenv": "VM_TEST_PASSWORD"}
  ]
}
```

Profile 名不能重复，`password` 与 `passwdenv` 二选一；后者引用 MCP 进程已有的环境变量，不会要求 Codex 自动透传。RDP 必填 `host`、`username`，默认端口为 3389、画面为 1440×900；VNC 必填 `host`，默认端口为 5900，仅支持共享会话。`host` 填主机名或 IP，端口单独填写。

每次连接重新读取配置，修改不影响已有会话。原有 `REMOTE_COMPUTER_*` 环境配置仍可在不指定 `profile` 时使用。运行时不加载 `.env`，也不设置 `UV_*` 默认值。可选字段、环境变量和 Labs 设置见[配置参考](docs/configuration.md)。

AI 填写功能默认关闭。在配置顶层设置 `enableDeclareAsFillByAgent: true`，可让 AI 填写 `fillByAgent: ["host", "password"]` 等声明字段，这些字段必须从 Profile 中省略；`name`、`type` 和 `passwdenv` 不允许填写。设置 `enableProfileFromAgent: true`，可开放直接提供连接参数的工具。填写值仅用于当次连接，不写回文件。示例见[AI 填写配置](docs/configuration.md#agent-supplied-connection-fields)。

## 使用

新开 Codex 会话，告诉它使用已配置的远程计算机。基本工具流程如下：

```text
computer_list_profiles()
computer_connect(profile="vm-dev", name="work")
computer_exec_py(session="work", code="display(computer.screenshot())")
computer_disconnect(session="work")
```

`profile` 选择保存的配置，`name` 标识运行会话。只有首次连接可省略 `name`，此时名称为 `default`；只连接一台计算机时，操作可省略 `session`。用 `computer_list()` 查看会话，`computer_status()` 查看状态。

`computer_exec_py` 预置了 `computer`、`pyautogui`、`Image`、`log` 和 `display`，例如：

```python
display(computer.screenshot())
computer.click(420, 300)
computer.write("Hello")
display(computer.screenshot())
```

`import pyautogui` 也使用远程兼容层。截图返回 Pillow 图像，坐标与输入一致。`computer_click`、`computer_screenshot`、`computer_run_actions` 等原子工具共用同一连接。支持的调用、滚动约定及后端限制见[兼容性说明](docs/upstream-compatibility.md)。

## 安全与恢复

`computer_exec_py` 在 MCP 服务的操作系统账号下执行完整 Python，**不是沙盒**。RDP/VNC 输入作用于远程桌面，但 Python 仍可访问宿主机文件、进程和网络，因此同账号代码也能读取配置中的明文密码。请把配置放在仓库和 ZIP 外并限制权限；不可信代码应放在隔离账号或虚拟机中运行。默认工具没有密码参数；开启 AI 填写后，密码可出现在相应工具参数和会话／客户端记录中。

VNC 支持 RFB 3.8、Raw 编码及 None/VNC 密码认证，不提供 TLS，请使用受保护的网络或隧道。输入中断时会限时尝试释放按键和鼠标按钮；若清理失败，请先恢复远程输入状态，再重启插件服务以复用被阻塞的会话名。

## 开发

运行 `python scripts/package_plugin.py`（Python 3.12+，仅标准库）或 `pnpm plugin:pack` 打包，默认生成 `dist/remote-computer-use-1.0.0.zip`；可用 `--output <路径>.zip` 指定目标。包中包含内置 MCP 运行时和文档，排除本地配置及开发产物，并固定 ZIP 元数据以便重复构建。

`pnpm check` 执行 Python、Console、契约、Labs 和构建检查。`pnpm dev:python` 启动保留的 runner 和 Console，`pnpm start:python` 使用构建后的 Console。旧 JavaScript/Playwright 后端已停用。

发布时先更新 `plugin.json` 的版本号并提交，再推送同版本 tag（可带 `v` 前缀）。GitHub Actions 会等待 Linux 和 Windows 检查全部通过，然后构建插件 ZIP，通过 [softprops/action-gh-release](https://github.com/softprops/action-gh-release) 发布。Release 标题为“{tag 名} - {tag 注释第一行}”。正文使用第一行之后的内容，从第一个非空行开始；后面没有非空行时，正文为空。轻量 tag 没有注释时，标题只使用 tag 名，正文为空。带预发布后缀的语义版本会标记为预发布。版本不匹配或检查失败时不会发布。使用内置 `GITHUB_TOKEN`，无需额外配置 Secret。

```sh
git tag -a v1.0.0 -m "发布标题" -m "在这里填写本次发布说明"
git push origin v1.0.0
```

发布时使用下一个尚未使用的版本号，以上 `v1.0.0` 仅为示例。多段 Markdown 发布说明可先写入文件，再用 `git tag -a <tag> --cleanup=verbatim -F <说明文件>` 保留格式和 `#` 标题。

## 许可证

[MIT](LICENSE)，Copyright (c) 2026 Napreth。保留了继承的 [OpenAI 许可证](LICENSES/OpenAI-CUA-Sample-App-LICENSE)和[示例 README](docs/upstream/openai-sample-README.md)。
