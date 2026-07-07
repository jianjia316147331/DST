***

name: DST违章查询
description: 当用户提到查询xxx公司/xxx车的违章、违法、违规、罚款、扣分、交通违法、12123时调用。通过PinchTab控制Chrome访问12123平台查询车辆违章，支持单/批量查询，支持车牌号自动识别省份。输出MD报告+飞书文档+多维表格。
------------------------------------------------------------------------------------------------------------------------------------

# DST违章查询

## 概述

通过 **PinchTab**（12MB Go 二进制，零依赖）自动操作 Chrome 访问 12123 平台，完成单位用户扫码登录后查询车辆违章。输出：本地 MD 报告 + 飞书云文档 + 飞书多维表格。支持防退出保活和飞书通知。

> PinchTab 内置 daemon 模式、持久 session、accessibility tree 快照（带 ref 编号）、stealth 注入和人体化操作。后台常驻，所有 CLI 命令共用同一个 Chrome session，无反复建连开销。

### 📂 文件存储约定

所有本地文件统一保存在 **项目根目录下的** **`违章查询/`** **文件夹**中，目录结构如下：

```
违章查询/
├── screenshots/   # 二维码截图、页面截图
├── reports/       # MD 格式查询报告
└── data/          # SQLite 数据库 (violations.db)
```

首次执行 `init` 时自动创建所有子文件夹。

### 🔧 Linux 命令执行模型

**核心规则：`python3` / `lark-cli` / `pinchtab` 均在 PATH 上，直接调用即可。含中文路径或中文参数的操作推荐通过 Python 脚本的 subprocess（list 形式）完成以确保稳妥。**

```
执行流程：
1. Write tool 将 Python 脚本写入 /home/openclaw/<script>_{pid}.py
2. Bash 直接执行：python3 /home/openclaw/<script>_{pid}.py
3. Python 内部通过 subprocess（list 形式，不走 shell）调用 pinchtab / lark-cli
4. 结果通过 helper 的 --output FILE 写入 UTF-8 文件，外部脚本读取文件获取结果
```

**工具路径（均在 PATH 上，无需硬编码）：**

| 工具          | 调用方式                                                                     |
| ----------- | -------------------------------------------------------------------------- |
| Python      | `python3`（`/opt/aiext/bin/python3`） |
| lark-cli    | `lark-cli`（`/home/openclaw/.npm-global/bin/lark-cli`） |
| pinchtab    | `pinchtab`（`/home/openclaw/.npm-global/bin/pinchtab`） |
| TEMP helper | `/tmp/violation_helper.py`（首次 init 时从 skill 目录复制） |
| 输出目录        | `<cwd>/违章查询/`（通过 helper `get-dir -o <file>` 安全获取）               |

## 适用场景

- 用户提到"违章"、"违法"、"交通违法"、"罚款"、"扣分"、"违规"等关键词
- 用户需要批量查询多台车的违章信息
- 用户需要保持12123平台登录状态防过期

## 核心铁律

1. **只查未处理违章：** 只对"未处理"或"未缴费"状态的违章记录点击"查看详情"获取罚款金额和记分。"已处理且已缴费"的记录直接跳过。查询前先对比 SQLite 数据库：已存在且状态未变的跳过；状态变更的（从未处理→已处理）更新记录。
2. **随机延迟反爬：** 每条违章记录查询之间间隔 2-5 秒随机，每台车之间间隔 2-5 秒随机，点击操作间隔 1-2 秒随机。触发风控时立即停止所有操作。
3. **中文参数兼容：** Linux 终端原生支持 UTF-8，含中文路径/参数可直接传入 Bash。复杂中文参数操作（如 pinchtab find/wait 含中文描述）建议通过 helper 子命令（`pt-find` / `pt-wait`）完成以确保稳妥。
4. **文件操作建议：** Linux 可直接在 Bash 中使用中文路径。为提高可靠性，推荐使用 helper 的 `get-dir` / `get-screenshot-dir` 等子命令获取路径后操作。
5. **风控熔断：** 三种触发条件：(1) 页面含"频繁"、"异常操作"、"黑名单"等关键词；(2) open_vehicle 连续 3 台车全部失败；(3) 查看详情 XHR 返回 `{"code":500,"message":"查询过于频繁"}`。任一触发立即终止所有进程并告警。XHR 监控由 `_setup_xhr_monitor()` 注入，`_check_xhr_rate_limit()` 轮询。
6. **禁止随意退出登录：** 检测到已登录状态（单位用户）时严禁退出。只需确认省份和公司匹配当前任务即可继续。只有实际查询遇到非单位用户报错时才重新登录。
7. **保活生命周期：** 登录成功后立即启动保活（每 18 分钟 reload + dismiss 弹窗）。查询正常完成后保活继续运行不停止，除非用户明确要求或会话自然过期。
8. **弹窗防御：** 每次 `get-page-vehicles`、`open-vehicle`、`collect-violations` 操作前自动检测并关闭"本人已知晓"等系统弹窗，确保表格数据可访问。
9. **🔴 铁律：先查人再开登录页（防二维码失效）：** 用户指定通知对象（姓名/手机号/群名）时，**必须先完成飞书 ID 查询（查人/查群），确认 ID 获取成功后再打开 12123 登录页截图二维码**。二维码有效期约 5 分钟，先查人可避免扫码等待期间二维码过期。
10. **🔴 铁律：批量查询必须走 helper 已有子命令：** 批量查询（全量/多台车）的循环逻辑必须通过 `violation_helper.py` 已有子命令组合实现（`get-page-vehicles` → `open-vehicle` → `collect-violations` → `go-back` → `save-detail-progress` → `click-page`）。**禁止为批量查询新写独立 Python 脚本**，只能写极简调用封装（循环体内只调 helper 子命令）。
11. **🔴 铁律：违章详情必须逐个查询（不可跳过）：** 车辆列表上显示有未处理违章的车辆，**必须进入详情页 + 逐条点击"查看详情"获取真实罚款金额和记分**。禁止只读列表数据不查详情、禁止仅凭列表摘要生成报告。此条为最高优先级铁律，违反即为查询失败。
12. **🔴 数据落库策略：查一条落库一条 → 逐条即时写入：** `collect-violations` 每提取完一条违章详情，调用方应立即通过 `db-insert-violation` 写入 SQLite。不使用中间 JSON/JS 文件暂存、不攒到最后批量落库。这样即使中途崩溃，已查询的违章数据不会丢失。helper `db-insert-violation` 支持按自然键 upsert（车牌+时间+地点+行为），重复写入不会产生脏数据。

### 中文参数兼容说明

> **注意：** Linux 终端原生支持 UTF-8，含中文路径/参数可直接在 Bash 中使用。
> 为保持最大兼容性，复杂中文参数操作（如 pinchtab find/wait 含中文描述）仍建议通过
> helper 子命令（`pt-find` / `pt-wait`）完成，确保编码安全。文件路径含中文时推荐
> 使用 helper 的 `get-dir` / `get-screenshot-dir` 等子命令获取路径。

## 前置条件

- **PinchTab** 已安装并运行 daemon：
  ```bash
  # 安装（仅首次）
  curl -fsSL https://pinchtab.com/install.sh | bash
  # 验证
  pinchtab health
  ```
  注：PinchTab 自动管理 Chrome 实例，无需手动启动 Chrome 调试端口。
- `lark-cli` 已安装并完成 `config init`（路径：`lark-cli`）
- `lark-contact`、`lark-im`、`lark-shared` 官方 skill 已安装（`npx skills add larksuite/cli -y -g`）
- 用户持有12123单位用户账号（扫码登录用）
- 飞书操作统一用 `lark-cli` CLI（不用 MCP），发消息用 `--as bot`

### ⚡ `violation_helper.py` 子命令速查

> 通过 Python 脚本调用 helper。所有子命令支持 `-o FILE` 将结果写入 UTF-8 文件。

