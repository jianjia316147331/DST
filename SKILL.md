***

name: DST违章查询
description: 当用户提到查询xxx公司/xxx车的违章、违法、违规、罚款、扣分、交通违法、12123时调用。通过PinchTab控制Chrome访问12123平台查询车辆违章，支持单/批量查询，支持车牌号自动识别省份。输出MD报告+飞书完成通知。
------------------------------------------------------------------------------------------------------------------------------------

# DST违章查询

## 概述

通过 **PinchTab**（12MB Go 二进制，零依赖）自动操作 Chrome 访问 12123 平台，完成单位用户扫码登录后查询车辆违章。输出：本地 MD 报告 + 飞书完成通知。支持防退出保活和飞书通知。

> PinchTab 内置 daemon 模式、持久 session、accessibility tree 快照（带 ref 编号）、stealth 注入和人体化操作。后台常驻，所有 CLI 命令共用同一个 Chrome session，无反复建连开销。

### 📂 文件存储约定

所有本地文件统一保存在 **项目根目录下的** **`violation_query/`** **文件夹**中，目录结构如下：

```
violation_query/
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
| helper      | `/home/openclaw/.claude/skills/DST违章查询/violation_helper.py`（直接引用，无需复制） |
| 输出目录        | `<cwd>/violation_query/`（通过 helper `get-dir -o <file>` 安全获取）               |

## 适用场景

- 用户提到"违章"、"违法"、"交通违法"、"罚款"、"扣分"、"违规"等关键词
- 用户需要批量查询多台车的违章信息
- 用户需要保持12123平台登录状态防过期

## 核心铁律

1. **只查未处理违章：** 只对"未处理"或"未缴费"状态的违章记录点击"查看详情"获取罚款金额和记分。"已处理且已缴费"的记录直接跳过。查询前先对比 SQLite 数据库：已存在且状态未变的跳过；状态变更的（从未处理→已处理）更新记录。
2. **随机延迟反爬：** 每条违章记录查询之间间隔 2-5 秒随机，每台车之间间隔 2-5 秒随机，点击操作间隔 1-2 秒随机。触发风控时立即停止所有操作。
3. **中文参数兼容：** Linux 终端原生支持 UTF-8，含中文路径/参数可直接传入 Bash。复杂中文参数操作（如 pinchtab find/wait 含中文描述）建议通过 helper 子命令（`pt-find` / `pt-wait`）完成以确保稳妥。**⚠️ 导航后 `pt-find` 有竞态风险（异步语义索引未就绪），详见「导航后元素定位」章节。**
4. **🔴 导航后元素定位 —— snap 优先于 find：** `pinchtab snap` 同步读 DOM，导航后立即可用。`pinchtab find` 异步语义索引，有 1-5s 竞态窗口。导航后首次定位元素**必须用 `snap` 直接获取 ref 编号后 `click`**，不得依赖 `pt-find`。`pt-find` 仅用于页面稳定后（>5s）或需要语义模糊匹配的场景。
5. **文件操作建议：** Linux 可直接在 Bash 中使用中文路径。为提高可靠性，推荐使用 helper 的 `get-dir` / `get-screenshot-dir` 等子命令获取路径后操作。
6. **风控熔断：** 三种触发条件：(1) 页面含"频繁"、"异常操作"、"黑名单"等关键词；(2) open_vehicle 连续 3 台车全部失败；(3) 查看详情 XHR 返回 `{"code":500,"message":"查询过于频繁"}`。任一触发立即终止所有进程并告警。XHR 监控由 `_setup_xhr_monitor()` 注入，`_check_xhr_rate_limit()` 轮询。
7. **禁止随意退出登录：** 检测到已登录状态（单位用户）时严禁退出。只需确认省份和公司匹配当前任务即可继续。只有实际查询遇到非单位用户报错时才重新登录。
8. **保活生命周期：** 登录成功 → 进入我的主页确认正常 → **🔴 调用 `ensure-keepalive` 自动启动保活**（脚本内置，不再依赖模型记忆；自动防重复）。保活程序导航到我的主页（车辆列表页）而非省份首页，确保有异常（风控/掉线）能及时发现。查询正常完成后保活继续运行不停止，除非用户明确要求或会话自然过期。**退出登录后不再自动重启**（systemd `RestartPreventExitStatus=42 43 44`）。
9. **弹窗防御：** 每次 `get-page-vehicles`、`open-vehicle`、`collect-violations` 操作前自动检测并关闭"本人已知晓"等系统弹窗，确保表格数据可访问。
10. **🔴 铁律：先查人再开登录页（防二维码失效）：** 用户指定通知对象（姓名/手机号/群名）时，**必须先完成飞书 ID 查询（查人/查群），确认 ID 获取成功后再打开 12123 登录页截图二维码**。二维码有效期约 5 分钟，先查人可避免扫码等待期间二维码过期。
11. **🔴 铁律：批量查询必须走 helper 已有子命令：** 批量查询（全量/多台车）的循环逻辑必须通过 `violation_helper.py` 已有子命令组合实现（`get-page-vehicles` → `open-vehicle` → `collect-violations` → `go-back` → `save-detail-progress` → `click-page`）。**禁止为批量查询新写独立 Python 脚本**，只能写极简调用封装（循环体内只调 helper 子命令）。
12. **🔴 铁律：违章详情必须逐个查询（不可跳过）：** 车辆列表上显示有未处理违章的车辆，**必须进入详情页 + 逐条点击"查看详情"获取真实罚款金额和记分**。禁止只读列表数据不查详情、禁止仅凭列表摘要生成报告。此条为最高优先级铁律，违反即为查询失败。
13. **🔴 数据落库策略：查一条落库一条 → 逐条即时写入：** `collect-violations` 每提取完一条违章详情，调用方应立即通过 `db-insert-violation` 写入 SQLite。不使用中间 JSON/JS 文件暂存、不攒到最后批量落库。这样即使中途崩溃，已查询的违章数据不会丢失。helper `db-insert-violation` 支持按自然键 upsert（车牌+时间+地点+行为），重复写入不会产生脏数据。
14. **🔴 公司名称以平台公司列表页为准（权威来源）：** Profile 注册和数据库写入时使用的公司名称，**必须以扫码登录后 12123 平台公司列表页实际显示的名称为准**。用户输入的公司名（如"深圳公司"）和 `profile-lookup` 模糊匹配返回的名称仅供定位 Profile 和平台 URL 使用，不得直接作为最终公司名落库。扫码登录后、选择公司前，必须从公司列表页提取实际公司名称，对比校验后再写入 `profile-register` 和 `db-insert-company`。若 profile 中已有旧名称但与平台实际名称不一致，以平台为准更新覆盖。
15. **🔴 平台排序假设 + 双模式查询：** 12123 平台车辆列表按"未处理违章数"降序排列（有未处理违章的车辆排在最前，均为 0 的排在后）。批量查询支持两种模式：

   | 模式 | 行为 | 触发条件 |
   |------|------|----------|
   | **自动检测 `auto`（默认）** | 连续 2 页 `unprocessed` 全部为 0 时安全终止 | 用户未明确指定查询意图 |
   | **全量扫描 `full`** | 忽略清零页，持续查询直到所有页面遍历完成 | 用户明确说"首次"/"首批"/"第一次"/"全部查完"等 |

   > **意图识别规则：** 用户提到"首次查询"、"首批"、"第一次查"、"全部查一遍"、"全量扫描"、"从头查"等全量意图关键词时 → 使用 `full` 模式。用户未明确表达 → 默认 `auto` 模式（自动检测停止）。此假设同样适用于违章详情页翻页：`auto` 模式下当前页无任何未处理违章时后面的详情页也无需翻看；`full` 模式下所有详情页逐页扫描。严禁在未验证此假设的前提下对有未处理的页面提前终止。

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
| `get-dir`            | 输出 `violation_query/` 根目录路径        | Bash: `python <helper> get-dir -o <file>` |
| `get-screenshot-dir` | 输出 `screenshots/` 子目录路径 | Bash: `python <helper> get-screenshot-dir` |
| `get-report-dir`     | 输出 `reports/` 子目录路径     | Bash: `python <helper> get-report-dir`     |
| `get-data-dir`       | 输出 `data/` 子目录路径        | Bash: `python <helper> get-data-dir`       |
| `init-db`            | 初始化 SQLite 数据库，返回路径      | Bash: `python <helper> init-db`            |
| `db-insert-company`  | 增量写入/更新公司记录              | 通过 Python 脚本（stdin JSON 或 CLI 参数）       |
| `db-insert-vehicle`  | 增量写入/更新车辆记录              | 通过 Python 脚本（stdin JSON 或 CLI 参数）       |
| `db-insert-violation`| 增量写入/更新违章记录（按自然键匹配）    | 通过 Python 脚本（stdin JSON）                |
| `db-check-vehicle-collected`| 查询车辆今日是否已采集（--plate-number + --query-date） | 通过 Python 脚本                              |
| `profile-lookup`    | 查公司→Profile 映射（精准→模糊），返回 profile 信息+登录态 | 通过 Python 脚本                              |
| `profile-list`      | 列出所有已注册的公司 Profile          | Bash: `python <helper> profile-list`       |
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
| `gen-result-msg`     | 生成简洁完成通知 JSON（仅摘要，无飞书文档） | 通过 Python 脚本                              |
| `upload-image`       | 上传图片获取 image\_key       | 通过 Python 脚本                              |
| `send-msg`           | 发送 post 消息              | 通过 Python 脚本                              |
| `send-image-msg`     | 发送独立图片消息                | 通过 Python 脚本                              |
| `search-user`        | 按姓名查飞书用户                | 通过 Python 脚本（参数 stdin/--query 传入）         |
| `search-chat`        | 按群名搜索群                  | 通过 Python 脚本                              |
| `batch-get-id`       | 按手机号查用户                 | 通过 Python 脚本                              |
| `pt-find`            | pinchtab find 中文描述      | 通过 Python 脚本                              |
| `pt-wait`            | pinchtab wait 中文文本      | 通过 Python 脚本                              |
| `poll-login`         | 等待登录完成；推荐 `--browser-only` 模式（纯浏览器检测，无飞书 API 调用）；支持 QR 失效检测 + 自动刷新上限 | 通过 Python 脚本                              |
| `extract-message-id` | 从响应 JSON 提取 message\_id | 通过 Python 脚本                              |
| `run-js`             | 从文件执行含中文的 JS（无需经过 bash） | 通过 Python 脚本                              |
| `list-vehicles`      | 提取车辆列表+分页信息为 JSON       | 通过 Python 脚本                              |
| `open-vehicle`       | 双击第 N 台车进入详情页           | 通过 Python 脚本                              |
| `collect-violations` | 逐条点击"查看详情"提取罚金/记分，支持SQLite增量对比、详情页智能翻页、违章级断点续跑、`--auto-insert` 逐条即时落库、`--query-mode auto|full` 控制详情页翻页策略 | 通过 Python 脚本                              |
| `go-back`            | 从详情页返回车辆列表              | 通过 Python 脚本                              |
| `click-page`         | 点击分页（next/prev/页码）；next/prev 内部转为明页号导航（避免点"下一页"元素跳错页的 bug），页码支持智能跳转      | 通过 Python 脚本                              |
| `save-detail-progress` | 保存进度（--company + --query-date 隔离）      | 通过 Python 脚本                              |
| `load-detail-progress` | 加载进度（--company + --query-date 隔离）           | 通过 Python 脚本                              |
| `reset-detail-progress`| 安全重置详情进度（--company + --query-date） | 通过 Python 脚本                              |
| `get-page-vehicles`    | 获取当前页车辆列表+页码+总页数         | 通过 Python 脚本                              |
| `get-login-type`       | 检测登录类型（单位/个人/未登录）        | 通过 Python 脚本                              |
| `check-login-state`    | **统一登录状态检测**：URL+DOM (Tier 1) 初始检查 + 关键字匹配 (Tier 2) 扫码轮询检测 | 通过 Python 脚本                              |
| `find-plate-page`      | 在断点页找不到上次车辆时向前搜索（最多3页） | 通过 Python 脚本                              |
| `session_manager.py`   | **会话+实例生命周期管理**（init/bind/release/list/current + instance-discover/instance-status）；通过 `VIOLATION_TAB_ID` + `VIOLATION_INSTANCE_PORT` 环境变量自动注入 `--tab` + `--server` 到所有 PinchTab 命令 | `eval $(python3 session_manager.py init --label "name" [--instance-port <port>])` |
| `keepalive-health`     | 检查保活守护进程健康状态（health file + PID），返回 alive + 状态详情 | 通过 Python 脚本                              |
| `ensure-keepalive`     | **自动启动保活守护进程**（登录后调用）；检查是否已运行，未运行则 `systemctl start`；已运行则跳过（防重复） | 通过 Python 脚本                              |
| `save-notify`          | 持久化扫码人信息到保活通知文件（--company, --project-root, --type, --id, --label, [--at-user-id, --at-user-name]），供保活守护进程自动恢复扫码时使用 | 通过 Python 脚本                              |

> **`profile-lookup` 匹配策略**：精准匹配（`=`）→ 模糊匹配（`LIKE '%keyword%'`）。模糊命中 1 条自动采用（`match_type: "fuzzy"`），命中多条返回 `candidates` 列表需用户确认。返回结果包含 `keepalive_alive` (bool) 和 `keepalive_state` (str) 字段。
> **⚠️ 注意：`profile-lookup` 返回的公司名仅供定位 Profile 和平台 URL，不得直接作为最终公司名落库。最终公司名必须以扫码登录后 12123 平台公司列表页实际显示为准（铁律 #14）。**
> **`profile-list`**：列出所有已注册公司 Profile，用于诊断回退或手动查找。

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

### 🔴 导航后元素定位：snap 优先，find 兜底

**根因：** `pinchtab snap` 是同步操作，直接读取 DOM accessibility tree，导航完成后立即可用。`pinchtab find` 内部使用异步语义索引（AI/embedding 匹配），导航后存在 **1-5 秒竞态窗口**——即使 `snap` 已能看到目标元素，`find` 也可能因为索引未完成而返回空（`No element found`）或空字符串。

**优化策略：**

| 场景 | 推荐做法 | 原因 |
|------|---------|------|
| 导航后首次定位 | `snap` → 从输出中 grep 目标文本 → 直接 `click <ref>` | 零等待，100% 可靠 |
| 页面已稳定（>5s） | `pt-find` 可用 | 语义索引已完成 |
| 需要语义理解（模糊匹配） | `pt-find`（等待 3-5s 后调用） | find 的 AI 匹配能力不可替代 |
| 批量操作中的反复定位 | 缓存 ref 编号，直接 `click` | 避免每次都等索引 |

**标准模式（推荐）：**

```python
# ❌ 错误：导航后立即 find，可能返回空
pinchtab nav "https://fj.122.gov.cn"
time.sleep(2)
ref = pinchtab find "我的主页" --ref-only  # 可能返回空!

