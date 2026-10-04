# Remote Computer Use for Codex

[English](README.md) | 简体中文

Remote Computer Use 是基于 OpenAI Computer Use 示例改造的 Codex 插件。它保留了上游“让模型编写 Python，并在持续运行的进程中执行”的方式，将本地桌面输入后端换成 RDP 和 VNC，让截图与鼠标键盘操作发生在远程计算机上。旧的 JavaScript/Playwright 后端已停用。

一次 `computer_exec_py` 调用可以连续截图、操作、检查像素并循环等待；同一连接的 Python 全局变量会在多次调用之间保留。插件内置的 MCP 服务器可以同时管理多个具名远程会话，每个会话有自己的 Python worker、连接和输入状态。

## 安装

先安装 Codex、`uv` 和 Python 3.12，并确保命令行可以找到 `uv`。打开[项目代码仓库](https://github.com/Napreth/RemoteComputerUse-for-Codex)，点击 **Code → Download ZIP** 下载代码压缩包，无需解压。进入 Codex 客户端，点击侧边栏的 **Customize**，进入 **Plugins**，点击 **添加**，再点击 **上传插件压缩包**，选择刚下载的 ZIP。请从侧边栏的 Customize 进入；从设置页打开 Plugins 时，可能看不到「上传插件压缩包」选项。使用源码开发时，可在仓库根目录运行 `uv sync --project python-app --frozen`。

根目录的[插件清单](plugin.json)会发现 `skills/` 和 [mcp.json](mcp.json) 中内置的 `remote_computer` 服务。Codex 直接启动 `uv run --frozen --no-dev --project ${PLUGIN_ROOT}/python-app remote-computer-use`，将 `${PLUGIN_ROOT}` 展开为插件安装根目录，并使用项目锁文件。用户无需寻找插件安装位置或填写源码路径。插件不会接管宿主机的鼠标、键盘或本地浏览器。

首次启动可能需要联网安装锁定的 Python 依赖。Agent Plugins 1.0.0 的 `mcp.json` 没有启动或工具调用超时字段，这些限制由 Codex Host 控制。

## 配置远程连接

连接变量由 Codex Host 提供给内置 MCP 进程。选择 `REMOTE_COMPUTER_BACKEND=rdp` 或 `vnc` 并提供对应目标参数：RDP 必须有主机地址和用户名，目标要求密码认证时还需提供密码；VNC 必须有主机地址，服务器启用 VNC 密码认证时还需提供密码。插件直接启动 uv，不改写 `REMOTE_COMPUTER_*` 或 `UV_*`。密码只从运行时环境读取，不接受工具参数；运行时不加载 `.env` 文件。缺少必要连接参数时，MCP 仍可初始化并列出工具，实际调用 `computer_connect` 时返回配置错误。

`UV_*` 完全可选，交由 uv 按 Host 已提供的值解释。插件不设置 uv 默认值，不声明环境变量透传白名单，也不保存凭据值。

Host 兼容性要求：目标 Codex 版本必须能把所需变量注入内置 MCP 进程。[当前官方插件文档](https://developers.openai.com/plugins/build/plugins)只说明了用户级启用、审批和工具访问策略，没有公布内置 MCP 的用户级 transport `env`／`env_vars` 覆盖入口。如果 Host 无法提供这些值，凭据配置仍是上游阻塞项。目前尚未验证具备该能力的最低 Codex 版本。

RDP 示例：

```text
REMOTE_COMPUTER_BACKEND=rdp
REMOTE_COMPUTER_RDP_HOST=192.0.2.10
REMOTE_COMPUTER_RDP_USERNAME=codex
REMOTE_COMPUTER_RDP_PASSWORD=你的密码
```

VNC 示例：

```text
REMOTE_COMPUTER_BACKEND=vnc
REMOTE_COMPUTER_VNC_HOST=192.0.2.11
REMOTE_COMPUTER_VNC_PASSWORD=你的密码
```

RDP 可选变量包括 `REMOTE_COMPUTER_RDP_PORT`（默认 `3389`）、`REMOTE_COMPUTER_RDP_DOMAIN`、`REMOTE_COMPUTER_RDP_WIDTH`（默认 `1440`）、`REMOTE_COMPUTER_RDP_HEIGHT`（默认 `900`）和 `REMOTE_COMPUTER_RDP_INSECURE_TLS`。VNC 可选变量包括 `REMOTE_COMPUTER_VNC_PORT`（默认 `5900`）和 `REMOTE_COMPUTER_VNC_SHARED`。VNC 当前支持 RFB 3.8、Raw 编码，以及 None 或传统 VNC 密码认证；传统 VNC 密码认证不加密画面和输入，请使用受保护的网络或隧道。

## 开始使用

上传插件 ZIP，并通过能提供环境变量的 Host 配置目标后，新开 Codex 会话并确认内置 `remote_computer` 工具可用，再连接远程计算机、截图或执行 Python。以下是一个具名会话的调用示意；如果只连接一台计算机，后续工具可省略 `session`：

```text
computer_connect(name="win-dev", backend="rdp", host="192.0.2.10", username="codex")
computer_screenshot(session="win-dev")
computer_exec_py(session="win-dev", code="display(computer.screenshot())")
computer_disconnect(session="win-dev")
```

第一次连接可以省略 `name`，此时会话名为 `default`；一旦已有注册会话，新的连接就必须显式指定名称。连接多台计算机时，每次操作都应指定 `session`，避免选错目标。可用 `computer_list()` 查看会话，`computer_status(session="win-dev")` 查看连接状态。

推荐使用 `computer_exec_py` 连续完成多步操作。例如，下面的代码在同一远程会话中截图、点击并再次截图：

```python
display(computer.screenshot())
computer.click(420, 300)
display(computer.screenshot())
```

这个 worker 预置 `computer`、`pyautogui`、`Image`、`log` 和 `display`。`import pyautogui` 也会使用远程兼容层，不会导入宿主机输入库。普通 Python 全局变量会保留到后续 `computer_exec_py` 调用。`computer.screenshot()` 返回 Pillow 图像，坐标与输入操作使用同一远程 framebuffer。

还可以使用 `computer_move`、`computer_click`、`computer_drag`、`computer_scroll`、`computer_type`、`computer_keypress` 和 `computer_run_actions` 等原子工具。`computer_run_actions` 一次最多执行 50 个经过校验的动作并返回截图。原子工具与 Python 代码共用会话及最后一次成功发送的指针位置；无坐标 `computer.click()` 使用该位置。更多 PyAutoGUI 行为和远程限制见[兼容性说明](docs/upstream-compatibility.md)。

`computer.scroll(10)` 表示向上滚动十格，`computer.scroll(-10)` 表示向下滚动十格；原子工具 `computer_scroll` 和批量动作则使用显式协议增量，正值表示向下或向右，每格为 120。RDP 目前只支持垂直滚动，VNC 支持水平和垂直滚动。

## Labs 与 Windows 客体

保留的 Labs 工作流要求 Windows 客体能够使用 Win+R 并运行 Edge。设置 `REMOTE_COMPUTER_GUEST_OS=windows` 后，runner 会在客体可访问的宿主机接口上提供所选 lab，并通过客体打开该 URL。VNC 还需要先验证其服务端会转发 Win+R，再显式设置 `REMOTE_COMPUTER_VNC_WINDOWS_NAVIGATION=win-r`；其他 VNC 快捷键映射和 Windows 布局没有自动导航支持。

网络环境特殊时，可设置 `REMOTE_COMPUTER_LAB_HOST`（客体 URL 中的宿主机地址）、`REMOTE_COMPUTER_LAB_BIND_HOST`（监听接口）、`REMOTE_COMPUTER_LAB_PORT`（`0` 为随机端口），以及在转发或 NAT 改变 HTTP 来源地址时设置 `REMOTE_COMPUTER_LAB_GUEST_HOST`。所选地址和端口必须允许客体通过防火墙访问。每次 lab 运行都使用新的随机 capability 路径，并限制允许请求的来源；访问 URL 中含有令牌，运行期间不要分享。客体成功 GET 页面只证明页面已送达，不证明 JavaScript 已完成渲染。

## 安全与恢复

`computer_exec_py` 执行的是可信主机代码，**不是沙盒**：持久 Python 子进程与 MCP 服务器使用同一操作系统账号，可以访问该账号有权访问的文件、进程和网络。远程桌面隔离只保证输入不会直接控制宿主机桌面；如果生成的代码或提示内容并非完全可信，请把插件运行在低权限账号、容器或虚拟机中。

连接中断或执行被终止时，插件会尝试限时释放可能仍按住的远程按键和鼠标按钮。如果清理无法确认成功，会话名会被阻塞；先检查并恢复远程输入状态，再重启插件服务器。`computer_status` 报告的是已知的传输健康状态，不能把它当作独立的远程画面探测。

## 开发

在仓库根目录运行 `pnpm check`，执行 Python、Console、契约和 Labs 检查及 Console 生产构建。`pnpm dev:python` 启动 Python runner 和 Console，`pnpm start:python` 使用构建后的 Console；两者都继承启动它们的进程环境，不读取仓库 `.env`。旧 JavaScript/Playwright 后端仅作为历史参考，不属于当前构建目标。完整 Python 框架测试可运行 `uv run --frozen --project python-app pytest -s -p no:cacheprovider python-app/tests`。

## 许可证

项目采用 MIT 许可证，Copyright (c) 2026 Napreth。继承的 OpenAI 示例许可证保存在 [`LICENSES/OpenAI-CUA-Sample-App-LICENSE`](LICENSES/OpenAI-CUA-Sample-App-LICENSE)，原始示例 README 保存在 [`docs/upstream/openai-sample-README.md`](docs/upstream/openai-sample-README.md)。