| 子命令                  | 用途                      | 调用方式                                      |
| -------------------- | ----------------------- | ----------------------------------------- |
| `init`               | **第一步：初始化环境（含子文件夹）**    | Bash: `python <helper> init -o <file>`    |
| `get-dir`            | 输出 `违章查询/` 根目录路径        | Bash: `python <helper> get-dir -o <file>` |
| `get-screenshot-dir` | 输出 `screenshots/` 子目录路径 | Bash: `python <helper> get-screenshot-dir` |
| `get-report-dir`     | 输出 `reports/` 子目录路径     | Bash: `python <helper> get-report-dir`     |
| `get-data-dir`       | 输出 `data/` 子目录路径        | Bash: `python <helper> get-data-dir`       |
| `init-db`            | 初始化 SQLite 数据库，返回路径      | Bash: `python <helper> init-db`            |
| `db-insert-company`  | 增量写入/更新公司记录              | 通过 Python 脚本（stdin JSON 或 CLI 参数）       |
| `db-insert-vehicle`  | 增量写入/更新车辆记录              | 通过 Python 脚本（stdin JSON 或 CLI 参数）       |
| `db-insert-violation`| 增量写入/更新违章记录（按自然键匹配）    | 通过 Python 脚本（stdin JSON）                |
| `profile-lookup`    | 查公司→Profile 映射，返回 profile 信息+登录态 | 通过 Python 脚本                              |
| `profile-register`  | 注册/更新公司→Profile 映射（login 后写入，标记 is_logged_in=1） | 通过 Python 脚本                              |
| `profile-logout`    | 标记公司已登出（is_logged_in=0），保活脚本据此停止 | 通过 Python 脚本                              |
| `pinchtab-path`      | 输出 pinchtab 完整路径        | Bash: `python <helper> pinchtab-path`     |
| `lark-cli-path`      | 输出 lark-cli 完整路径        | Bash: `python <helper> lark-cli-path`     |
| `get-login-url`      | 输出单位用户登录直连 URL          | Bash: `python <helper> get-login-url`     |
| `license-lookup`     | 车牌首字→省份+URL             | 通过 Python 脚本（参数 stdin JSON 传入）            |
| `province-url`       | 省份→12123 首页 URL         | 通过 Python 脚本（参数 stdin JSON 传入）            |
| `province-login-url` | 省份→12123 首页 URL（登录后导航用） | 通过 Python 脚本（参数 stdin JSON 传入）            |
| `gen-qr-msg`         | 生成扫码通知 JSON             | 通过 Python 脚本（参数 stdin JSON 传入）            |
| `gen-qr-fallback`    | 生成降级纯文本 JSON            | 通过 Python 脚本                              |
| `gen-result-msg`     | 生成结果通知 JSON             | 通过 Python 脚本                              |
| `upload-image`       | 上传图片获取 image\_key       | 通过 Python 脚本                              |
| `send-msg`           | 发送 post 消息              | 通过 Python 脚本                              |
| `send-image-msg`     | 发送独立图片消息                | 通过 Python 脚本                              |
| `search-user`        | 按姓名查飞书用户                | 通过 Python 脚本（参数 stdin/--query 传入）         |
| `search-chat`        | 按群名搜索群                  | 通过 Python 脚本                              |
| `batch-get-id`       | 按手机号查用户                 | 通过 Python 脚本                              |
| `pt-find`            | pinchtab find 中文描述      | 通过 Python 脚本                              |
| `pt-wait`            | pinchtab wait 中文文本      | 通过 Python 脚本                              |
| `poll-login`         | 轮询飞书消息等待登录；支持动态间隔＋QR失效检测+浏览器自动检测登录＋刷新上限 | 通过 Python 脚本                              |
| `extract-message-id` | 从响应 JSON 提取 message\_id | 通过 Python 脚本                              |
| `run-js`             | 从文件执行含中文的 JS（无需经过 bash） | 通过 Python 脚本                              |
| `list-vehicles`      | 提取车辆列表+分页信息为 JSON       | 通过 Python 脚本                              |
| `open-vehicle`       | 双击第 N 台车进入详情页           | 通过 Python 脚本                              |
| `collect-violations` | 逐条点击"查看详情"提取罚金/记分，支持SQLite增量对比、详情页智能翻页、违章级断点续跑、`--auto-insert` 逐条即时落库 | 通过 Python 脚本                              |
| `go-back`            | 从详情页返回车辆列表              | 通过 Python 脚本                              |
| `click-page`         | 点击分页（next/prev/页码）；页码支持智能跳转      | 通过 Python 脚本                              |
| `save-detail-progress` | 保存进度（--company + --query-date 隔离）      | 通过 Python 脚本                              |
| `load-detail-progress` | 加载进度（--company + --query-date 隔离）           | 通过 Python 脚本                              |
| `reset-detail-progress`| 安全重置详情进度（--company + --query-date） | 通过 Python 脚本                              |
| `get-page-vehicles`    | 获取当前页车辆列表+页码+总页数         | 通过 Python 脚本                              |
| `get-login-type`       | 检测登录类型（单位/个人/未登录）        | 通过 Python 脚本                              |
| `check-login-state`    | **统一登录状态检测**：URL+DOM (Tier 1) 初始检查 + 关键字匹配 (Tier 2) 扫码轮询检测 | 通过 Python 脚本                              |
| `find-plate-page`      | 在断点页找不到上次车辆时向前搜索（最多3页） | 通过 Python 脚本                              |
| `new-tab`              | 创建新浏览器标签页，返回 tab ID（会话隔离） | 通过 Python 脚本                              |
| `switch-tab`           | 切换到指定标签页 --id \<tab_id\> | 通过 Python 脚本                              |

### 🔧 PinchTab 命令速查

| PinchTab 命令 | 对应功能 | Bash 安全 |
|---|---|---|
| `pinchtab nav "<url>"` | 导航到指定 URL | ✅ URL 不含中文即可 |
| `pinchtab screenshot -o "<path>"` | 截图保存为 PNG | ❌ 路径含中文必须走 Python |
| `pinchtab snap` | accessibility tree 快照（含 ref 编号） | ✅ |
| `pinchtab find "<描述>" --ref-only` | 自然语言找元素，只返回 ref | ❌ 描述含中文走 `pt-find` |
| `pinchtab click <ref>` | 点击指定 ref 的元素 | ✅ |
| `pinchtab eval "<js>"` | 执行 JS 表达式并返回结果 | ✅ JS 不含中文即可 |
| `pinchtab wait --text "<文本>" --timeout <ms>` | 等待文本出现在页面 | ❌ 文本含中文走 `pt-wait` |
| `pinchtab tab` | 列出所有标签页 | ✅ |
| `pinchtab tab <id>` | 切换到指定标签页 | ✅ |
| `pinchtab reload` | 刷新当前页面 | ✅ |
| `pinchtab text` | 提取页面纯文本 | ✅ |
| `pinchtab health` | 检查 daemon 健康状态 | ✅ |

> **Bash 安全规则：`✅` = 可直接 Bash 执行；`❌` = 必须通过 Python 脚本调用 helper 子命令。**

### ⚡ 身份合规速查

| 操作                        | 身份          | 原因               |
| ------------------------- | ----------- | ---------------- |
| 发送飞书消息（通知/扫码/结果）          | `--as bot`  | 消息以应用身份推送，无需用户在线 |
| 上传图片（`im images create`）  | `--as bot`  | API 仅支持 bot      |
| 按姓名查飞书用户（`lark-contact`）  | `--as user` | 需用户权限搜索通讯录       |
| 按手机号查飞书用户（`batch_get_id`） | `--as bot`  | API 限制           |
| 监听飞书消息回执（`event consume`） | `--as bot`  | 以应用身份接收事件        |

### ⚡ 第零步：自动配置权限（每次执行必做）

**执行前自动检查并写入当前工作区** **`.claude/settings.local.json`：**

1. Read `<项目根目录>/.claude/settings.local.json`（不存在则按空文件处理）
2. 将以下 allow 列表合并到已有配置中（保留已有条目，只新增缺失项）：

```json
{
  "permissions": {
    "allow": [
      "Bash(pinchtab:*)",
      "Bash(ls:*)",
      "Bash(mkdir:*)",
      "Bash(cd:*)",
      "Bash(cat:*)",
      "Bash(lark-cli:*)",
      "Bash(lark-cli contact *)",
      "Bash(lark-cli im *)",
      "Bash(lark-cli event *)",
      "Bash(lark-cli base *)",
      "Bash(lark-cli docs *)",
      "Bash(lark-cli api *)"
    ]
  }
}
```

## ⚠️ 全局交互规则

1. **禁止 AskUserQuestion 弹窗：** 所有确认交互通过对话自然语言完成
2. **意图识别优先：** 已指定信息直接使用不重复询问。根据用户提供的城市/省份/车牌信息确定省份，用于导航到正确12123平台和匹配公司。单台车查询时：省份匹配的公司仅一家则直接进入，多家则向用户确认。登录后呈现多个公司且用户信息无法唯一确定时，才需补充公司名称
3. **姓名查人优先：** 用户提供姓名时，优先通过 `lark-contact` skill 按姓名查找飞书用户（`--as user`），查找失败时再请求用户提供手机号

***

## 执行流程

> **开始执行前，必须先完成「第零步：自动配置权限」。**

### 环境初始化（每次执行第一步）

用 Write 工具写入以下脚本到 `/home/openclaw/init_violation_{pid}.py`，然后执行：

```bash
python3 /home/openclaw/init_violation_{pid}.py
```

脚本内容：

```python
import shutil, os, json, subprocess, sys

src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))),
    '.claude', 'skills', 'DST违章查询', 'violation_helper.py')
dst = '/tmp/violation_helper.py'
shutil.copy2(src, dst)

# detect lark-cli (on PATH)
lark = shutil.which('lark-cli') or 'lark-cli'

# create output dir
query_dir = os.path.join(os.getcwd(), '违章查询')
os.makedirs(query_dir, exist_ok=True)

result = {
    'helper': dst,
    'lark_cli': lark,
    'query_dir': query_dir,
    'python': sys.executable
}
print(json.dumps(result, ensure_ascii=False))
```

> 初始化完成后，后续 Python 调用使用 `python3 <HELPER路径>` 执行。关键数据通过 `-o <file>` 写入文件获取。

### 会话标签页隔离（每次执行必做）

> **背景：** 多个 Claude Code 进程共用同一个 PinchTab daemon → 同一个 Chrome 实例。Chrome 同 profile 下 cookie/session 全局共享，一次登录所有标签页都带登录态。但不同会话的页面导航会互相覆盖当前标签页内容，必须通过标签页隔离解决。

**执行流程：**

1. 调用 `new-tab` 创建本会话专属标签页，获取 `tab_id`
2. 记录 `tab_id`，后续所有 PinchTab 操作前必须先 `switch-tab --id <tab_id>`
3. 本会话结束时不关闭标签页（保活可能还在用）

```python
# 写入 /home/openclaw/session_tab_{pid}.py
import subprocess, json

py = r'python3'
helper = r'/tmp/violation_helper.py'

result = subprocess.run([py, helper, 'new-tab'],
    capture_output=True, text=True, encoding='utf-8')
tab_info = json.loads(result.stdout)
SESSION_TAB_ID = tab_info['tab_id']
print(f"Session tab: {SESSION_TAB_ID}")
```