# ✅ 正确：导航后 snap → 手动定位 ref → 直接 click
pinchtab nav "https://fj.122.gov.cn"
time.sleep(3)
snap = pinchtab snap
# snap 输出: e2:link "我的主页[*磊]"
# 直接解析 ref 编号点击
pinchtab click e2
```

**`pt-find` 返回空时的排查清单：**

1. 先用 `pinchtab snap` 确认元素确实在 accessibility tree 中
2. 如果 `snap` 有元素但 `find` 返回空 → **竞态问题**，等 3-5s 重试
3. 如果 `snap` 也没有元素 → 页面未加载完成或元素被弹窗遮挡
4. 如果反复 `find` 都返回空 → 回退到 `snap` + grep 定位 ref

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
import os, json, shutil, subprocess, sys

# Helper lives in the skill directory — no /tmp copy needed
home = os.path.expanduser('~')
helper = os.path.join(home, '.claude', 'skills', 'DST违章查询', 'violation_helper.py')

# detect lark-cli (on PATH)
lark = shutil.which('lark-cli') or 'lark-cli'

# create output dir
query_dir = os.path.join(os.getcwd(), 'violation_query')
os.makedirs(query_dir, exist_ok=True)

result = {
    'helper': helper,
    'lark_cli': lark,
    'query_dir': query_dir,
    'python': sys.executable
}
print(json.dumps(result, ensure_ascii=False))
```

