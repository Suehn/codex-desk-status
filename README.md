# Codex Desk Status · 会话状态

在 Codex 桌面端的对话旁查看上下文占用、额度快照、缓存命中率、输出速度、费用估算和 Git 状态。

Python 标准库实现的本地 MCP 插件，无需 pip 依赖或 API Key。运行时只读本机会话日志，不发送网络请求。

[下载安装包](https://github.com/Suehn/codex-desk-status/releases) · [指标怎么算](docs/metrics.md) · [更新记录](CHANGELOG.md)

## 安装

需要 Python 3.8+、支持插件的 Codex 桌面端，以及 PATH 中可运行的 `codex` CLI。CLI 可以安装插件；图形面板需要支持 MCP Apps 的桌面宿主。首个公开版本为预览版。

### 从 GitHub 安装（macOS / Linux）

先确认 `python3 --version` 和 `codex --version` 能正常运行，然后执行：

```sh
codex plugin marketplace add Suehn/codex-desk-status
codex plugin add desk-status@codex-desk-status
```

完全退出并重新打开 Codex。在对话右上角的菜单里选择 **Open app → 会话状态**；菜单名称可能因版本不同而变化。如果宿主未传入当前对话 ID，在面板中手动选择会话。

这使用的是 [Codex 仓库 marketplace](https://developers.openai.com/plugins/build/plugins#add-a-marketplace-from-the-cli)，无需加入官方公共插件目录。

### 下载 ZIP 安装

从 [Releases](https://github.com/Suehn/codex-desk-status/releases) 下载 `desk-status-plugin-版本.zip`，解压后进入 `desk-status` 目录。

macOS / Linux / WSL：

```sh
python3 scripts/install.py --marketplace ~/codex-desk-status-local --register
```

Windows PowerShell（需要可用的 Python 与 Codex CLI）：

```powershell
py scripts/install.py --marketplace "$env:USERPROFILE\codex-desk-status-local" --python python --register
```

如果 Windows 只有 `py` 命令，将 `--python python` 换成 `--python py`。解释器使用 PATH 中的程序名，不填写机器专属的绝对路径。Windows / WSL 路径处理已有实现，但本次发布尚未实机验收这两个环境。

不加 `--register` 时，安装脚本只把文件暂存到指定目录，并打印注册命令。安装脚本会写入插件目录；注册由 Codex CLI 写入配置和缓存。面板运行时不修改会话日志。

## 更新和卸载

通过 GitHub marketplace 安装的用户：

```sh
codex plugin marketplace upgrade codex-desk-status
codex plugin add desk-status@codex-desk-status
```

更新后完全重启桌面应用。ZIP 用户下载新包，在同一个 marketplace 目录重新运行安装命令。

如果之前安装的是本地版（如 `desk-status@desk-status-local` 的 0.2.2），下载新包或更新源码后，在新版本目录执行：

```sh
python3 scripts/install.py --update-installed
```

这会通过 `codex plugin list --json` 找到现有本地 marketplace，更新源文件并重新安装 Codex 缓存。只更新源码或另加一个 GitHub marketplace，不会替换原来启用的本地版。此命令要求恰好存在一个已启用的本地安装；多个安装时用 `--marketplace DIR --register` 明确指定。Windows 使用 `py scripts/install.py --update-installed --python python`。

安装完成后关闭并重新打开「会话状态」，在右下角菜单的「诊断信息」中核对版本为 **0.2.5**。如果仍是旧版，等当前任务结束后再重启 Codex。

GitHub 安装的卸载命令：

```sh
codex plugin remove desk-status@codex-desk-status
codex plugin marketplace remove codex-desk-status
```

ZIP 安装的卸载命令（使用安装时的目录）：

```sh
python3 scripts/install.py --marketplace ~/codex-desk-status-local --uninstall --register
```

安装脚本保留同一 marketplace 的其他插件，只有目录中已无其他插件时才移除 marketplace 注册。

## 能看到什么

| 指标 | 含义 |
| --- | --- |
| 上下文 | 最近响应的上下文占用；压缩后等待新读数 |
| 额度 | 日志中的额度快照及记录时间，可能滞后 |
| 缓存 | 本次和最近几次响应的提示缓存命中率 |
| 速度 | 同模型、同推理强度最近 5 个有效样本的加权 tok/s |
| 费用 | 按随包价表计算的 API 价格估算，不能作为订阅账单 |
| 会话 / Git | 模型、推理强度、工作目录、分支及改动数量 |

支持浅色 / 深色、手动选择会话、历史读数和分段日志。费用价表及缓存保留规则沿用原实现，本次分发整理没有重新核验其与服务端当前规则的一致性；精确定义和局限见 [指标说明](docs/metrics.md)。

## 配置和排障

服务默认读取 `$CODEX_HOME/sessions`，未指定时为 `~/.codex/sessions`。WSL 默认也会扫描 `/mnt/c/Users/*/.codex/sessions`；可用 `DESK_STATUS_WSL_WINDOWS_HOMES=0` 关闭。

| 配置 | 用途 |
| --- | --- |
| `DESK_STATUS_SESSION_ROOTS` / `--sessions-root DIR` | 自定义日志根目录；环境变量中多个目录用系统路径分隔符分开 |
| `DESK_STATUS_GIT=0` / `--no-git` | 关闭 Git 查询 |
| `DESK_STATUS_LOG_LEVEL` / `--log-level` | 设置 stderr 日志级别 |

如果面板不可用，先检查 Python 是否在 Codex 进程的 PATH 中，以及插件是否启用。可在插件目录运行：

```sh
python3 -B server/desk_status_server.py --print-roots
```

如果额度、子代理费用能显示，但主代理的上下文、缓存、速度都是「—」，并一直显示「已中断 / 还没有完成的请求」，先核对诊断信息中的插件版本。0.2.2 只读取会话最初的日志；当前 Codex 可能把实际请求写入后续分段。0.2.4 起已有分段读取支持，0.2.5 增加了上述本地更新入口和这一故障形态的回归检查。

插件仅能读取它所在系统上的日志：本机运行的插件不能自动读取另一台主机上的会话。Codex 日志格式和宿主绑定字段可能随版本改变；没有足够数据时应显示未知，发现错误可提交 Issue。请勿在 Issue 中上传原始会话日志、聊天正文或凭据。

## 隐私

发布包不包含会话记录或账号配置。运行时读取本地日志并生成指标，面板可显示会话 ID、工作目录和 Git 分支；分享截图前请检查这些信息。Git 查询只调用带超时的只读命令。面板资源自包含，无外部脚本、字体或网络域名。

## 开发和维护

```sh
git clone https://github.com/Suehn/codex-desk-status.git
cd codex-desk-status
python3 scripts/build_ui.py
DESK_STATUS_SKIP_REAL=1 python3 -B -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/package.py
```

Python 测试包含协议、日志解析、计价和安装打包验证。部分面板测试需要 Node.js；没有 Node 时会跳过对应项。要读取本机日志做只读冒烟验证，移除 `DESK_STATUS_SKIP_REAL=1`。

浏览器测试使用合成数据和 mock host，不等同于原生 Codex 面板验收：

```sh
npm install --no-save --package-lock=false playwright
npx playwright install chromium
node tests/browser/panel.test.cjs
```

`ui/src` 是界面源码，`ui/dist/panel.html` 是随插件提交的构建产物。`server/desk_status` 是服务器；`tests` 和 `harness` 是测试与演示工具。GitHub Actions 检查测试、构建同步和打包；后续发布需同步版本号、更新 CHANGELOG，运行检查，再创建 tag 和 Release，上传 `dist` 中的两个 ZIP。

## 许可证与来源

基于原本地 **desk-status / Clepsydra** 项目整理，保留原作者 zhuangziyi 的 MIT 版权声明。公开分发与后续维护仓库：Suehn/codex-desk-status。本项目是社区插件，与 OpenAI 没有隶属关系。