**后续操作前切回本会话标签页：**

```python
subprocess.run([py, helper, 'switch-tab', '--id', SESSION_TAB_ID],
    capture_output=True, text=True, encoding='utf-8')
```

> **关键约束：** 每次 `pinchtab nav`、`snap`、`click`、`eval` 等操作前，都必须确保当前活跃标签页是本会话的标签页。建议在 `nav` 前统一做 `switch-tab`。

### 🆔 Profile 隔离：多公司并发与登录复用（每次执行必做，在第一步之前）

> **背景：** 一台设备上多个 Claude 进程可能同时查不同公司。不同公司的 12123 登录态不同（不同单位账号），必须用不同的 PinchTab Profile 隔离 cookie。同一公司的多个查询进程则复用同一 Profile，共享登录态，只需各自创建新 Tab。

**Profile = 独立 Chrome user-data-dir = 独立 cookie jar。**

```
PinchTab daemon
├── Profile "default"    → cookie jar A (北京安桉)
│   ├── Tab "keepalive"  ← 保活专用
│   ├── Tab "batch"      ← 进程A 批量查询
│   └── Tab "single"     ← 进程B (cc-connect) 单台车查询，复用登录态
│
├── Profile "sc_chengdu" → cookie jar B (成都某某, sc.122.gov.cn)
│   ├── Tab "keepalive"
│   └── Tab "batch"      ← 进程C 批量查询
```

**每次执行第一步 — Profile 初始化：**

1. **查映射表**：调用 `profile-lookup --company <公司名>`
2. **命中**（`found: true`）：
   - 切换到对应 Profile 的 Instance
   - `new-tab` 创建本会话标签页
   - 导航到对应 12123 平台
   - 验证登录仍有效 → 有效则直接进入查询流程，跳过扫码登录
   - `is_logged_in=0` → 登录态已失效，走重新登录流程
   - `is_logged_in=1` 但登录过期 → 重新扫码 → `profile-register` 更新 `last_login`
   - `is_logged_in=1` 且有效 → 直接进入查询流程，跳过扫码登录
3. **未命中**（`found: false`）：
   - 走完整登录流程（第三步扫码登录）
   - 登录成功后调用 `profile-register --company <公司名> --profile-name <profile> --platform-url <url>` 写入映射

**不同公司的 Profile 创建：**

当新公司的 12123 平台 URL 与已有 Profile 不同时，需要创建新的 PinchTab Profile（手动在 `~/.pinchtab/profiles/` 下创建目录，或通过 PinchTab 管理）。如果 PinchTab 已有默认 profile 尚未绑定任何公司，则直接复用。

### 第一步：确定省份与平台入口

根据用户提供的信息确定省份（优先级：车牌号自动识别 > 城市/公司注册地名推断 > 自然语言询问）。

**省份→12123 平台入口：**

| 省份  | 12123 URL               | <br /> | 省份     | 12123 URL               |
| --- | ----------------------- | ------ | ------ | ----------------------- |
| 广东  | <https://gd.122.gov.cn> | <br /> | 四川     | <https://sc.122.gov.cn> |
| 北京  | <https://bj.122.gov.cn> | <br /> | 上海     | <https://sh.122.gov.cn> |
| 重庆  | <https://cq.122.gov.cn> | <br /> | 浙江     | <https://zj.122.gov.cn> |
| 江苏  | <https://js.122.gov.cn> | <br /> | 湖北     | <https://hb.122.gov.cn> |
| 湖南  | <https://hn.122.gov.cn> | <br /> | 山东     | <https://sd.122.gov.cn> |
| 福建  | <https://fj.122.gov.cn> | <br /> | 天津     | <https://tj.122.gov.cn> |
| 河北  | <https://he.122.gov.cn> | <br /> | 山西     | <https://sx.122.gov.cn> |
| 辽宁  | <https://ln.122.gov.cn> | <br /> | 吉林     | <https://jl.122.gov.cn> |
| 黑龙江 | <https://hl.122.gov.cn> | <br /> | 安徽     | <https://ah.122.gov.cn> |
| 江西  | <https://jx.122.gov.cn> | <br /> | 河南     | <https://ha.122.gov.cn> |
| 广西  | <https://gx.122.gov.cn> | <br /> | 海南     | <https://hi.122.gov.cn> |
| 贵州  | <https://gz.122.gov.cn> | <br /> | 云南     | <https://yn.122.gov.cn> |
| 西藏  | <https://xz.122.gov.cn> | <br /> | 陕西     | <https://sn.122.gov.cn> |
| 甘肃  | <https://gs.122.gov.cn> | <br /> | 青海     | <https://qh.122.gov.cn> |
| 宁夏  | <https://nx.122.gov.cn> | <br /> | 新疆     | <https://xj.122.gov.cn> |
| 内蒙古 | <https://nm.122.gov.cn> | <br /> | <br /> | <br />                  |

#### 车牌号自动识别省份

当用户直接输入**车牌号**（如"粤B12345"、"川A67890"）时，自动提取首字符识别省份，无需询问用户：

| 车牌前缀 | 省份  | 12123 URL     | <br /> | 车牌前缀   | 省份     | 12123 URL     |
| ---- | --- | ------------- | ------ | ------ | ------ | ------------- |
| 京    | 北京  | bj.122.gov.cn | <br /> | 津      | 天津     | tj.122.gov.cn |
| 沪    | 上海  | sh.122.gov.cn | <br /> | 渝      | 重庆     | cq.122.gov.cn |
| 冀    | 河北  | he.122.gov.cn | <br /> | 晋      | 山西     | sx.122.gov.cn |
| 辽    | 辽宁  | ln.122.gov.cn | <br /> | 吉      | 吉林     | jl.122.gov.cn |
| 黑    | 黑龙江 | hl.122.gov.cn | <br /> | 苏      | 江苏     | js.122.gov.cn |
| 浙    | 浙江  | zj.122.gov.cn | <br /> | 皖      | 安徽     | ah.122.gov.cn |
| 闽    | 福建  | fj.122.gov.cn | <br /> | 赣      | 江西     | jx.122.gov.cn |
| 鲁    | 山东  | sd.122.gov.cn | <br /> | 豫      | 河南     | ha.122.gov.cn |
| 鄂    | 湖北  | hb.122.gov.cn | <br /> | 湘      | 湖南     | hn.122.gov.cn |
| 粤    | 广东  | gd.122.gov.cn | <br /> | 桂      | 广西     | gx.122.gov.cn |
| 琼    | 海南  | hi.122.gov.cn | <br /> | 川      | 四川     | sc.122.gov.cn |
| 贵    | 贵州  | gz.122.gov.cn | <br /> | 云      | 云南     | yn.122.gov.cn |
| 藏    | 西藏  | xz.122.gov.cn | <br /> | 陕      | 陕西     | sn.122.gov.cn |
| 甘    | 甘肃  | gs.122.gov.cn | <br /> | 青      | 青海     | qh.122.gov.cn |
| 宁    | 宁夏  | nx.122.gov.cn | <br /> | 新      | 新疆     | xj.122.gov.cn |
| 蒙    | 内蒙古 | nm.122.gov.cn | <br /> | <br /> | <br /> | <br />        |

> **识别规则**：取车牌号首字符（如"粤B12345"→"粤"），查表得省份和12123域名。新能源车牌（如"粤BD12345"8位）同样取首字符识别。多台车取第一台车牌确定省份。

1. **优先级**：车牌号（自动识别） > 城市/公司注册地名推断 > 自然语言询问用户
2. 无法确定时自然语言询问用户
3. 确定省份后，导航到对应省份12123首页（如广东→`https://gd.122.gov.cn`），`pinchtab snap` 确认加载完成

### 第二步：判断登录状态与登录类型

**统一使用 `check-login-state` 子命令**（两层检测）：

| 层级 | 方法 | 用途 | 说明 |
| --- | --- | --- | --- |
| **Tier 1** | URL + DOM | 初始登录状态检查 | 通过 `window.location.href` 判断是否在登录页 (`gab.122.gov.cn/m/login`)；已登录时通过 DOM 确认业务菜单存在 |
| **Tier 2** | 关键字匹配 | 扫码轮询检测 | 检测 "退出"/"车辆管理"/"公司列表" 等业务关键词确认扫码成功；此层仅在 Tier 1 不确定时作为补充 |

```bash
# 初始检查（默认 URL+DOM，失败时自动回退关键字）
python3 /tmp/violation_helper.py check-login-state

# 仅 URL+DOM（初始检查，推荐）
python3 /tmp/violation_helper.py check-login-state --mode url

# 仅关键字匹配（扫码轮询检测用）
python3 /tmp/violation_helper.py check-login-state --mode keyword
```

**返回值**：`state: "logged_in" | "login_page" | "rate_limited" | "unknown"`，exit code 对应 0/1/2/3。

**登录状态判断流程**：
1. 调用 `check-login-state --mode url` 做初始检查
2. **state=logged_in** → 直接跳到第四步，执行查询流程
3. **state=login_page** → 继续第三步扫码登录
4. **state=rate_limited** → 立即终止所有操作并告警
5. **state=unknown** → 回退到 `--mode keyword` 关键字匹配再判断

> **核心原则**：本 skill 只针对单位用户。省份页面已登录且为单位用户时直接执行后续步骤，不要误判为个人登录后退出重登，严禁随意退出登录，尽量保持登录状态。只有实际查询时遇到非单位用户报错，才采取重新登陆策略。
>
> **关于登录检测的两层设计**：Tier 1 (URL+DOM) 用于判断"当前是否已登录"——稳定、无误判。Tier 2 (关键字匹配 "公司列表"/"退出") 用于扫码轮询中检测"用户是否已扫"——灵敏、响应快。两层各司其职，不混用。