> 初始化完成后，后续 Python 调用使用 `python3 <HELPER路径>` 执行。关键数据通过 `-o <file>` 写入文件获取。

### 会话标签页 + 实例隔离（每次执行必做）

> **背景：** 多个 Claude Code 进程可能同时查询不同公司。Chrome 同 profile 下 cookie 全局共享，不同公司的 12123 SSO 会话会互相干扰（gab.122.gov.cn 自动跳转到有活跃 session 的省份）。`--tab` 只能隔离导航，无法隔离 cookie。**不同公司必须使用不同的 PinchTab 实例（独立 profile + 独立端口）**，同一公司内不同任务用 `--tab` 隔离导航。

**双层隔离机制：**

| 层级 | 机制 | 环境变量 | 隔离什么 |
|------|------|---------|---------|
| 实例 | `--server http://127.0.0.1:<port>` | `VIOLATION_INSTANCE_PORT` | Cookie / 登录态（不同公司间） |
| 标签页 | `--tab <id>` | `VIOLATION_TAB_ID` | 导航状态（同一公司内并发任务） |

`violation_helper.py._run()` 和 `keepalive_daemon.py._run_pinchtab()` 已内置两层自动注入——读取环境变量，对每个 PinchTab 命令同时注入 `--server` 和 `--tab`。

**执行流程：**

1. （首次或实例重启后）`python3 session_manager.py instance-discover` 同步运行中实例到 profiles 表
2. `profile-lookup --company "公司名"` 返回 `instance_port` + `instance_running` 状态
3. 调用 `session_manager.py init --instance-port <port>` 创建本会话专属标签页
4. `eval` 输出设置 `VIOLATION_TAB_ID` + `VIOLATION_INSTANCE_PORT` 环境变量
5. 后续所有 PinchTab 操作自动携带 `--server <url>` + `--tab <id>`

```python
# 写入 /home/openclaw/session_tab_{pid}.py
import subprocess, os

py = r'python3'
session_mgr = r'/home/openclaw/.claude/skills/DST违章查询/session_manager.py'
helper = r'/home/openclaw/.claude/skills/DST违章查询/violation_helper.py'

# Step 0: Ensure instance is discovered (first time or after restart)
subprocess.run([py, session_mgr, 'instance-discover'], ...)

# Step 1: Look up profile to get instance_port
lookup = subprocess.run(
    [py, helper, 'profile-lookup', '--company', '深圳公司'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
profile = json.loads(lookup.stdout)
instance_port = profile.get('instance_port')  # e.g. 9872

# Step 2: Create tab on the correct instance + set env vars
result = subprocess.run(
    [py, session_mgr, 'init', '--label', 'session_name',
     '--instance-port', str(instance_port)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
# Output: "export VIOLATION_TAB_ID=xxx\nexport VIOLATION_INSTANCE_PORT=9872"
for line in result.stdout.strip().split('\n'):
    if line.startswith('export '):
        key, val = line.replace('export ', '').split('=', 1)
        os.environ[key] = val

# Step 3: All subsequent helper calls auto-inject BOTH --server AND --tab
# Command becomes: pinchtab --server http://127.0.0.1:9872 nav <url> --tab <id>
subprocess.run([py, helper, 'check-login-state'], ...)
subprocess.run([py, helper, 'get-page-vehicles'], ...)
```

> **关键约束：** 不再需要手动 `switch-tab`。设置环境变量后，`_run()` 和 `_run_pinchtab()` 自动对所有 pinchtab 命令注入 `--server` 和 `--tab`。实例路由通过 PinchTab CLI 的 `--server` 全局标志（**非** `PINCHTAB_PORT` 环境变量——该变量 CLI 不认）。`tab` 和 `close` 命令不受 tab 注入影响（tab 管理命令本身即可指定目标）。

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

1. **实例发现**（首次或 PinchTab 重启后）：`python3 session_manager.py instance-discover` 同步运行中实例的端口号到 profiles 表
2. **查映射表**：调用 `profile-lookup --company <公司名>`
   - 返回结果包含 `instance_port` 和 `instance_running`（PinchTab 实例是否真实存活）
   - `profile-lookup` 内置**两级匹配**：优先精准匹配（`=`），未命中自动降级模糊匹配（`LIKE '%keyword%'`）
   - 模糊匹配**命中 1 条** → 自动采用，`match_type: "fuzzy"`，继续后续流程
   - 模糊匹配**命中多条** → 返回 `need_confirm: true` + `candidates` 列表，**向用户确认**后再继续
3. **命中**（`found: true`）：
   - 切换到对应 Profile 的 Instance（通过 `instance_port`，如 9872）
   - 通过 `session_manager.py init --instance-port <port>` 创建本会话标签页，设置 `VIOLATION_TAB_ID` + `VIOLATION_INSTANCE_PORT`
   - 导航到对应 12123 平台（`platform_url`，如 `https://fj.122.gov.cn`）
   - 若 `keepalive_alive=true` → 信任保活，点击"我的主页"进入业务页面，直接跳到第四步查询
   - 若 `keepalive_alive=false` → 调 `check-login-state` 自行验证登录态
   - 验证登录仍有效 → 点击"我的主页"进入业务页面，跳过扫码登录
   - `is_logged_in=0` → 登录态已失效，走重新登录流程
   - `is_logged_in=1` 但登录过期 → 重新扫码 → `profile-register` 更新 `last_login`
4. **未命中**（`found: false`，且 `need_confirm` 不为 `true`）：
   - **🔴 必须先做诊断回退，禁止直接跳到登录流程：**
     a. 调用 `profile-list` 列出所有已注册公司
     b. 检查 systemd 服务：`systemctl --user list-units --type=service | grep -i keepalive`
     c. 检查 data 目录：`ls violation_query/data/keepalive_health_*.json`
     d. 若诊断发现匹配的公司 → 用完整公司名重试 `profile-lookup`
     e. 仅当诊断确认无任何匹配时才走完整登录流程
   - 登录成功后，**从平台公司列表页 snap 提取实际公司名称**，调用 `profile-register --company <平台实际公司名> --profile-name <profile> --platform-url <url>` 写入映射。**禁止使用用户输入的公司名或模糊匹配结果直接落库**，必须以平台页面显示为准。`profile-register` 完成后会自动调用 `instance-discover` 绑定实例端口

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

**🔑 核心原则：优先信任保活守护进程（keepalive daemon）的结果，避免重复检测。**

保活守护进程每 18 分钟 reload 页面并持续验证登录态，其 health file 是最新的登录状态快照。查询流程应先查看保活结果，仅在保活不可用时才自行检测。

