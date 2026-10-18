# Remote Computer Use for Codex

[English](README.md) | 简体中文

Remote Computer Use 是基于 OpenAI Computer Use 示例改造的 Codex 插件。它保留了上游“让模型编写 Python，并在持续运行的进程中执行”的方式，将本地桌面输入后端换成 RDP 和 VNC，让截图与鼠标键盘操作发生在远程计算机上。旧的 JavaScript/Playwright 后端已停用。

一次 `computer_exec_py` 调用可以连续截图、操作、检查像素并循环等待；同一连接的 Python 全局变量会在多次调用之间保留。插件内置的 MCP 服务器可以同时管理多个具名远程会话，每个会话有自己的 Python worker、连接和输入状态。

## 安装

先安装 Codex、`uv` 和 Python 3.12，并确保命令行可以找到 `uv`。打开[项目代码仓库](https://github.com/Napreth/RemoteComputerUse-for-Codex)，点击 **Code → Download ZIP** 下载代码压缩包，无需解压。进入 Codex 客户端，点击侧边栏的 **Customize**，进入 **Plugins**，点击 **添加**，再点击 **上传插件压缩包**，选择刚下载的 ZIP。请从侧边栏的 Customize 进入；从设置页打开 Plugins 时，可能看不到「上传插件压缩包」选项。使用源码开发时，可在仓库根目录运行 `uv sync --project python-app --frozen`。

根目录的[插件清单](plugin.json)会发现 `skills/` 和 [mcp.json](mcp.json) 中内置的 `remote_computer` 服务。Codex 直接启动 `uv run --frozen --no-dev --project ${PLUGIN_ROOT}/python-app remote-computer-use`，将 `${PLUGIN_ROOT}` 展开为插件安装根目录，并使用项目锁文件。用户无需寻找插件安装位置或填写源码路径。插件不会接管宿主机的鼠标、键盘或本地浏览器。

首次启动可能需要联网安装锁定的 Python 依赖。2026-10-15 核对的 [Codex main 实现](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/rmcp_client.rs)默认给 MCP 启动 30 秒、工具调用 300 秒；Agent Plugins 1.0.0 的 `mcp.json` 没有超时字段，已公布的插件用户策略也没有超时覆盖入口。**1.0 发布仍受阻：干净 Windows 来宾的冷缓存启动超过 30 秒，未完成初始化。**详见[发布兼容性](docs/release-compatibility.md)。只验证有缓存时的依赖安装，不能证明真实首次安装兼容。

## 配置远程连接

由用户创建并维护 `config.json`：Windows 路径为 `%LOCALAPPDATA%\remote-computer-use-for-codex\config.json`；Linux 路径为 `${XDG_CONFIG_HOME:-$HOME/.config}/remote-computer-use-for-codex/config.json`。`XDG_CONFIG_HOME` 未设置或为相对路径时使用 `$HOME/.config`；Codex 未传入 `LOCALAPPDATA` 时，运行时直接查询当前用户的 Windows 文件夹。插件不会自动生成配置。`version` 使用整数 `1`，profile 名不能重复，每个 profile 只能选择一种密码来源：

```json
{
  "version": 1,
  "profiles": [
    {"name": "vm-dev", "type": "rdp", "host": "192.0.2.10", "username": "alice", "password": "<你的密码>"},
    {"name": "vm-test", "type": "vnc", "host": "192.0.2.11", "port": 5900, "passwdenv": "VM_TEST_PASSWORD"}
  ]
}
```

先调用 `computer_list_profiles()`，仅获取名称和协议类型，再使用 `computer_connect(profile="vm-dev", name="work")`。`profile` 选择保存的连接配置，`name` 标识运行会话，二者相互独立。选择 profile 后不能同时提供显式连接参数，避免将已保存密码用于另一个目标。每次连接都重新读取并验证文件；修改只影响新连接，已有连接及其 emergency release 保留连接时解析出的配置。无效 JSON、重复名称或属性、未知字段，以及同时设置 `password`／`passwdenv` 都会报错，错误信息不回显文件内容。文件大小上限为 1 MiB。

RDP profile 必须提供 `host`、`username`，可选 `port`（3389）、`domain`（空字符串）、`width`（1440）、`height`（900）、`insecure_tls`（false）。VNC profile 必须提供 `host`，可选 `port`（5900）、`shared`（只能为 true）；当前不支持独占会话，`shared=false` 会在 profile 校验时被拒绝。`password` 必须为字符串；两种密码字段都不填时使用空密码。`passwdenv` 是 MCP 进程已有环境变量的名称，只允许字母、数字和下划线，不能以数字开头；变量不存在时报错，不会自动要求 Host 透传，也不会加载 `.env`。启动 worker 前会清除配置中引用的凭据变量，父进程保留已解析密码供清理使用。

`host` 可以为 DNS 主机名、`rdp_service` 或 `win_host.local` 等 ASCII 内网名称、IPv4 或 IPv6 地址；IPv6 可带方括号。国际化 DNS 名使用 IDNA2008 和 UTS #46 非过渡式处理，例如 `faß.de` 转换为 `xn--fa-hia.de`，不会改成 `fass.de`。名称会转换为小写，支持单段计算机名。下划线仅允许用于完全由 ASCII 字符组成的内网名称；每段长度为 1–63 个字符，只允许字母、数字、下划线和连字符，连字符不能出现在段首或段尾。端口须单独填写，不接受 URL、用户信息、路径、查询参数、片段、百分号转义、IPv6 scope 标识或有歧义的非标准 IPv4 数字写法。RDP URL 构造器也会校验直接创建的配置中的主机和端口。

缺少 `config.json` 时，`computer_list_profiles()` 返回空列表且不创建文件；原有环境变量用户可继续使用不带 `profile` 的 `computer_connect(name="work")`。显式指定 profile 时，配置文件缺失仍会报错。格式错误或不可读的文件也不会被静默忽略。

明文 `password` 可被同一账号下的 `computer_exec_py` 读取。请限制配置文件权限，例如 Linux 使用 `0600`、Windows 使用仅当前用户可读的 ACL，不要放入仓库或插件 ZIP；这些权限无法隔离同账号 Python。插件不提供写入凭据的工具，连接工具也不接受密码参数。

`UV_*` 完全可选，交由 uv 按 Host 已提供的值解释。插件不设置 uv 默认值，不声明环境变量透传白名单，也不保存凭据值。

显式连接参数只有在省略或为 `None` 时才继承对应环境配置。空 `host`、空 `backend` 或空 RDP `username` 会被拒绝；`domain=""` 则明确清除环境中提供的 RDP 域。端口和尺寸必须为支持范围内的整数，零、布尔值和浮点数会报错，不会被环境值或默认值替换。

原有 `computer_connect(backend=..., host=...)` 调用仍保留，并读取 MCP 环境中的 `REMOTE_COMPUTER_*`。[内置 MCP 用户级环境配置缺口](https://github.com/openai/codex/issues/36854)仍是上游问题：Host 无法注入引用变量时，可在用户 profile 中直接提供明文密码。Profiles 为连接参数提供了不依赖该 Host 能力的入口，不管理 uv，也不增加环境透传名单。没有目标配置时，MCP 仍可初始化和列出工具，实际连接时报配置错误。下面的环境变量示例仅适用于 Host 已提供这些值的情况：

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

上传插件 ZIP，并创建用户 Profiles 文件后，新开 Codex 会话并确认内置 `remote_computer` 工具可用，再连接远程计算机、截图或执行 Python。以下是一个具名会话的调用示意；如果只连接一台计算机，后续工具可省略 `session`：

```text
computer_list_profiles()
computer_connect(profile="vm-dev", name="win-dev")
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

运行 `python scripts/package_plugin.py`（Python 3.12+，仅使用标准库）或 `pnpm plugin:pack` 生成上传用 ZIP，默认输出为 `dist/remote-computer-use-1.0.0.zip`，名称和版本取自 `plugin.json`；可用 `--output <路径>.zip` 指定目标。脚本通过自身路径定位源码，因此也可从其他工作目录运行，且下载的源码 ZIP 解压后无需 Git 即可打包。脚本先在临时目录整理插件清单、MCP 运行时及锁定的 Python 项目、Skill、许可证和用户文档，不包含本地配置、虚拟环境、缓存、测试、Console 或 Labs。归档条目顺序、时间戳和权限固定，便于重复构建；源文件中的链接会被拒绝。生成后从 **Customize → Plugins → 添加 → 上传插件压缩包** 导入。打包不代表已通过暂缓处理的发布兼容性检查。

在仓库根目录运行 `pnpm check`，执行 Python、Console、契约和 Labs 检查及 Console 生产构建。`pnpm dev:python` 启动 Python runner 和 Console，`pnpm start:python` 使用构建后的 Console；两者都继承启动它们的进程环境，不读取仓库 `.env`。旧 JavaScript/Playwright 后端仅作为历史参考，不属于当前构建目标。完整 Python 框架测试可运行 `uv run --frozen --project python-app pytest -s -p no:cacheprovider python-app/tests`。

## 许可证

项目采用 MIT 许可证，Copyright (c) 2026 Napreth。继承的 OpenAI 示例许可证保存在 [`LICENSES/OpenAI-CUA-Sample-App-LICENSE`](LICENSES/OpenAI-CUA-Sample-App-LICENSE)，原始示例 README 保存在 [`docs/upstream/openai-sample-README.md`](docs/upstream/openai-sample-README.md)。