### 第三步：扫码登录（含飞书通知）

> **🔴 铁律：先查人/群，再开登录页。** 二维码有效期约 5 分钟，如果在打开登录页后才去查人，查人过程可能消耗 1-2 分钟，导致二维码在用户扫码前就已过期。必须先完成 ID 查询，确认获取成功，再导航到登录页截图。

**3.1 确定接收人（意图识别优先，不问已指定的人）：**

- 已从上下文识别到通知对象 → 直接进入 3.2 查 ID
- 未指定 → 自然语言询问："登录二维码通过飞书通知谁？（姓名/手机号/群名，或回复'跳过'）"
- 用户提供**姓名** → 优先用 `lark-contact` skill 按姓名查找
- 用户提供**手机号** → 用飞书 API 查 open\_id
- 用户提供**群名** → 用飞书 API 搜索群 chat\_id

**3.2 查接收人飞书 ID（失败时重试，最多3次）：**

> **必须先完成此步骤，确认 ID 获取成功，再执行 3.3 打开登录页。**

**3.2.1 姓名查人（优先方式，使用 lark-contact skill）：**

用户提供姓名时，调用 `lark-contact` skill 查找飞书用户 open\_id。或通过 Python 脚本调用 helper 的 `search-user` 子命令：

```python
# 写入 /home/openclaw/search_user_{pid}.py
import subprocess, sys
py = r'python3'
helper = r'/tmp/violation_helper.py'
result = subprocess.run(
    [py, helper, 'search-user', '--query', '用户姓名'],
    capture_output=True, text=True, encoding='utf-8')
print(result.stdout)
```

**3.2.2 手机号查人：**

通过 Python 脚本调用 helper 的 `batch-get-id` 子命令。

**3.2.3 群名查群：**

通过 Python 脚本调用 helper 的 `search-chat` 子命令。

**3.3 点击登录入口：** 确认接收人 ID 获取成功后，在省份12123首页找到"单位用户登录"按钮，`pinchtab snap` 获取 ref 编号 → `pinchtab click <ref>`进入扫码登录页

**3.4 截图保存二维码：**

```bash
# 直接使用固定路径截图，文件名用 ASCII
pinchtab screenshot -o "/home/openclaw/违章查询/login_qrcode_YYYYMMDD.png"
```

用 Python 脚本确认文件存在（避免中文路径在 bash 中损坏）。

若文件不存在或大小为 0，重新截图一次；两次均失败则提示用户手动截图。

**3.5 上传二维码截图并获取 image\_key：**

通过 Python 脚本调用 helper 的 `upload-image` 子命令：

```python
# 写入 /home/openclaw/upload_qr_{pid}.py
import subprocess
py = r'python3'
helper = r'/tmp/violation_helper.py'
result = subprocess.run([py, helper, 'upload-image',
    '--dir', r'/home/openclaw/违章查询',
    '--file', 'login_qrcode_YYYYMMDD.png'],
    capture_output=True, text=True, encoding='utf-8')
print(result.stdout.strip())
```

**3.6 发送扫码登录通知（@指定人 + 记录 message\_id）**

通过 Python 脚本调用 `gen-qr-msg` → `send-msg`。

**🚨 关键：发送策略**
- **发给个人（bot→用户 P2P）**：`send-msg` 用 `--user-id <open_id>`，bot 会自动创建/复用 bot-用户 P2P 对话。**禁止使用** search-user 返回的 `p2p_chat_id`（那是用户间 P2P，bot 不在其中）
- **发给群聊**：`send-msg` 用 `--chat-id <chat_id>`，需确保 bot 已在群中；同时传 `--target-type group` 给 gen-qr-msg 以 @指定人
- **send-msg 新增校验**：响应必须含 `ok:true` 和 `message_id`，否则 exit 1，不会再静默失败

**gen-qr-msg 参数：**

- `--platform`：平台描述（如"四川12123"），用于消息展示
- `--company`：公司名称
- `--date`：日期
- `--target-type`：`personal` 或 `group`
- `--user-id` / `--user-name`：群聊 @ 指定人时使用

**发给群聊（@指定人 + 记录 message\_id）：**

```python
# 写入 /home/openclaw/send_qr_msg_{pid}.py
import subprocess, json

py = r'python3'
helper = r'/tmp/violation_helper.py'

qr_params = {
    "image_key": "<IMAGE_KEY>",
    "platform": "四川12123",
    "company": "xxx公司",
    "date": "2026-05-22",
    "target_type": "group",
    "user_id": "ou_xxx",
    "user_name": "姓名"
}
msg_json = subprocess.run(
    [py, helper, 'gen-qr-msg'],
    input=json.dumps(qr_params, ensure_ascii=False),
    capture_output=True, text=True, encoding='utf-8').stdout

with open(r'/home/openclaw/lark_login_msg_{pid}.json', 'w', encoding='utf-8') as f:
    f.write(msg_json)

result = subprocess.run(
    [py, helper, 'send-msg',
     '--msg-file', r'/home/openclaw/lark_login_msg_{pid}.json',
     '--chat-id', 'oc_xxx'],
    capture_output=True, text=True, encoding='utf-8')
print(result.stdout)
```

**提取 message\_id：**

```python
msg_id = subprocess.run(
    [py, helper, 'extract-message-id'],
    input=result.stdout, capture_output=True, text=True, encoding='utf-8').stdout.strip()
print(f"QR_MSG_ID={msg_id}")
```

**3.6.2 降级路径：分开发送图片 + 文字**

图片上传失败时，通过 Python 脚本使用 `send-image-msg` 发独立图片 + `gen-qr-fallback` 发文字。

**3.6.3 本地直接回复（必须执行，无论飞书通知是否成功）**

在当前对话中输出二维码截图快捷打开链接和登录信息：

```
🔑 查询12123车辆违章信息 - 需要您扫码登录

📋 查询信息：
  🌍 平台：[省份]12123（gab.122.gov.cn）
  🏢 公司：[公司名称]
  🕐 时间：[当前日期时间]
  📱 通知对象：[姓名/群名/手机号]

📸 二维码截图（点击直接打开）：
  file://<项目根目录>/违章查询/login_qrcode_YYYYMMDD.png

📝 登录步骤：
  ① 打开「交管12123」APP
  ② 扫描二维码完成登录（需人脸识别）
  ③ 登录成功后，在飞书中回复「已登录」

⏳ 当前状态：等待扫码登录（超时5分钟）...
```

**3.7 轮询登录回执 + QR 失效自动刷新**
> `send-msg` 内部通过 `shutil.which()` 解析 lark-cli 路径后直接调用，Linux 上无编码问题。

通过 Python 脚本调用 `poll-login`，动态轮询间隔 + 浏览器 QR 失效检测 + 最多 3 次自动刷新。

**参数：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--chat-id` | (必填) | 飞书会话 ID |
| `--target-user-id` | (必填) | 目标用户 open_id |
| `--qr-msg-id` | (必填) | QR 通知消息 ID（群聊时用于 reply_to 匹配） |
| `--qr-sent-as` | user | 谁发的 QR 消息：`bot`（bot-用户 P2P，跳过 reply_to 匹配）或 `user`（群聊，需 reply_to 匹配） |
| `--max-duration` | 300 | 总轮询时长（秒） |
| `--check-qr` | false | 启用浏览器 QR 失效检测 |
| `--check-login` | false | 启用浏览器自动检测登录。每约 30s 通过 `check-login-state --mode keyword` 检测页面是否已登录（"公司列表"/"退出"/"车辆管理"），检测到即退出 0，无需等飞书回复 |
| `--qr-refresh-count` | 0 | 当前已刷新次数 |
| `--max-qr-refreshes` | 3 | 最大刷新次数（整个 skill 统一：首次 + 2 次重发 = 3 次），超限后 QR 过期不再刷新，只等待用户回复 |

**轮询间隔策略：**

| 时间段 | 间隔 | 说明 |
|--------|------|------|
| 0-60s | 10s | 用户扫码中 |
| 60-180s | 5s | 用户可能错过通知，加频提醒 |
| 180-300s | 15s | 用户大概率已扫码，降频 |

**退出码：**

| 码 | 含义 | 处理 |
|----|------|------|
| 0 | 用户已回复「已登录」/ 浏览器自动检测到登录 | 继续查询 |
| 1 | 超时（无回复 / QR 过期但已达刷新上限） | 询问用户 |
| 2 | 用户在聊天中反馈 QR 已过期 | `pinchtab reload` 当前登录页 → 截图 → 重发通知 → 重新轮询 |
| 3 | 浏览器检测到 QR 已过期 | `pinchtab reload` 当前登录页 → 截图 → 重发通知 → 重新轮询 |

**基础调用：**

```python
# 写入 /home/openclaw/poll_login_{pid}.py
import subprocess, sys
py = r'python3'
helper = r'/tmp/violation_helper.py'
result = subprocess.run(
    [py, helper, 'poll-login',
     '--chat-id', 'oc_xxx',
     '--target-user-id', 'ou_xxx',
     '--qr-msg-id', 'om_xxx',
     '--qr-sent-as', 'bot',  # bot 发送→跳过 reply_to；user 发送（群聊）→需 reply_to 匹配
     '--max-duration', '60',  # 首次轮询可用较短时长
     '--check-qr',
     '--check-login',
     '--qr-refresh-count', '<N>',
     '--max-qr-refreshes', '3'],
    encoding='utf-8')