**登录状态判断流程（保活优先）：**

1. **查保活健康状态**：调用 `profile-lookup --company "公司名"`（支持模糊匹配，见 Profile 隔离章节）
   - 返回结果包含 `keepalive_alive` (bool) 和 `keepalive_state` (str)
   - 也可单独调用 `keepalive-health --company "公司名"` 获取完整健康详情
   - **若 `keepalive-health` 返回 `alive: false, reason: "no health file"`**：不要直接断定保活未运行，先用 `ls violation_query/data/keepalive_health_*.json` 列出所有 health file，用完整公司名重试
2. **keepalive_alive=true 且 is_logged_in=true** → **信任保活，直接进入第四步查询流程**
   - 跳过 `check-login-state` 检测
   - **直接导航到 `platform_url`（如 `https://fj.122.gov.cn`），不要经过 `gab.122.gov.cn/m/login` 认证网关**
   - 导航后按第四步流程：选择公司 → 点击"我的主页" → 租赁车辆管理
3. **keepalive_alive=false 或 is_logged_in=false** → 回退到自行检测 `check-login-state`

**自行检测（仅在保活不可用时使用）**，`check-login-state` 子命令（两层检测）：

| 层级 | 方法 | 用途 | 说明 |
| --- | --- | --- | --- |
| **Tier 1** | URL + DOM | 初始登录状态检查 | 通过 `window.location.href` 判断是否在登录页 (`gab.122.gov.cn/m/login`)；已登录时通过 DOM 确认业务菜单存在 |
| **Tier 2** | 关键字匹配 | 扫码轮询检测 | 检测 "退出"/"车辆管理"/"公司列表" 等业务关键词确认扫码成功；此层仅在 Tier 1 不确定时作为补充 |

```bash
# 初始检查（默认 URL+DOM，失败时自动回退关键字）
python3 /home/openclaw/.claude/skills/DST违章查询/violation_helper.py check-login-state

# 仅 URL+DOM（初始检查，推荐）
python3 /home/openclaw/.claude/skills/DST违章查询/violation_helper.py check-login-state --mode url

# 仅关键字匹配（扫码轮询检测用）
python3 /home/openclaw/.claude/skills/DST违章查询/violation_helper.py check-login-state --mode keyword
```

**返回值**：`state: "logged_in" | "login_page" | "rate_limited" | "unknown"`，exit code 对应 0/1/2/3。