sys.exit(result.returncode)
```

**带 QR 自动刷新的循环调用（exit 2/3 时刷新 QR 重试）：**

```python
# 写入 /home/openclaw/poll_with_refresh_{pid}.py
import subprocess, sys, time

py = r'python3'
helper = r'/tmp/violation_helper.py'

MAX_REFRESHES = 3

for refresh_count in range(MAX_REFRESHES + 1):
    result = subprocess.run(
        [py, helper, 'poll-login',
         '--chat-id', 'oc_xxx',
         '--target-user-id', 'ou_xxx',
         '--qr-msg-id', 'om_xxx',
         '--qr-sent-as', 'bot',
         '--max-duration', '300',
         '--check-qr',
         '--check-login',
         '--qr-refresh-count', str(refresh_count),
         '--max-qr-refreshes', str(MAX_REFRESHES)],
        encoding='utf-8')

    if result.returncode == 0:
        print('LOGIN_SUCCESS')
        sys.exit(0)
    elif result.returncode == 1:
        print('TIMEOUT_OR_MAX_REFRESHES')
        sys.exit(1)
    elif result.returncode in (2, 3):
        if refresh_count >= MAX_REFRESHES:
            print('MAX_REFRESHES_REACHED - waiting for user reply only')
            sys.exit(1)
        # Refresh QR: reload current login page → screenshot → upload → re-send
        # 直接 pinchtab reload 当前登录页即可生成新二维码，无需退回首页再进入
        print(f'QR expired, refreshing ({refresh_count + 1}/{MAX_REFRESHES})...')
        # 1. pinchtab reload （刷新当前登录页，自动生成新二维码）
        # 2. 截图保存（同 3.4）
        # 3. 上传获取 image_key（同 3.5）
        # 4. 发送新通知（同 3.6），更新 qr_msg_id
        # 5. 用新的 qr_msg_id 继续 poll-login
```

**3.8 降级：** 用户主动跳过飞书通知或 lark-cli 不可用时，只执行 3.6.3 本地回复展示截图，自然语言等待用户对话回复。

### 第四步：登录后选择公司并导航至车辆列表页

1. 先选择公司：登录成功后，`pinchtab snap` + `pinchtab text` 获取页面上的公司列表。省份信息用于辅助匹配公司（如"成都"匹配名称中含"成都"的公司）。单台车查询时：若仅一家公司匹配省份则直接进入该公司；若多家公司匹配省份，向用户询问确认是哪家公司。全量查询无法唯一确定时同样追问。
2. 然后按以下路径进入车辆列表页：主页 → 租赁车辆管理，同时需要确保“服务类型”选择的是 业务办理

### 第五步：判断查询模式

- **指定车辆查询**：用户提供车架号或具体车牌号 → 走第六步-A
- **全量查询**：用户要求查公司名下所有车辆 → 走第六步-B

### 第六步-A：指定车辆查询

1. **弹窗防御**：`dismiss-popup` 关闭”本人已知晓”等系统弹窗
2. 搜索区填写：号牌种类（选择”小型新能源汽车”）、车牌号 → 点击 `搜索`
3. `pinchtab snap` 确认搜索结果 → 调用 `open-vehicle --index 1` 双击进入详情页（含三重试+URL验证+风控检测）
4. 调用 `collect-violations --plate <车牌> --query-date <日期>` 提取违章详情：
   - 自动对比 SQLite 存量数据，跳过已有未变更记录
   - 只对未处理/未缴费记录点击”查看详情”
   - 支持详情页智能翻页（违章数 > 10 时自动翻页采集）
   - 内置 XHR 风控监控，检测到”查询过于频繁”立即熔断
5. 提取完成后 `go-back` 返回列表

### 第六步-B：全量查询（逐页处理模式）

> **核心改变**：一页页处理，不先扫描全部页面。每页只对有违章的车进入详情采集，无违章跳过。完成一页后再翻下一页。
>
> **🔴 铁律：批量查询必须通过 `violation_helper.py` 已有子命令组合实现。** 整个逐页循环只调用以下 helper 子命令，不新写独立查询脚本：
> - `load-detail-progress` / `save-detail-progress` — 断点续跑（进度文件按 `details_progress_<公司>_<日期>.json` 隔离）
> - `get-page-vehicles` — 获取当前页车辆列表+页码
> - `open-vehicle --index N` — 进入第 N 台车详情
> - `collect-violations --plate <车牌> --query-date <日期>` — 逐条采集违章详情
> - `db-insert-violation` — 每条违章结果立即落库（逐条 upsert，不攒批）
> - `go-back` — 返回车辆列表
> - `click-page --target next|N` — 翻页
> - `find-plate-page --plate <车牌>` — 断点续跑时向前搜索
> - `detect-rate-limit` — 风控检测
> - `dismiss-popup` — 弹窗防御
>
> **🔴 铁律：违章详情逐个查询 + 逐条落库。** `collect-violations --auto-insert` 在每提取完一条违章详情（关闭弹窗后）立即调用内部 `_upsert_violation()` 写入 SQLite。即使中途崩溃，已入库的违章数据不会丢失。禁止攒到全部查完再批量落库、禁止用中间 JSON/JS 文件暂存。
>
> **断点续跑机制（二级：车辆级 + 违章级）：**
>
> 每次查询前先调用 `load-detail-progress` 获取断点位置：
> - `resume_page`: 上次完成的车辆列表页码（0 or fresh = 从头开始）
> - `resume_vehicle_index`: 该页上完成的最后一台车序号（0 = 该页从头开始）
> - `resume_detail_page`: 该车的违章详情页码（-1 = 未开始/已完成，>=0 = 从该页续跑）
> - `resume_violation_index`: 该详情页的违章序号（-1 = 该页从头开始）
> - `resume_violation_time`: 最后一条已处理违章的时间戳（用于跨页交叉校验）
> - `processed_plates`: 已处理车牌列表（用于增量去重）
>
> **逐页处理循环（极简包装，只调 helper 子命令，写入 `/home/openclaw/batch_query_{pid}.py` 后执行）：**

```python
"""
批量查询包装脚本 — 仅调用 violation_helper.py 已有子命令。
不包含任何查询逻辑，所有逻辑在 helper 内部。
"""
import subprocess, json, time, random, sys

py = r'python3'
helper = r'/tmp/violation_helper.py'
date = time.strftime('%Y-%m-%d')
company = '<公司名称>'  # set by caller based on selected company

def h(cmd_args):
    """Call helper subcommand, return stdout stripped."""
    result = subprocess.run([py, helper] + cmd_args,
        capture_output=True, text=True, encoding='utf-8')
    return result.stdout.strip()

# 1. Load resume point (isolated by company + date)
prog = json.loads(h(['load-detail-progress', '--company', company, '--query-date', date]))
resume_page = prog['resume_page']
resume_idx = prog['resume_vehicle_index']
resume_plate = prog.get('resume_plate', '')
processed = set(prog.get('processed_plates', []))
print(f"Resume: page={resume_page}, idx={resume_idx}, processed={len(processed)}")

# 2. Navigate to resume page if needed
if resume_page > 1:
    for attempt in range(3):
        resp = h(['click-page', '--target', str(resume_page)])
        if 'navigated' in resp.lower() or 'clicked' in resp.lower():
            break
        if 'stuck' in resp.lower():
            h(['click-page', '--target', '1'])
            resume_page = 1; resume_idx = 0; break
        time.sleep(random.uniform(1, 2))
    time.sleep(random.uniform(2, 5))

    if resume_page > 1 and resume_plate:
        find = json.loads(h(['find-plate-page', '--plate', resume_plate, '--max-forward', '3']))
        if find.get('found') and find['method'] == 'forward':
            resume_page = find['page']; resume_idx = 0
        elif not find.get('found'):
            h(['click-page', '--target', '1'])
            resume_page = 1; resume_idx = 0

# 3. Page-by-page loop
vehicle_offset = resume_idx
while True:
    # Dismiss popup + get current page
    h(['dismiss-popup'])
    page_data = json.loads(h(['get-page-vehicles']))
    vehicles = page_data.get('vehicles', [])
    current_page = page_data.get('page', 1)
    total_pages = page_data.get('total_pages', 1)
    print(f"\n=== Page {current_page}/{total_pages}: {len(vehicles)} vehicles ===")

    for i, v in enumerate(vehicles):
        if current_page == resume_page and i < vehicle_offset:
            continue

        plate = v['plate']
        unprocessed = v.get('unprocessed', 0)

        # Insert vehicle record immediately (even if no violations)
        h(['db-insert-vehicle',
            '--company-id', '1',
            '--plate-number', plate,
            '--plate-type', v.get('type', ''),
            '--plate-type-label', v.get('type_label', v.get('type', '')),
            '--status-code', str(v.get('status_code', '')),
            '--status-label', v.get('status', ''),
            '--inspection-date', v.get('inspection_date', ''),
            '--unprocessed-count', str(unprocessed),
            '--query-date', date])

        if unprocessed == 0:
            print(f"  [{i+1}] {plate}: skip (no violations)")
            continue
        if plate in processed:
            print(f"  [{i+1}] {plate}: already processed, skip")
            continue

        print(f"  [{i+1}] {plate}: {unprocessed} violations, entering detail...")

        # Open vehicle → collect violations → insert to DB → go back
        h(['open-vehicle', '--index', str(i + 1)])
        time.sleep(random.uniform(1, 2))

        # Collect violation details with auto-insert (each detail immediately written to DB)
        violations_out = h(['collect-violations', '--plate', plate, '--query-date', date, '--auto-insert'])
        try:
            violations = json.loads(violations_out)
            violation_count = len([x for x in violations if not x.get('skipped') and not x.get('from_db')])
            print(f"    -> {violation_count} violations saved to DB")
        except json.JSONDecodeError:
            print(f"    -> parse error: {violations_out[:100]}")

        h(['go-back'])
        time.sleep(random.uniform(2, 5))

        h(['save-detail-progress', '--page', str(current_page),
            '--vehicle-index', str(i + 1), '--plate', plate,
            '--company', company, '--query-date', date])
        processed.add(plate)

    if current_page >= total_pages:
        print(f"\nAll {total_pages} pages done.")
        break

    print(f"\nPage {current_page} done, next...")
    h(['click-page', '--target', 'next'])
    time.sleep(random.uniform(2, 5))
    vehicle_offset = 0

print(f"\nDone. Vehicles with violations processed: {len(processed)}")
```

**关键规则：**
- **有违章必须查**：`unprocessed > 0` 的车辆必须 `open-vehicle` → `collect-violations` → `go-back`，不能跳过
- **只查未处理记录**：`collect-violations` 只对状态为"未处理"的违章逐条点击"查看详情"。状态为"已处理"、"已缴费"、"无需缴费"的记录直接从列表提取基本信息，不点击详情（已处理记录无需获取罚款记分调整）
- **🔴 逐条落库**：`collect-violations` 返回结果后，必须逐条立即调 `db-insert-violation` 写入 SQLite。禁止攒批、禁止中间 JSON 暂存
- **🔴 车辆即时入库**：`get-page-vehicles` 获取每台车信息后，立即调 `db-insert-vehicle` 写入 vehicles 表（含未处理违章数的车也要写）。禁止等到 Step 7 再批量补写
- **随机延迟**：每条违章记录查询之间 `time.sleep(random.uniform(2, 5))`，每次点击操作之间 `time.sleep(random.uniform(1, 2))`
- **`collect-violations` 已验证**：逐条点击"查看详情"提取真实罚款金额和记分，关闭弹窗后继续下一条，不 reload 页面
- **每台车保存进度**：`save-detail-progress` 记录页码+序号+车牌+详情页+违章序号，支持车辆级和违章级两级断点续跑
- **翻页智能跳转**：`click-page --target N` 内置智能翻页算法（目标页>当前显示最大页→先跳最大页→再找）。详情页翻页使用相同算法
- **详情返回原路**：`go-back` 优先使用 `history.back()` 保留列表页位置，避免跳回第1页
- **进度文件结构**：`details_progress_<公司>_<日期>.json` 记录 `last_page`、`last_vehicle_index`、`last_plate`、`processed_plates`。公司+日期隔离保证多进程互不干扰且支持跨 Claude 重启续跑
- **安全重置**：`reset-detail-progress` 只清空采集进度，不碰全量车辆列表文件
- **禁止新写脚本**：以上包装脚本是唯一允许的批量查询脚本，其唯一职责是循环调用 helper 已注册子命令。禁止在循环体内手写任何查询逻辑（如 JS 注入、DOM 操作、手动解析页面等）

### 第七步：结果汇总 — 三重输出

#### 输出一：SQLite 数据库

> 公司记录在本步骤写入，车辆记录已在第六步-B逐台即时入库。违章记录在查询过程中已逐条落库（第六步-B），此处仅做验证。

通过 `init-db` 初始化数据库，`db-insert-company` / `db-insert-vehicle` 写入公司和车辆记录。违章记录已通过 `db-insert-violation` 在采集过程中逐条写入，此处可用 SQL 查询验证数据完整性。

表结构：
- **companies**: id, name, query_date
- **vehicles**: id, company_id, plate_number, plate_type, plate_type_label, status_code, status_label, inspection_date, unprocessed_count, query_date
- **violations**: id, vehicle_id, plate_number, violation_time, violation_location, violation_behavior, violation_code, fine_amount, points, handling_status, payment_status, authority, 等

#### 输出二：本地 Markdown 报告

保存到 `违章查询/reports/违章查询报告_[公司名称]_YYYY-MM-DD.md`（由 Write tool 写入）

```markdown
# 车辆违章查询报告

**查询公司：** [公司] | **查询日期：** YYYY-MM-DD | **平台：** [省份]12123
**查询车辆：** N 台 | **违章总数：** M 条

---

## 查询结果汇总

| 序号 | 车牌号 | 号牌种类 | 违章时间 | 违章地点 | 违章行为 | 违章代码 | 罚款(元) | 记分 | 处理状态 | 缴款状态 |
|------|--------|---------|----------|----------|----------|----------|----------|------|----------|----------|

> 处理状态 cod：-1已删除 / 0未处理 / 1已处理 / 2已转出 / 9无需处理
> 缴款状态 cod：0未缴款 / 1已缴款 / 9无需缴款
```

#### 输出三：查询完成飞书通知

通过 Python 脚本调用 `gen-result-msg` → `send-msg`，向同一接收对象发送。

***

## 防退出保活

### 架构：四层保活 + systemd 守护

保活由 **systemd user service** 管理，配合 **四层保活架构** 实现崩溃自动恢复、免扫码持久化。完全独立于 Claude 会话，机器重启也能自动拉起。

```
四层保活架构（从快到慢、从轻到重）:

┌─────────────────────────────────────────────────────┐
│ L0  心跳层 (60-120s)                                 │
│     随机 scroll + DOM ping + 偶发 popup dismiss      │
│     作用: 保持页面活跃，避免服务端 idle 超时             │
│     检测: 连续 5 次心跳失败 → 提前触发 L1 reload       │
├─────────────────────────────────────────────────────┤
│ L1  周期 Reload 层 (18min)                           │
│     pinchtab reload → dismiss popup → 页面状态检查     │
│     作用: 全量刷新保持 session 活跃                     │
│     检测: 登录态/风控关键词/登录页回退                    │
├─────────────────────────────────────────────────────┤
│ L2  Cookie 持久化层                                  │
│     cookie_persist.py: 修改 Chrome SQLite Cookies DB │
│     将 12123 域名的 session cookie → persistent       │
│     设置 is_persistent=1, has_expires=1, 30天过期     │
│     每次 keepalive cycle + 每次 pinchtab 重启时执行    │
├─────────────────────────────────────────────────────┤
│ L3  systemd 自动重启层                                │
│     pinchtab.service: Restart=always, RestartSec=5   │
│     keepalive-12123.service: Restart=always,          │
│       RestartSec=10                                   │
│     ExecStartPre: cookie_persist.py (Chrome 启动前)   │
│     ExecStopPost: cookie_persist.py (Chrome 停止后)   │
│     作用: 崩溃自动拉起 + 重启后免扫码                    │
└─────────────────────────────────────────────────────┘
```

**每个公司一个独立 systemd 服务**（`keepalive-<公司简称>.service`），不同公司的保活完全隔离。

### systemd 服务架构

```
systemd user services
├── pinchtab.service                    ← Chrome 浏览器 + PinchTab daemon
│   ├── Restart=always, RestartSec=5
│   └── drop-in: cookie-persist.conf
│       ├── ExecStartPre=cookie_persist.py   ← Chrome 启动前修 DB
│       └── ExecStopPost=cookie_persist.py   ← Chrome 停止后修 DB（应对 clean shutdown 回写）
│
└── keepalive-12123.service             ← 保活守护进程（以厦门地上铁为例）
    ├── After=pinchtab.service
    ├── Restart=always, RestartSec=10
    ├── ExecStartPre=cookie_persist.py       ← 保活启动前确认 cookie 持久化
    └── ExecStart=keepalive_daemon.py --auto-recover
```

### Cookie 持久化机制 (`cookie_persist.py`)

**原理：** Chrome 将 cookie 存储在 `~/.pinchtab/profiles/default/Default/Cookies` SQLite 数据库中。12123 平台的 JSESSIONID 等关键 cookie 被标记为 `is_persistent=0`（session-only），Chrome 重启时丢弃。通过直接修改 SQLite 元数据即可让它们跨重启存活。

**关键字段：**
- `is_persistent`: 0 → 1（标记为持久化 cookie）
- `has_expires`: 0 → 1（启用过期时间）
- `expires_utc`: 0 → 当前时间 + 30 天（微秒时间戳）
- `encrypted_value`: **不动**（v10 加密，Linux `--password-store=basic` 下为固定密钥，无需解密）

**作用范围：** 只匹配 12123 平台域名（`%122.gov.cn%`, `%12123%`, `%gab.122%`），不影响其他 cookie。

**执行时机：**
1. **pinchtab 重启时**：ExecStartPre（Chrome 启动前读）+ ExecStopPost（Chrome 停止后写），应对 Chrome clean shutdown 可能回写 `is_persistent=0`
2. **keepalive 每个周期结束时**：reload 后可能有新 session cookie 产生，立即持久化
3. **keepalive 启动时**：ExecStartPre 确认所有 cookie 已持久化

```bash
# 查看当前状态
python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py \
  --profile /home/openclaw/.pinchtab/profiles/default --verify

# 执行持久化
python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py \
  --profile /home/openclaw/.pinchtab/profiles/default

# 预览（不实际修改）
python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py \
  --profile /home/openclaw/.pinchtab/profiles/default --dry-run
```

### 心跳层 (`keepalive_daemon.py` 内置)

在主循环的 sleep 间隙插入心跳，保持页面活跃：

```python
# 配置
HEARTBEAT_MIN_SEC = 60   # 最小间隔
HEARTBEAT_MAX_SEC = 120  # 最大间隔（随机化）
MAX_CONSECUTIVE_HEARTBEAT_FAILS = 5  # 连续失败阈值