**自行检测判断流程**：
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
helper = r'/home/openclaw/.claude/skills/DST违章查询/violation_helper.py'
result = subprocess.run(
    [py, helper, 'search-user', '--query', '用户姓名'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
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
pinchtab screenshot -o "/home/openclaw/violation_query/login_qrcode_YYYYMMDD.png"
```

用 Python 脚本确认文件存在（避免中文路径在 bash 中损坏）。

若文件不存在或大小为 0，重新截图一次；两次均失败则提示用户手动截图。

**3.5 上传二维码截图并获取 image\_key：**

通过 Python 脚本调用 helper 的 `upload-image` 子命令：

```python
# 写入 /home/openclaw/upload_qr_{pid}.py
import subprocess
py = r'python3'
helper = r'/home/openclaw/.claude/skills/DST违章查询/violation_helper.py'
result = subprocess.run([py, helper, 'upload-image',
    '--dir', r'/home/openclaw/violation_query',
    '--file', 'login_qrcode_YYYYMMDD.png'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
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
helper = r'/home/openclaw/.claude/skills/DST违章查询/violation_helper.py'

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
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8').stdout

with open(r'/home/openclaw/lark_login_msg_{pid}.json', 'w', encoding='utf-8') as f:
    f.write(msg_json)

result = subprocess.run(
    [py, helper, 'send-msg',
     '--msg-file', r'/home/openclaw/lark_login_msg_{pid}.json',
     '--chat-id', 'oc_xxx'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
print(result.stdout)
```

**提取 message\_id：**

```python
msg_id = subprocess.run(
    [py, helper, 'extract-message-id'],
    input=result.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8').stdout.strip()
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
  file://<项目根目录>/violation_query/login_qrcode_YYYYMMDD.png

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
| `--browser-only` | false | **🔴 推荐模式**：纯浏览器检测，**不调用飞书 API**。登录检测：每 ~10s 通过 `pinchtab text+snap` 关键词匹配。QR 过期检测：每 ~60s 通过 `pinchtab eval` JS 检测。此模式下 `--chat-id`/`--target-user-id`/`--qr-msg-id` 可选 |
| `--chat-id` | (browser-only 时可选) | 飞书会话 ID |
| `--target-user-id` | (browser-only 时可选) | 目标用户 open_id |
| `--qr-msg-id` | (browser-only 时可选) | QR 通知消息 ID（群聊时用于 reply_to 匹配） |
| `--qr-sent-as` | user | 谁发的 QR 消息：`bot`（bot-用户 P2P，跳过 reply_to 匹配）或 `user`（群聊，需 reply_to 匹配） |
| `--max-duration` | 300 | 总轮询时长（秒） |
| `--check-qr` | false | [legacy] 轮询耗尽后启用浏览器 QR 失效检测 |
| `--check-login` | false | [legacy] 飞书轮询期间启浏览器自动检测登录。每约 30s 检测，检测到即退出 0 |
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

**基础调用（推荐 browser-only 模式）：**

```python
# 写入 /home/openclaw/poll_login_{pid}.py
import subprocess, sys
py = r'python3'
helper = r'/home/openclaw/.claude/skills/DST违章查询/violation_helper.py'
# 🔴 推荐：纯浏览器检测模式，不调用飞书 API
result = subprocess.run(
    [py, helper, 'poll-login',
     '--browser-only',
     '--max-duration', '300',
     '--qr-refresh-count', '<N>',
     '--max-qr-refreshes', '3'],
    encoding='utf-8')
sys.exit(result.returncode)
```

**Legacy 调用（飞书轮询 + 浏览器检测）：**

```python
# 保留用于群聊场景（需要监听用户反馈 QR 过期）
result = subprocess.run(
    [py, helper, 'poll-login',
     '--chat-id', 'oc_xxx',
     '--target-user-id', 'ou_xxx',
     '--qr-msg-id', 'om_xxx',
     '--qr-sent-as', 'bot',
     '--max-duration', '300',
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
helper = r'/home/openclaw/.claude/skills/DST违章查询/violation_helper.py'

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

**3.9 登录后校验公司名称并进入我的主页确认（必须执行）：**

> **登录成功后，必须先校验平台实际公司名称、进入「我的主页」确认页面正常、未被风控，再启动保活。**

1. 扫码/轮询确认登录成功后
2. **🔴 公司名称校验（必须执行）：** 在省份首页 `pinchtab snap` 提取公司列表中的**实际公司名称**。将此名称与用户意图（如"深圳公司"）做对比校验：
   - 若平台只有一家公司 → 直接采用该公司名，与用户意图确认匹配
   - 若平台有多家公司 → 根据用户意图选择匹配的公司，无法唯一确定时向用户确认
   - **以平台实际显示的公司名称为准**，调用 `profile-register --company <平台实际公司名> --profile-name <profile> --platform-url <url>` 标记 `is_logged_in=1`
   - 若 profile 中已有旧名称但与平台实际名称不一致 → **以平台为准更新覆盖**
3. 选择公司 → 点击「我的主页」
4. 检查页面状态：
   - 有「退出」和「我的主页[*用户名]」→ 登录正常 ✅
   - 有车辆列表正常加载 → 未被风控 ✅
   - 有「频繁」「异常操作」「验证码」等关键词 → 风控 ⚠️，停止操作
5. **持久化扫码人信息：** 调用 `save-notify` 将扫码人写入 `keepalive_notify_<公司>.json`。这样后续保活 daemon 独立启动时无需重新指定通知对象，自动恢复 QR 会直接发给此人。

   - **发给个人（P2P）：** `save-notify --company <公司名> --project-root <项目根目录> --type user --id <open_id> --label "<姓名>"`
   - **发给群聊（@指定人）：** `save-notify --company <公司名> --project-root <项目根目录> --type chat --id <chat_id> --label "<群名>" --at-user-id <open_id> --at-user-name "<姓名>"`
   - 带 `--at-user-id` 时，保活恢复 QR 会在群中 @同一个人（与查询流程 QR 的 @提及行为一致）
6. **🔴 自动启动保活（必须执行）：** 调用 `ensure-keepalive --company <公司名> --project-root <项目根目录>` **自动检测并启动 systemd 保活服务**。此命令内置防重复逻辑：若 keepalive daemon 已运行则跳过（返回 `already_running`）；若未运行则自动 `systemctl --user start` + `enable`。**不再依赖模型记忆手动启动保活，此步骤为脚本强制内置**。
7. 保活启动后再进行后续查询操作

### 第四步：选择公司 → 进入业务主页 → 车辆列表页

> **🔑 登录成功并确认我的主页正常后，后续路径统一如下：**
>
> **注意：首次登录时已在上一步（3.9）选择了公司并进入了我的主页，本步骤直接复用当前页面即可。**
> **保活信任 / 自行检测确认已登录时，按以下步骤操作：**

1. **选择公司**：`pinchtab snap` 获取页面上的公司列表。省份信息用于辅助匹配公司（如”成都”匹配名称中含”成都”的公司）。单台车查询时：若仅一家公司匹配省份则直接进入该公司；若多家公司匹配省份，向用户询问确认是哪家公司。全量查询无法唯一确定时同样追问。
2. **点击「我的主页」**：选择公司后，点击”我的主页”按钮进入该公司的业务主页（即租赁车辆管理 vehlist.html）。
   - 方法：`pinchtab snap` 获取 ref → `pinchtab click <ref>` → dismiss popup
   - 我的主页 = 车辆列表页，包含「退出」「我的主页[*用户名]」等登录特征
3. **检查页面状态**：确认无风控关键词，车辆列表正常加载
4. **车辆列表已显示**：我的主页就是租赁车辆管理页，无需额外导航。如需进入特定业务，点击左侧「租赁车管理」→ 确保「业务办理」被选中。

### 第五步：判断查询模式

- **指定车辆查询**：用户提供车架号或具体车牌号 → 走第六步-A
- **全量查询**：用户要求查公司名下所有车辆 → 走第六步-B

#### 全量查询模式选择（`--query-mode`）

在全量查询（第六步-B）启动前，根据用户意图自动判断查询模式：

| 用户意图关键词 | 模式 | `--query-mode` 值 | 行为 |
|---------------|------|-------------------|------|
| "首次"、"首批"、"第一次"、"全部查一遍"、"全量扫描"、"从头查"、"完整查询" | **全量扫描** | `full` | 遍历所有页面，不因连续清零页提前终止 |
| 未明确指定意图（缺省） | **自动检测** | `auto` | 连续 2 页 `unprocessed` 全部为 0 时安全终止（默认） |

**意图识别规则（必须遵守）：**
1. 用户消息中包含上述全量意图关键词 → **直接采用 `full` 模式**，无需向用户确认
2. 用户未提及任何模式关键词 → **默认 `auto` 模式**，无需向用户确认
3. 当无法确定用户意图时（如"你看着办"、"随便"等模糊表述）→ 默认 `auto` 模式
4. **不要在对话中询问用户选择哪种模式**——除非用户明确指出两种可能性但未做选择

> `--query-mode` 参数传递给第六步-B 的批量查询包装脚本，脚本在循环体内据此决定是否启用提前终止逻辑。

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
> - `collect-violations --plate <车牌> --query-date <日期> [--query-mode auto|full]` — 逐条采集违章详情，`--query-mode` 控制详情页翻页策略（同车辆列表页）
> - `db-insert-violation` — 每条违章结果立即落库（逐条 upsert，不攒批）
> - `go-back` — 返回车辆列表
> - `click-page --target N` — 翻页（直接用明页号，禁止 `--target next`）
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

Usage: python3 batch_query_{pid}.py --company <公司名> [--query-mode auto|full]
  --query-mode auto  (default) 连续2页清零时自动终止
  --query-mode full             全量扫描所有页面，不提前终止
"""
import subprocess, json, time, random, sys

py = r'python3'
helper = r'/home/openclaw/.claude/skills/DST违章查询/violation_helper.py'
date = time.strftime('%Y-%m-%d')
company = '<公司名称>'  # set by caller based on selected company
query_mode = 'auto'   # default: auto-detect stop

# Parse CLI args
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == '--company' and i + 1 < len(args):
        company = args[i + 1]; i += 2
    elif args[i] == '--query-mode' and i + 1 < len(args):
        query_mode = args[i + 1]; i += 2
    else:
        i += 1

print(f"Batch query: company={company}, mode={query_mode}")

def h(cmd_args):
    """Call helper subcommand, return stdout stripped."""
    result = subprocess.run([py, helper] + cmd_args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')
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
all_clean_streak = 0
while True:
    # Dismiss popup + get current page
    h(['dismiss-popup'])
    page_data = json.loads(h(['get-page-vehicles']))
    vehicles = page_data.get('vehicles', [])
    current_page = page_data.get('page', 1)
    total_pages = page_data.get('total_pages', 1)
    print(f"\n=== Page {current_page}/{total_pages}: {len(vehicles)} vehicles ===")

    # Early termination logic (auto mode only):
    # Platform sorts by unprocessed count descending. In auto mode, 2 consecutive
    # all-clean pages means all remaining pages are clean → safe to stop early.
    # In full mode, skip this check and scan every page.
    unprocessed_vehicles = [v for v in vehicles if v.get('unprocessed', 0) > 0]
    if len(unprocessed_vehicles) == 0:
        if query_mode == 'auto':
            all_clean_streak += 1
            print(f"  All clean page (streak {all_clean_streak}/2)")
            if all_clean_streak >= 2:
                print("Early stop (auto mode): 2 consecutive pages with no unprocessed violations.")
                break
        else:
            print(f"  All clean page (full mode: continuing regardless)")
        # Go to next page
        h(['click-page', '--target', str(current_page + 1)])
        time.sleep(random.uniform(2, 5))
        if current_page >= total_pages:
            break
        continue
    else:
        all_clean_streak = 0

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
        violations_out = h(['collect-violations', '--plate', plate, '--query-date', date, '--auto-insert', '--query-mode', query_mode])
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

    next_page = current_page + 1
    if next_page > total_pages:
        break
    print(f"\nPage {current_page} done, navigating to page {next_page}...")
    h(['click-page', '--target', str(next_page)])
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
- **🔴 查询模式传参**：调用批量查询脚本时必须传入 `--query-mode`（值为 `auto` 或 `full`），由第五步意图识别结果决定。脚本内部根据此参数决定是否启用提前终止逻辑

### 第七步：结果汇总 — 双重输出 + 飞书通知

> **🔴 查询完成后不生成飞书文档/多维表格，仅发送简洁完成通知。**

#### 输出一：SQLite 数据库

> 公司记录在本步骤写入，车辆记录已在第六步-B逐台即时入库。违章记录在查询过程中已逐条落库（第六步-B），此处仅做验证。

通过 `init-db` 初始化数据库，`db-insert-company` / `db-insert-vehicle` 写入公司和车辆记录。违章记录已通过 `db-insert-violation` 在采集过程中逐条写入，此处可用 SQL 查询验证数据完整性。

表结构：
- **companies**: id, name, query_date
- **vehicles**: id, company_id, plate_number, plate_type, plate_type_label, status_code, status_label, inspection_date, unprocessed_count, query_date
- **violations**: id, vehicle_id, plate_number, violation_time, violation_location, violation_behavior, violation_code, fine_amount, points, handling_status, payment_status, authority, 等

#### 输出二：本地 Markdown 报告

保存到 `violation_query/reports/violation_query报告_[公司名称]_YYYY-MM-DD.md`（由 Write tool 写入）

```markdown
# 车辆violation_query报告

**查询公司：** [公司] | **查询日期：** YYYY-MM-DD | **平台：** [省份]12123
**查询车辆：** N 台 | **违章总数：** M 条

---

## 查询结果汇总

| 序号 | 车牌号 | 号牌种类 | 违章时间 | 违章地点 | 违章行为 | 违章代码 | 罚款(元) | 记分 | 处理状态 | 缴款状态 |
|------|--------|---------|----------|----------|----------|----------|----------|------|----------|----------|

> 处理状态 cod：-1已删除 / 0未处理 / 1已处理 / 2已转出 / 9无需处理
> 缴款状态 cod：0未缴款 / 1已缴款 / 9无需缴款
```

#### 输出三：查询完成飞书通知（简洁摘要）

通过 Python 脚本调用 `gen-result-msg` → `send-msg`，向同一接收对象发送**简洁完成通知**，仅包含：

| 字段 | 说明 |
|------|------|
| 查询车辆数 | 本次查询车辆总数 |
| 扫描车辆 | 本次在车辆列表页遍历的车辆总数（含无违章跳过的） |
| 新入库车辆 | 本次新增到 SQLite vehicles 表的车辆数 |
| 新增违章 | 本次新写入 SQLite 的违章条数 |
| 新增扣分 | 新增违章的记分总和 |
| 新增待缴费 | 新增违章的罚款总额（仅未处理/未缴款） |
| 对比历史已处理 | 上次查询为未处理、本次查询已变为已处理/已缴款的违章数（>0 时显示） |

**通知模板（gen-result-msg 自动生成）：**
```
✅ 12123违章查询完成
🏢 <公司名>  🕐 <日期>
📋 扫描车辆：S 台  🚗 查询车辆：N 台  🆕 新入库：V 台
⚠️ 新增违章：M 条
📛 新增扣分：P 分
💰 新增待缴费：X 元
✅ 对比历史已处理：R 条   ← 仅当 R > 0 时显示
数据来源于12123平台，仅供参考。
```

> **不再生成飞书云文档和飞书多维表格。** SQLite 数据库 + 本地 MD 报告保留用于数据存档。

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
│     pinchtab reload → dismiss popup → 点击我的主页    │
│     作用: 全量刷新保持 session 活跃                     │
│     原因: 我的主页（车辆列表页）能即时暴露异常           │
│           风控/掉线都会导致页面异常，不会漏检             │
├─────────────────────────────────────────────────────┤
│ L2  Cookie 持久化层                                  │
│     cookie_persist.py: 修改 Chrome SQLite Cookies DB │
│     将 12123 域名的 session cookie → persistent       │
│     设置 is_persistent=1, has_expires=1, 30天过期     │
│     每次 keepalive cycle + 每次 pinchtab 重启时执行    │
├─────────────────────────────────────────────────────┤
│ L3  systemd 自动重启层                                │
│     pinchtab.service: Restart=always, RestartSec=5   │
│     keepalive-12123.service: Type=notify,              │
│       WatchdogSec=240, Restart=always, RestartSec=10   │
│     看门狗: 独立看门狗定时器每 60s 无条件发送 WATCHDOG=1,  │
│       心跳成功时也发送, 周期开始时发送 → 最长间隔 60s     │
│       (远小于 WatchdogSec=240, 4x 安全余量)              │
│     若 daemon 真正卡死 >240s → systemd SIGABRT → 自动重启 │
│     ExecStartPre: cookie_persist.py (Chrome 启动前)   │
│     ExecStopPost: cookie_persist.py (Chrome 停止后)   │
│     作用: 崩溃自动拉起 + 看门狗卡死检测 + 重启后免扫码   │
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
    ├── Type=notify, WatchdogSec=240
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

### ⚠️ 看门狗超时陷阱

**现象：** 保活 daemon 频繁重启（restart counter 持续增长），日志中无 "exiting" 或 "signal" 记录，进程"凭空消失"。

**根因：** systemd `Type=notify` + `WatchdogSec=240` 要求 daemon 在 240s 内必须调 `sd_notify("WATCHDOG=1")`。旧代码**仅在心跳成功时才发送 WATCHDOG=1**。当最后心跳 → 下一个 reload 周期的时间窗口超过 240s（例如心跳错过、页面慢响应、或 reload 阶段本身 20-30s 无通知），systemd 发送 **SIGABRT**（不是 SIGTERM！），daemon 未捕获该信号 → 无日志死亡 → systemd 10s 后重启。

**修复（2026-07-08）：**

1. **独立看门狗定时器**：在 18-min sleep 循环内增加 60s 间隔的 `WATCHDOG_INTERVAL`，**无条件**发送 `WATCHDOG=1`，与心跳成功/失败完全解耦
2. **周期开始时发送**：每轮 reload 周期开始前立刻发送一次 `WATCHDOG=1`，覆盖 reload + dismiss + navigate（20-30s pinchtab 调用）的时间窗口
3. **捕获 SIGABRT**：新增 `signal.signal(signal.SIGABRT, _on_signal)`，即使未来 watchdog 触发也能有日志
4. **WatchdogSec: 120→240**：增加系统级安全余量（60s 通知间隔 × 4 = 240s）

```python
# keepalive_daemon.py 关键改动
WATCHDOG_INTERVAL = 60   # 独立于心跳的看门狗通知间隔
signal.signal(signal.SIGABRT, _on_signal)  # 捕获 SIGABRT

while sleep_time > 0:
    chunk = min(30, next_heartbeat, next_watchdog, sleep_time)
    ...
    if next_watchdog <= 0:
        _sd_notify("WATCHDOG=1")   # 无条件发送
        next_watchdog = WATCHDOG_INTERVAL

# 周期开始也发送
_sd_notify("WATCHDOG=1")  # 覆盖 reload 窗口
```

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
Type=notify
WatchdogSec=240
Environment="PATH=/home/openclaw/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
ExecStartPre=/opt/aiext/bin/python3 /home/openclaw/.claude/skills/DST违章查询/cookie_persist.py --profile /home/openclaw/.pinchtab/profiles/default
ExecStart=/opt/aiext/bin/python3 /home/openclaw/.claude/skills/DST违章查询/keepalive_daemon.py \
    --company "<公司名>" \
    --project-root <项目根目录> \
    --auto-recover
Restart=always
RestartSec=10
RestartPreventExitStatus=42 43 44
# 42=登录失效(is_logged_in=0) 43=风控限流 44=已有实例运行
# 这些退出码不会触发 systemd 自动重启，避免无限重启循环

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
tail -f violation_query/data/keepalive_<公司名>.log

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
6. 日志写入 `violation_query/data/keepalive_<公司>.log`（含时间戳）

**18 分钟 sleep 期间：** 心跳以 60-120s 随机间隔执行 random scroll + DOM ping，保持页面活跃。

**标签页持久化：** Tab ID 保存在 `violation_query/data/keepalive_tab_<公司>.txt`。守护进程重启时复用已有 Tab（不重复创建），Tab 失效时自动创建新 Tab 并导航到 `platform_url`。

**自动恢复策略（`--auto-recover`）：**
- 每次保活会话最多触发 **1 次** 自动恢复
- 那次恢复内最多发送 **3 次** QR 码（应对二维码过期自动刷新）
- 3 次 QR 均超时或恢复失败 → 静默退出，等待下次查询任务自然触发重新登录
- **通知人持久化：** 查询流程登录成功后，通过 `save-notify` 命令将扫码人信息持久化到 `keepalive_notify_<公司>.json`。daemon 启动时自动读取，实现"一次指定，持续生效"——无需每次手动配置 `--notify-user`。
- **群聊 @提及保持：** 若查询 QR 是发到群聊并 @指定人，`save-notify` 会记录 `at_user_id`/`at_user_name`，保活恢复 QR 同样会在群中 @同一个人，与查询流程行为一致。

**通知人传递链路：**
```
查询登录成功
  ├─ P2P: save-notify --type user --id <open_id> --label "姓名"
  └─ 群聊: save-notify --type chat --id <chat_id> --label "群名" --at-user-id <open_id> --at-user-name "姓名"
  → violation_query/data/keepalive_notify_<公司>.json
    → keepalive daemon 启动时自动加载 (--notify-* 优先于持久化文件)
      → 自动恢复时发送 QR 到正确的人/群（群聊自动 @指定人）
        → 日志记录 "Scanned by: <姓名>"
```

**通知方式（按优先级）：**
1. `--notify-user <姓名>` / `--notify-phone <手机号>` → daemon 启动时解析 Lark ID → 发送到 bot-user P2P
2. `--notify-chat <群名>` → daemon 启动时搜索群 → 发送到群聊
3. `--lark-chat-id <raw_id>` → 直接使用原始 chat_id（向后兼容）
4. 持久化文件 `keepalive_notify_<公司>.json` → 自动读取（无 CLI 参数时），支持 `at_user_id` @提及
5. 都未配置 → QR 仅保存本地文件，不发送飞书通知

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

- **车牌自动识别：** `查粤B12345违章` → 粤→广东→导航 gab.122.gov.cn → 登录 → 选择公司 → 点击"我的主页" → 租赁车辆管理 → 输入车牌 → 生成报告
- **批量：** `查成都公司违章` → 成都→四川→导航 gab.122.gov.cn → 登录 → 选择公司 → 点击"我的主页" → 租赁车辆管理 → 车辆列表 → 逐台查 → 三重输出
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
9. **登录入口统一：** 所有省份扫码登录使用 `https://gab.122.gov.cn/m/login?t=2`。省份 URL 用于登录后导航（选择公司 → 点击"我的主页" → 租赁车辆管理）和报告展示。已登录状态下**禁止经过 gab.122.gov.cn 认证网关**，直接从省份 URL 进入
10. **车牌自动识别：** 用户输入车牌号时，提取首字符匹配省份，用于确定省份上下文
11. **文件存储：** `violation_query/screenshots/`（截图）、`violation_query/reports/`（报告）、`violation_query/data/`（SQLite 数据库）
12. **PinchTab 调试：** 若 PinchTab 命令失败，先检查 `pinchtab health`，必要时 `pinchtab daemon restart`
13. **元素定位：** 通过 `pinchtab snap` 获取 ref 编号（如 `e5`、`e12`）。**每次页面导航后 ref 编号会重新分配**，必须重新 snap 获取新 ref 后再操作。翻页相关操作优先使用 `click-page` helper（内部用 JavaScript，不依赖 ref）
14. **禁止推算：** 有违章车辆必须 `collect-violations` 逐条获取真实罚款和记分
15. **跳过无违章车辆：** `unprocessed == 0` 且 `status == "正常"` 的车辆无需进入详情页
16. **lark-cli 直调：** helper `_run()` 直接使用 PATH 上的 `lark-cli` 二进制（或通过 `shutil.which()` 解析的完整路径），Linux 上不经过任何中间包裹器。`_node_path()` 优先使用 `shutil.which('node')` 搜索 PATH。
17. **Python stdout 编码（Issue #2 三重保障修复）：** helper 启动时：(1) 强制覆盖 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`（非 setdefault）；(2) `TextIOWrapper` 先包裹原始 buffer（绕过控制台编码）；(3) `reconfigure(encoding='utf-8')` 兜底。子进程 `_run()` env 也强制覆盖。同时包裹 `sys.stdin` 确保 piped 输入为 UTF-8
18. **路径输出乱码：** 通过 `-o FILE` 将含中文路径写入 UTF-8 文件，外部脚本读取文件获取正确路径
19. **poll-login 退出码：** 0=已登录（浏览器自动检测，browser-only 模式）或飞书回复 / 1=超时或达刷新上限 / 2=用户反馈 QR 过期（legacy） / 3=浏览器检测 QR 过期。**推荐 `--browser-only` 模式**：纯浏览器检测，不调飞书 API，退出码仅有 0/1/3
20. **QR 刷新上限：** 最多自动刷新 3 次，达到上限后仅等待用户主动回复，不再刷新
21. **send-msg 响应校验：** send-msg 发送后校验响应 JSON 必须含 `ok:true` 和 `message_id`，否则非零退出。不再静默失败。
22. **poll-login --qr-sent-as：** bot 发送时传 `--qr-sent-as bot`，跳过 reply_to 匹配（bot-用户 P2P 对话中用户消息无需回复特定消息）；群聊中用户发送时保留默认 `user` 模式（需 reply_to 匹配）。
23. **bot 发个人消息用 --user-id：** bot 发给个人时使用 `send-msg --user-id <open_id>`（创建 bot-用户 P2P），**禁止使用** search-user 返回的 p2p_chat_id。
24. **逐页处理模式 + 双模式查询：** 全量查询不再先扫描所有页面，而是一页一页处理。每页提取车辆→有违章则进入详情采集→采集完翻下一页。每台车完成后保存进度（页码+序号+车牌），下次从断点继续。支持两种查询模式：`auto`（默认，连续2页清零自动终止）和 `full`（全量扫描，首次查询时使用）。模式由第五步意图识别自动判断，通过 `--query-mode` 传入批量查询脚本。
25. **智能翻页（Issue #5 ref 漂移修复）：** `click-page --target next|prev` 内部先读当前页码，转为明页号（current±1）后再走智能跳转逻辑，**不直接点击"下一页/上一页"元素**（已确认该元素在12123平台会跳错页，如 page 11→next→page 2）。`click-page --target N` 使用 JS 三策略（text/CSS/charCode）点击明页号，不依赖 ref。翻页后每次 `get-page-vehicles` 重新提取当前页车辆+页码。详情页翻页复用相同算法（`_click_detail_page`）。
26. **详情返回保留位置：** `go-back` 优先 `history.back()` 保留列表页和翻页状态；`collect-violations` 关闭弹窗而非 reload 页面，避免丢失列表位置。
27. **安全重置进度：** 使用 `reset-detail-progress` 只清空采集进度（plates+resume point），不碰全量车辆列表文件 `all_vehicles_progress.json`。禁止直接编辑/清空进度文件。
28. **终端编码：** 终端显示中文乱码是已知问题，文件内数据正常。关键输出走 `-o FILE` 写入 UTF-8 文件获取正确内容。
29. **单位用户优先：** 判断已登录状态后直接点击"我的主页"继续查询，不要自行检测登录类型。保活守护进程已持续验证登录态，信任其结果。只有实际查询遇到非单位报错时才重新登录。不要因为不确定就退出重登。
30. **弹窗遮挡（Issue #4 修复）：** `_close_popup()` 四层策略：(1) JavaScript `dispatchEvent` 点击关闭/×/取消按钮（绕过 pinchtab occlusion 检查）；(2) pinchtab click 关闭按钮（occlusion 失败时自动降级为 JS dispatchEvent）；(3) Escape 键事件（KeyEvent + activeElement）；(4) 直接 DOM 隐藏 modal/overlay/fixed 元素（最后的保险）。确保查看违章详情后能可靠关闭弹窗返回列表页。
31. **禁止随意退出登录：** 检测到已登录（有"退出"按钮、公司列表菜单）时严禁退出。只需验证省份和公司匹配当前任务即可继续。只有实际查询时遇到非单位用户报错，才重新登录。
32. **风控熔断机制：** `detect-rate-limit` 检测页面关键词（频繁/异常操作/黑名单/第三方软件等）。`open-vehicle` 连续 3 次失败也触发熔断。触发后立即终止所有查询进程，保留进度，发送飞书告警。
33. **保活生命周期：** 登录成功 → **进入我的主页确认正常** → **🔴 调用 `ensure-keepalive` 自动启动 systemd 服务**（脚本内置，自动防重复）。服务通过 `Restart=always` + `RestartPreventExitStatus=42 43 44` + `WatchdogSec=240` 保活。**退出码 42（登录失效）/43（风控）/44（重复启动）不会触发 systemd 自动重启**，避免无限重启循环。**守护进程导航到我的主页（车辆列表页 vehlist.html）进行保活**，而非省份首页。`is_logged_in=0` 或检测到异常时 daemon 以退出码 42/43 退出 → systemd 不再拉起。查询流程通过 `profile-lookup` 返回的 `keepalive_alive` 字段判断守护进程存活，存活时直接信任其登录态 → 点击"我的主页"进入查询。
34. **图片上传方式：** `lark-cli im images create --file` 无法读取含中文路径文件。改用 stdin 管道：`cat /path/to/img.png | lark-cli im images create --as bot --file "image=-" --data '{"image_type":"message"}'`
35. **总页数动态获取：** 不强制一开始获取全量页数。每页查询时通过可见分页链接获取当前 total_pages，动态更新。翻页后验证 URL 是否变化（防止假翻页）。连续 2 页检测到 0 辆车时认为到达末尾。
36. **SQLite 增量对比：** `collect-violations` 查询前先读取 SQLite 中该车牌已有记录。已存在且状态未变的跳过；状态变更的（未处理→已处理）重新查询详情并更新。
37. **详情页分页采集：** 违章数 > 10 的车辆，`collect-violations` 自动翻页采集所有详情页的未处理记录。支持 `--resume-from N` 断点续跑。`--query-mode auto`（默认）时当前详情页无未处理则跳过后续详情页；`--query-mode full` 时全量扫描所有详情页。
38. **先查人再开登录页（🔴 铁律 #9）：** 用户指定了通知对象时，必须先用 `search-user`/`search-chat`/`batch-get-id` 完成飞书 ID 查询，确认获取成功后，再导航到 12123 登录页截图二维码。二维码有效期约 5 分钟，先查人避免浪费。
39. **批量查询走 helper（🔴 铁律 #10）：** 全量/多台车查询的循环逻辑必须通过 `violation_helper.py` 已有子命令组合实现（`get-page-vehicles` → `open-vehicle` → `collect-violations` → `db-insert-violation` → `go-back` → `save-detail-progress` → `click-page`）。禁止为批量查询新写独立 Python 脚本。唯一允许的包装脚本只做循环调用，不包含任何查询逻辑。
40. **违章详情逐个查询（🔴 铁律 #11）：** 有未处理违章的车辆必须逐条点击"查看详情"获取真实罚款和记分。禁止只读列表数据不查详情、禁止仅凭列表摘要生成报告。此为最高优先级铁律。
41. **逐条落库（🔴 铁律 #12）：** `collect-violations --auto-insert` 在每条违章详情提取后立即写入 SQLite（helper 内置 upsert）。禁止攒到全部查完再批量落库、禁止用中间 JSON/JS 文件暂存。即使中途崩溃，已入库的违章数据不会丢失。
42. **双层会话隔离（实例 + 标签页）：** 多个 Claude 进程共用同一 PinchTab daemon。通过 `session_manager.py init [--instance-port <port>]` 创建会话专属标签页，设置 `VIOLATION_TAB_ID` + `VIOLATION_INSTANCE_PORT` 环境变量后，`violation_helper.py._run()` 和 `keepalive_daemon.py._run_pinchtab()` 自动对所有 PinchTab 命令注入 `--tab <id>`（标签页隔离）和 `--server http://127.0.0.1:<port>`（实例隔离）。**同一公司**共享 Instance（含 cookie/登录态），不同 Tab 独立导航互不干扰；**不同公司**使用不同 Instance（独立 Chrome Profile + 独立 cookie jar），彻底隔离 SSO 会话。
43. **多进程文件隔离：** 临时 Python 脚本路径含 `{pid}` 后缀（`batch_query_{pid}.py` 等），不同进程互不覆盖。进度文件按公司+日期隔离（`details_progress_<公司>_<日期>.json`），支持多进程并行查不同公司，且 Claude 上下文满重启后同一天自动续跑。SQLite 开启 WAL 模式支持多进程并发读写。
44. **Profile 隔离与登录复用：** 每个公司绑定一个 PinchTab Profile（`profiles` 表）→ 1:1 映射到 PinchTab Instance（独立端口）。同一公司的多个查询进程复用同一 Instance（共享 cookie/登录态），通过 `session_manager.py init --instance-port <port>` + `VIOLATION_TAB_ID` + `VIOLATION_INSTANCE_PORT` 实现双层隔离（实例级 cookie 隔离 + 标签页级导航隔离）。不同公司使用不同 Instance。每次执行前先 `instance-discover` 同步实例端口 → `profile-lookup` 查映射表，命中则跳过登录直接查询。首次登录成功后 `profile-register` 写入映射表（自动联动 `instance-discover` 绑定端口）。**🔴 `profile-register` 的公司名必须以平台公司列表页实际显示为准（铁律 #14），禁止使用用户输入或模糊匹配结果直接落库。**
45. **保活守护进程：** `keepalive_daemon.py` 通过 systemd user service 管理（`keepalive-<公司>.service`），配合 pinchtab drop-in `cookie-persist.conf` 实现四层保活架构（L0 心跳 → L1 周期 reload + 我的主页 → L2 Cookie 持久化 → L3 systemd 自动重启）。保活目标页面为我的主页（vehlist.html），能即时检测风控和登录过期。服务通过 `Restart=always` + `RestartPreventExitStatus=42 43 44` 自动恢复崩溃但不重启已登出/风控的 daemon。退出码：42=登录失效(`is_logged_in=0`)、43=风控限流、44=已有实例运行，这些退出码不会触发 systemd 重启。`loginctl enable-linger` 确保会话退出后不终止。PID 文件在 `violation_query/data/keepalive_<公司>.pid`，日志在 `violation_query/data/keepalive_<公司>.log`。Cookie 持久化由 `cookie_persist.py` 实现。启动前先检查是否已有实例运行（防止重复启动）。Claude 会话结束时守护进程不受影响，继续在后台每 18 分钟执行保活周期。**登录成功后通过 `ensure-keepalive` 自动触发启动，不再依赖模型记忆。**
46. **双模式查询策略（`--query-mode`）：** 批量查询支持 `auto`（自动检测）和 `full`（全量扫描）两种模式。行为差异仅在提前终止逻辑：`auto` 模式连续 2 页 `unprocessed` 全为 0 时安全终止；`full` 模式遍历所有页面不提前终止。两种模式在违章详情采集、逐条落库、进度保存等方面完全一致。模式选择由第五步意图识别自动判断，**无需向用户确认**——首次/首批/第一次等全量意图自动使用 `full`，其余默认 `auto`。