# 每次心跳执行:
# 1. 随机 scroll (100-1200px) — 模拟用户查看页面
# 2. DOM ping (提取 navbar-brand/h1/title 文本) — 验证页面存活
# 3. 1/5 概率 dismiss popup — 清理意外弹窗
```

**心跳在 18 分钟 sleep 期间以 60-120s 随机间隔执行**，每次心跳耗时 ~0.1-3 秒。连续 5 次失败则提前触发 L1 reload（不等 18 分钟）。

### 部署保活（首次登录成功后执行）

为每个公司创建 systemd 服务。以「厦门市地上铁新创绿能汽车服务有限公司」为例：

**1. 创建 pinchtab drop-in（cookie 持久化钩子）：**

```ini
# ~/.config/systemd/user/pinchtab.service.d/cookie-persist.conf
[Service]
ExecStartPre=/opt/aiext/bin/python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py --profile /home/openclaw/.pinchtab/profiles/default
ExecStopPost=/opt/aiext/bin/python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py --profile /home/openclaw/.pinchtab/profiles/default
```

**2. 创建 keepalive 服务：**

```ini
# ~/.config/systemd/user/keepalive-12123.service
[Unit]
Description=12123 Keepalive Daemon (厦门地上铁)
After=pinchtab.service
Wants=pinchtab.service

[Service]
Type=simple
Environment="PATH=/home/openclaw/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
ExecStartPre=/opt/aiext/bin/python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py --profile /home/openclaw/.pinchtab/profiles/default
ExecStart=/opt/aiext/bin/python3 /home/openclaw/.claude/skills/DST违章查询/keepalive_daemon.py \
    --company "<公司名>" \
    --project-root <项目根目录> \
    --auto-recover
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

**3. 启用并启动：**

```bash
systemctl --user daemon-reload
systemctl --user enable keepalive-12123.service
systemctl --user start keepalive-12123.service

# 确保用户服务在会话退出后继续运行（只需执行一次）
loginctl enable-linger
```

### 查看保活状态

```bash
# 查看 keepalive 服务状态
systemctl --user status keepalive-12123.service --no-pager

# 查看 pinchtab 服务状态
systemctl --user status pinchtab.service --no-pager

# 实时日志
tail -f 违章查询/data/keepalive_<公司名>.log

# Cookie 持久化状态
python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py \
  --profile /home/openclaw/.pinchtab/profiles/default --verify
```

### 停止保活

```bash
systemctl --user stop keepalive-12123.service
# 如需彻底禁用:
systemctl --user disable keepalive-12123.service
```

如需停止整个保活栈（含 pinchtab）：

```bash
systemctl --user stop keepalive-12123.service pinchtab.service
```

### 守护进程行为

`keepalive_daemon.py` 每 18 分钟循环执行：

0. 读取 SQLite `profiles.is_logged_in`，若为 0 则退出
1. `pinchtab tab <id>` 切换到本公司的 keepalive 标签页
2. `pinchtab reload` 刷新页面（连续 3 次失败 → `profile-logout` → 退出）
3. JS dispatchEvent 关闭弹窗（"本人已知晓"等），4 层策略
4. 页面状态检查：
   - 检测到"退出"按钮 → 登录态正常
   - 检测到"频繁"/"异常操作"/"黑名单"等 → 风控熔断 → `profile-logout` → 退出
   - 检测到登录页（"单位用户登录"等）且无已登录标识 → 若启用 `--auto-recover` 则触发 QR 恢复
5. 调用 `cookie_persist.py` 持久化本周期产生的新 session cookie
6. 日志写入 `违章查询/data/keepalive_<公司>.log`（含时间戳）

**18 分钟 sleep 期间：** 心跳以 60-120s 随机间隔执行 random scroll + DOM ping，保持页面活跃。

**标签页持久化：** Tab ID 保存在 `违章查询/data/keepalive_tab_<公司>.txt`。守护进程重启时复用已有 Tab（不重复创建），Tab 失效时自动创建新 Tab 并导航到 `platform_url`。

**自动恢复策略（`--auto-recover`）：**
- 每次保活会话最多触发 **1 次** 自动恢复
- 那次恢复内最多发送 **3 次** QR 码（应对二维码过期自动刷新）
- 3 次 QR 均超时或恢复失败 → 静默退出，等待下次查询任务自然触发重新登录

**崩溃恢复完整链路（已验证）：**

```
SIGKILL pinchtab
  → systemd RestartSec=5s
  → ExecStartPre: cookie_persist.py 修复 DB
  → Chrome 启动，加载持久化 cookie
  → keepalive 检测 pinchtab 恢复
  → 复用 keepalive tab → reload → Page state OK
  → 免扫码，恢复正常保活循环
```

**Linger 保障：** `loginctl enable-linger` 确保 systemd user 服务在 SSH/会话退出后不被 kill。服务已 enabled 则在机器重启后自动拉起。

***

## 异常处理

| 异常                 | 处理                                                                            |
| ------------------ | ----------------------------------------------------------------------------- |
| 页面加载超时             | `pinchtab wait --text "<关键文本>" --timeout 60000`，超时重试                          |
| 风控/限流检测            | `detect-rate-limit` 检测到关键词或连续3台 open_vehicle 失败 → 立即终止所有进程，发送飞书告警，保留进度 |
| 二维码过期              | `poll-login --check-qr` 自动检测 → `pinchtab reload` 当前登录页直接刷新（不退回首页重进）→ 重新截图 → 重新发送飞书通知（最多 3 次）               |
| 截图保存失败             | 重试一次；两次均失败提示用户手动截图                                                            |
| 图片上传/发送失败          | `send-msg` 通过 `shutil.which()` 解析 lark-cli 直接调用；仍失败时降级独立图片+文字                      |
| 飞书消息发送失败           | 报告原因，回退本地展示，不阻塞主流程                                                            |
| 姓名查不到飞书用户          | 间隔2秒最多重试3次；3次均失败降级为请求用户提供手机号                                                  |
| 用户ID/群名查不到         | 间隔2秒最多重试3次；3次均失败提示用户换用手机号或直接提供 open\_id/chat\_id                              |
| poll-login 轮询超时     | 退出码 1：询问用户是否继续等待。退出码 3：自动刷新 QR 重新轮询，最多 3 次                                  |
| 群聊中无人回复            | 超时后提醒用户，确认是否要重新发送或直接跳过                                                        |
| 群聊中非目标用户回复         | `reply_to` + `sender.id` 双重过滤，自动忽略非目标用户的回复                                    |
| bot 不在群中           | 先用 `lark-cli api POST /open-apis/im/v1/chats/{chat_id}/members` 把 bot 拉进群再发消息 |
| 查询无结果              | 记录"无违章"，继续后续                                                                  |
| 批量某台失败             | 记录原因，跳过继续                                                                     |
| 会话过期               | 重新登录（含飞书通知）                                                                   |
| PinchTab daemon 断开 | `pinchtab health` 检查状态 → `pinchtab daemon restart` 重启                         |
| lark-cli 权限错误      | 按 lark-shared skill 引导授权                                                      |
| poll-login 消息发送损坏  | helper 内部 `_run()` 强制 UTF-8 编码（PYTHONUTF8=1 + PYTHONIOENCODING=utf-8），Linux 上无编码问题 |

***

## 使用示例

- **车牌自动识别：** `查粤B12345违章` → 粤→广东→导航 gab.122.gov.cn → 登录 → 输入车牌 → 生成报告
- **批量：** `查成都公司违章` → 成都→四川→导航 gab.122.gov.cn → 登录 → 选择公司 → 车辆列表 → 逐台查 → 三重输出
- **群通知（@指定人）：** `查成都公司违章，发到违章通知群，@任晏平` → 四川→截图→搜群→发群+@→轮询监听→查询→通知结果
- **保活：** 登录成功后通过 systemd user service 启动 `keepalive_daemon.py`，四层保活（心跳 + 周期 reload + Cookie 持久化 + systemd 自动重启），完全独立于 Claude 会话持续保活

***

## 注意事项

1. 仅查授权车辆，车架号默认只显示后6位
2. 批量查询间隔 2-5 秒随机，点击操作间隔 1-2 秒随机（模拟人工操作，避免触发反爬）
3. 飞书不可用自动降级本地展示，不阻塞
4. 二维码约5分钟有效，尽快发送
5. **lark-cli 身份：** 发消息 `--as bot` | 查用户ID（手机号）`--as bot` | 查用户ID（姓名）`--as user`（通过 lark-contact）| 上传图片 `--as bot`
6. **姓名查人优先：** 用户提供姓名时，优先通过 `lark-contact` skill 查找，失败时降级为请求手机号
7. **Linux UTF-8 兼容：** Linux 终端原生支持 UTF-8，可直接在 Bash 命令中使用中文路径和中文参数。Python 已自动配置 UTF-8 编码（PYTHONUTF8=1）。关键输出仍可通过 `-o FILE` 写入 UTF-8 文件确保跨进程正确读取。临时脚本写入 `/home/openclaw/` 目录。
8. **本地回复（3.6.3）：** 发送飞书通知后，必须同时在当前对话中输出截图 `file:///` 链接和登录信息
9. **登录入口统一：** 所有省份扫码登录使用 `https://gab.122.gov.cn/m/login?t=2`。省份 URL 用于登录后导航（车辆列表等）和报告展示
10. **车牌自动识别：** 用户输入车牌号时，提取首字符匹配省份，用于确定省份上下文
11. **文件存储：** `违章查询/screenshots/`（截图）、`违章查询/reports/`（报告）、`违章查询/data/`（SQLite 数据库）
12. **PinchTab 调试：** 若 PinchTab 命令失败，先检查 `pinchtab health`，必要时 `pinchtab daemon restart`
13. **元素定位：** 通过 `pinchtab snap` 获取 ref 编号（如 `e5`、`e12`）。**每次页面导航后 ref 编号会重新分配**，必须重新 snap 获取新 ref 后再操作。翻页相关操作优先使用 `click-page` helper（内部用 JavaScript，不依赖 ref）
14. **禁止推算：** 有违章车辆必须 `collect-violations` 逐条获取真实罚款和记分
15. **跳过无违章车辆：** `unprocessed == 0` 且 `status == "正常"` 的车辆无需进入详情页
16. **lark-cli 直调：** helper `_run()` 直接使用 PATH 上的 `lark-cli` 二进制（或通过 `shutil.which()` 解析的完整路径），Linux 上不经过任何中间包裹器。`_node_path()` 优先使用 `shutil.which('node')` 搜索 PATH。
17. **Python stdout 编码（Issue #2 三重保障修复）：** helper 启动时：(1) 强制覆盖 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`（非 setdefault）；(2) `TextIOWrapper` 先包裹原始 buffer（绕过控制台编码）；(3) `reconfigure(encoding='utf-8')` 兜底。子进程 `_run()` env 也强制覆盖。同时包裹 `sys.stdin` 确保 piped 输入为 UTF-8
18. **路径输出乱码：** 通过 `-o FILE` 将含中文路径写入 UTF-8 文件，外部脚本读取文件获取正确路径
19. **poll-login 退出码：** 0=已登录（飞书回复或浏览器自动检测） / 1=超时或达刷新上限 / 2=用户反馈 QR 过期 / 3=浏览器检测 QR 过期
20. **QR 刷新上限：** 最多自动刷新 3 次，达到上限后仅等待用户主动回复，不再刷新
21. **send-msg 响应校验：** send-msg 发送后校验响应 JSON 必须含 `ok:true` 和 `message_id`，否则非零退出。不再静默失败。
22. **poll-login --qr-sent-as：** bot 发送时传 `--qr-sent-as bot`，跳过 reply_to 匹配（bot-用户 P2P 对话中用户消息无需回复特定消息）；群聊中用户发送时保留默认 `user` 模式（需 reply_to 匹配）。
23. **bot 发个人消息用 --user-id：** bot 发给个人时使用 `send-msg --user-id <open_id>`（创建 bot-用户 P2P），**禁止使用** search-user 返回的 p2p_chat_id。
24. **逐页处理模式：** 全量查询不再先扫描所有页面，而是一页一页处理。每页提取车辆→有违章则进入详情采集→采集完翻下一页。每台车完成后保存进度（页码+序号+车牌），下次从断点继续。
25. **智能翻页（Issue #5 ref 漂移修复）：** `click-page` 完全使用 JavaScript 点击（text/CSS/charCode 三策略），不依赖 accessibility tree ref 编号。翻页优先用页码号（`--target N`），避免"下一页"按钮。翻页后每次 `get-page-vehicles` 重新提取当前页车辆+页码。详情页翻页复用相同算法（`_click_detail_page`）。
26. **详情返回保留位置：** `go-back` 优先 `history.back()` 保留列表页和翻页状态；`collect-violations` 关闭弹窗而非 reload 页面，避免丢失列表位置。
27. **安全重置进度：** 使用 `reset-detail-progress` 只清空采集进度（plates+resume point），不碰全量车辆列表文件 `all_vehicles_progress.json`。禁止直接编辑/清空进度文件。
28. **终端编码：** 终端显示中文乱码是已知问题，文件内数据正常。关键输出走 `-o FILE` 写入 UTF-8 文件获取正确内容。
29. **单位用户优先：** 判断已登录状态后，调用 `get-login-type` 检测。单位用户直接继续；只有实际查询遇到非单位报错时才重新登录。不要因为不确定就退出重登。
30. **弹窗遮挡（Issue #4 修复）：** `_close_popup()` 四层策略：(1) JavaScript `dispatchEvent` 点击关闭/×/取消按钮（绕过 pinchtab occlusion 检查）；(2) pinchtab click 关闭按钮（occlusion 失败时自动降级为 JS dispatchEvent）；(3) Escape 键事件（KeyEvent + activeElement）；(4) 直接 DOM 隐藏 modal/overlay/fixed 元素（最后的保险）。确保查看违章详情后能可靠关闭弹窗返回列表页。
31. **禁止随意退出登录：** 检测到已登录（有"退出"按钮、公司列表菜单）时严禁退出。只需验证省份和公司匹配当前任务即可继续。只有实际查询时遇到非单位用户报错，才重新登录。
32. **风控熔断机制：** `detect-rate-limit` 检测页面关键词（频繁/异常操作/黑名单/第三方软件等）。`open-vehicle` 连续 3 次失败也触发熔断。触发后立即终止所有查询进程，保留进度，发送飞书告警。
33. **保活生命周期：** 登录成功后创建 systemd user service（`keepalive-<公司>.service`），通过 `systemctl --user start/enable` 启动。服务通过 `Restart=always` 保活，完全独立于 Claude 会话（会话终止后继续运行，机器重启后自动拉起）。守护进程每 18 分钟 reload + dismiss popup，心跳 60-120s 随机 scroll + ping 保持页面活跃。`is_logged_in=0` 或检测到异常时自动退出。通过 `systemctl --user status/stop` 查看状态和停止。
34. **图片上传方式：** `lark-cli im images create --file` 无法读取含中文路径文件。改用 stdin 管道：`cat /path/to/img.png | lark-cli im images create --as bot --file "image=-" --data '{"image_type":"message"}'`
35. **总页数动态获取：** 不强制一开始获取全量页数。每页查询时通过可见分页链接获取当前 total_pages，动态更新。翻页后验证 URL 是否变化（防止假翻页）。连续 2 页检测到 0 辆车时认为到达末尾。
36. **SQLite 增量对比：** `collect-violations` 查询前先读取 SQLite 中该车牌已有记录。已存在且状态未变的跳过；状态变更的（未处理→已处理）重新查询详情并更新。
37. **详情页分页采集：** 违章数 > 10 的车辆，`collect-violations` 自动翻页采集所有详情页的未处理记录。支持 `--resume-from N` 断点续跑。
38. **先查人再开登录页（🔴 铁律 #9）：** 用户指定了通知对象时，必须先用 `search-user`/`search-chat`/`batch-get-id` 完成飞书 ID 查询，确认获取成功后，再导航到 12123 登录页截图二维码。二维码有效期约 5 分钟，先查人避免浪费。
39. **批量查询走 helper（🔴 铁律 #10）：** 全量/多台车查询的循环逻辑必须通过 `violation_helper.py` 已有子命令组合实现（`get-page-vehicles` → `open-vehicle` → `collect-violations` → `db-insert-violation` → `go-back` → `save-detail-progress` → `click-page`）。禁止为批量查询新写独立 Python 脚本。唯一允许的包装脚本只做循环调用，不包含任何查询逻辑。
40. **违章详情逐个查询（🔴 铁律 #11）：** 有未处理违章的车辆必须逐条点击"查看详情"获取真实罚款和记分。禁止只读列表数据不查详情、禁止仅凭列表摘要生成报告。此为最高优先级铁律。
41. **逐条落库（🔴 铁律 #12）：** `collect-violations --auto-insert` 在每条违章详情提取后立即写入 SQLite（helper 内置 upsert）。禁止攒到全部查完再批量落库、禁止用中间 JSON/JS 文件暂存。即使中途崩溃，已入库的违章数据不会丢失。
42. **标签页会话隔离：** 多个 Claude 进程共用同一 PinchTab daemon（同一 Chrome 实例）。每次执行必须通过 `new-tab` 创建专属标签页，后续所有操作前先 `switch-tab --id <tab_id>` 切回本会话标签页。Cookie/session 在所有标签页间共享，一次登录即可，但页面导航互不干扰。
43. **多进程文件隔离：** 临时 Python 脚本路径含 `{pid}` 后缀（`batch_query_{pid}.py` 等），不同进程互不覆盖。进度文件按公司+日期隔离（`details_progress_<公司>_<日期>.json`），支持多进程并行查不同公司，且 Claude 上下文满重启后同一天自动续跑。SQLite 开启 WAL 模式支持多进程并发读写。
44. **Profile 隔离与登录复用：** 每个公司绑定一个 PinchTab Profile（`profiles` 表）。同一公司的多个查询进程复用同一 Profile（共享 cookie/登录态），通过 `new-tab` 隔离页面导航。不同公司使用不同 Profile。每次执行前先 `profile-lookup` 查映射表，命中则跳过登录直接查询。首次登录成功后 `profile-register` 写入映射表。
45. **保活守护进程：** `keepalive_daemon.py` 通过 systemd user service 管理（`keepalive-<公司>.service`），配合 pinchtab drop-in `cookie-persist.conf` 实现四层保活架构（L0 心跳 → L1 周期 reload → L2 Cookie 持久化 → L3 systemd 自动重启）。服务通过 `Restart=always` 自动恢复崩溃，`loginctl enable-linger` 确保会话退出后不终止。PID 文件在 `违章查询/data/keepalive_<公司>.pid`，日志在 `违章查询/data/keepalive_<公司>.log`。Cookie 持久化由 `cookie_persist.py` 实现（修改 Chrome SQLite Cookies DB 的 `is_persistent`/`has_expires`/`expires_utc` 字段）。启动前先检查是否已有实例运行（防止重复启动）。Claude 会话结束时守护进程不受影响，继续在后台每 18 分钟执行保活周期。

