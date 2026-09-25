# gpt-bridge

给 Claude 装一个 `ask_gpt(prompt)` 工具，驱动 Chrome 里的 chatgpt.com，不用 OpenAI API。

```
Claude Code
  └─ MCP (stdio)
       └─ server.py
            └─ bridge.py
                 └─ osascript / Apple Events
                      └─ Google Chrome  ← execute javascript
                           └─ chatgpt.com 标签页（按 URL 锁定）
                                └─ DOM
```

## 为什么是这条路

Chromium 的 AppleScript 字典带 `execute javascript`。所以不需要 Accessibility API、
不需要 Hammerspoon、不需要模拟键盘、不需要剪贴板。"GPT 答完了没有" 和
"这一轮回答的完整文本" 都是确定性的 DOM 查询，不是猜。

在后台标签页里执行 JS 是可以的，不需要把窗口调到前台，**所以不抢焦点** ——
跑的时候你可以继续用电脑。

### 为什么不是 ChatGPT.app

一开始走的是 ChatGPT.app（它同样是 Chromium 外壳，实测 26.901.20858 /
Codex Framework 152，字典里也有 `execute javascript`）。放弃的原因是：

**它的主对话窗口不在 AppleScript 的窗口集合里。** App 在跑、frontmost 为 true，
`count of windows` 依然是 0；只有 `make new window` 造出来的浏览器窗口才可脚本化，
而那个窗口是未登录的 —— 主界面的登录态走 Keychain 里的 OAuth token，
不是浏览器 cookie（三个 profile 的 Cookies 库里都查不到 session cookie）。

顺带一提，Accessibility 那条路在本机也是被拒的（`osascript` 拿不到辅助访问，
报 -25211）—— macOS 把 TCC 权限算在"责任进程"头上，子进程继承的是父进程的归属。
Apple Events 路线完全绕开了这个问题。

## 前置条件（只做一次）

**一、Chrome 开启 Apple 事件 JS。** 菜单：**查看 → 开发者 → 允许 Apple 事件中的
JavaScript**。不用重启 Chrome。

**二、Chrome 里开一个 chatgpt.com 标签页并保持登录。** 标签页按 URL 认领，
放在哪个窗口、第几个位置都行，也可以在后台。没有的话 `bridge.ensure_tab()` 会开一个。

### 开关的代价

开启后，**任何拿到对 Chrome 的 Automation 权限的本地进程，都能在你所有登录态的
网站里执行任意 JS** —— 不只是 chatgpt.com，是 Chrome 里的每一个站点。
这比只给 ChatGPT 开口子的范围大。不用的时候在同一个菜单里关掉。

## 装

### Claude Desktop（当前使用的）

写进 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
"mcpServers": {
  "gpt-bridge": {
    "command": "$PROJECT_DIR/.venv/bin/python",
    "args": ["$PROJECT_DIR/server.py"]
  }
}
```

venv 用这两条建（只需一次）：

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python "mcp>=2.0,<3"
```

**不要在这里用 `uv run --script`。** 实测 Desktop 会 attach 失败弹
"could not attach gpt-bridge"，日志里能看到 initialize 回了 **55 秒**
（`~/Library/Logs/Claude/mcp-server-gpt-bridge.log`）。uv 本身只要 0.3s，
慢的是 Desktop 快速重试两次时 uv 的环境锁争用 —— 第一个进程没退干净，
第二个卡在锁上。直接指向 venv 解释器就没有这一层，冷启动恒定 ~0.3s。

**路径必须是绝对路径。** Claude Desktop 是 GUI 程序，PATH 是 launchd 的
默认值，没有 `~/.local/bin`，写裸命令会 ENOENT。改完重启 Desktop 生效。

首次调用时 macOS 会弹「Claude 想要控制 Google Chrome」的授权框，点允许。
这个授权是按**责任进程**归属的，所以 Claude Code 批过不代表 Desktop 也批过，
两边各弹一次。

### Claude Code（当前已停用）

**不要和 Desktop 同时开。** 两个客户端各起一个 server 进程、都驱动同一个
Chrome 标签页；虽然有跨进程文件锁兜底，但会互相排队拖慢，排查问题时也
分不清是哪个进程发的。

原来的注册文件备份在 `backups/*-claude-code-mcp/`。要切回 Claude Code：
先把 Desktop 配置里的 `mcpServers.gpt-bridge` 删掉，再还原那两个文件：

```bash
bk=$(ls -d backups/*claude-code-mcp | tail -1)
cp "$bk/.mcp.json" . && mkdir -p .claude && cp "$bk/settings.local.json" .claude/
```

### 排查

server 的日志走 stderr，Desktop 收进
`~/Library/Logs/Claude/mcp-server-gpt-bridge.log`，每行带 pid。
发送、认领标签页、每次轮询的 `n / generating / stable` 都在里面。

卡住时先看有没有僵尸进程占着锁：

```bash
lsof ~/.gpt_bridge.lock
pkill -f 'python.*server[.]py'
```

锁有 900 秒超时，超时会给出明确报错而不是无限挂起。

## 已实测确认的选择器

2026-09-04 在 chatgpt.com（zh-CN 界面）校准，四类全部由回退链第一条命中：

| 用途 | 选择器 | aria-label |
|---|---|---|
| 输入框 | `#prompt-textarea`（contenteditable div） | — |
| 发送 | `[data-testid="send-button"]` | 发送提示词 |
| 停止 | `[data-testid="stop-button"]` | 停止回答 |
| 回答 | `[data-message-author-role="assistant"]` | — |

### 标签页认领用 id，不能用序号

AppleScript 的 tab 序号是**位置性**的：关掉前面任何一个标签页，后面所有
标签页的序号都会前移。缓存序号会导致消息发到 A 页、轮询却在 B 页 ——
表现是"消息确实发出去了、GPT 也答了，但工具报『20s 没有观察到生成开始』"。

而且这种情况不会抛错（B 页也是合法的 chatgpt.com 页面），所以不会触发
重新解析。必须缓存 `id of tab`，每次调用时按 id 找回当前位置。

### 校准时踩到的两个坑

**一、发送键在输入框为空时根本不存在。** 那个位置显示的是「启动语音功能」，
填入文本后由 React 异步换成发送键。所以 `_send` 必须分两步：先填入，
再轮询等发送键出现，然后点。在同一个 JS tick 里找发送键一定找不到，
会静默落到回车回退分支。

**二、流式输出中 React 会瞬时卸载再挂载回答节点。** 实测在 2.7s 那一刻
`[data-message-author-role="assistant"]` 的数量掉到 0、文本长度掉到 0，
0.6s 后恢复。完成判定的三道守卫（generating 时 continue、`n <= before` 时
continue、`and st["last"]` 挡空串）少任何一道都会读到半截或空回答。

## 用之前先校准

```bash
uv run --script probe.py
```

它不发消息，只报告 `bridge.py` 里四类选择器（输入框 / 发送 / 停止 / 回答）
在当前页面的命中情况。全部命中才能用 `ask_gpt`。

ChatGPT 前端改版一定会打断这些选择器 —— 那时 `probe.py` 会指出哪一条落空了，
改 `bridge.py` 顶部的 `SELECTORS` 即可，那是唯一需要动的地方。

## 讨论协议怎么装

协议正文只有一份，在 `server.py` 的 `_protocol()` 里，三处共用：

| 装法 | 需要做什么 | 生效范围 |
|---|---|---|
| **server instructions**（默认） | 什么都不用做 | 每个连上 gpt-bridge 的对话 |
| MCP prompt `discuss` | 输入框 `+` → gpt-bridge → discuss | 单次 |
| Project 自定义指令 | 贴 `discuss-instructions.md` | 该 Project 内的对话 |

**默认走第一种。** MCP server 可以在 `initialize` 响应里声明 `instructions`，
客户端连接时自动注入 —— 不用贴、不用建 Project、每个对话都自带。

协议里加了一句作用域限定：单次提问（"问一下 GPT X 是什么"）直接用工具，
不走完整流程；只有用户要求**讨论/辩论/让 GPT 一起看**时才启动多轮协议。

### 踩过的坑

MCP 的 **tool 自动可用，prompt 必须主动选**。早期版本把协议只放在 prompt 里，
结果用户在普通对话里说"问一下 GPT…"时协议根本没加载（日志里 `prompts/get`
计数一直是 0），Claude 自然就讨论一轮回来问"要不要继续"。
放进 server instructions 才解决了这个加载问题。

另一个症状是**反复开新会话**：日志里出现过 3 次 `ask_gpt` 只有 1 次
`continue_gpt`，每开一次新会话就把讨论上下文全扔了，多轮讨论根本不可能发生。
协议里现在明写"推进一律用 continue_gpt"。

### 协议的几条硬约束

- **不许问用户许可** —— "要不要继续/追问/展开/查一下"全禁
- **停止的硬前提**：任何你认为 GPT 说错、遗漏、自相矛盾的地方，必须先
  `continue_gpt` 发给它、看它回应才算数。只报告给用户不发回 GPT 是最严重的
  失败方式；没经过 GPT 回应的异议不构成"剩余分歧"，不能作为停止理由
- **自相矛盾优先打** —— 信息量最大的一次交互
- **收尾必须共识确认** —— 综合稿发回给 GPT 逐条表态，没过这步的结论
  不能写进"双方共识"
- **GPT 给的文献/数字/年份单列标注「未核实」** —— 编造精确引用是它的
  常见故障模式

## 送达确认：为什么不会重发

早期版本出过一个坏 bug：GPT 思考久了，Claude 判断"上一条可能没送达"就重发，
而重发**打断了 GPT 正在进行的思考**，让它从头再想一遍。

日志显示根因不在判断力，在架构：任务状态放在 server 进程内存里，而客户端会
因为超时/重连**重启 server 进程**（实测 pid 从 50620 变成 51202）。进程一换，
`get_gpt_answer` 就查不到 job_id、报错，Claude 自然以为没送达。

现在三道保险：

1. **发送即确认。** `ask_gpt` / `continue_gpt` 会阻塞到观察到 GPT 真的开始
   生成才返回，返回文案里明确写着"已确认送达，不要重发"。
2. **状态落盘。** 任务记录写在 `~/.gpt_bridge/jobs/<id>.json`，进程重启后
   照样查得到；取回答看的是 ChatGPT 页面的实时状态，不依赖任何后台线程。
3. **正在生成时拒绝发送。** `continue_gpt` 发现页面在生成会直接拒绝并提示
   先取回上一条 —— 从结构上杜绝打断。

## 关于"独立第二意见"的一个前提

`ask_gpt` 会真的开一个空会话（实测 `new_chat()` 后 URL 回到
`chatgpt.com/`、`assistantCount=0`、`allTurns=0`）。

但**ChatGPT 账号级的「记忆」是跨会话的**。实测：在会话 A 里让 GPT 记住
一个暗号，`ask_gpt` 开全新会话后再问，它照样答得出来。

所以要真正独立的第二意见，需要在 ChatGPT 的
设置 → 个性化 → 记忆 里关掉引用记忆/聊天记录。这是你的账号设置，
桥接不碰它。开着也能用，只是"独立"要打折扣 —— debate 流程里已经
提示 Claude 留意 GPT 引用未提及的信息。

## 边界与注意

- 消息发到那个 chatgpt.com 标签页**当前打开的会话**，所以连续调用是有
  上下文的（辩论时正需要）。要干净的第二意见，先手动开一个新会话。
- 任务存在 server 进程内存里，重启客户端会丢掉尚未取回的 job_id。
- **不要同时在 Claude Desktop 和 Claude Code 里注册。** 两个客户端各起一个
  server 进程、都驱动同一个标签页；虽然有跨进程文件锁兜底，但会互相排队
  拖慢，排查时也分不清是谁发的。
- 标签页按 URL 锁定，不是用 `active tab` —— Chrome 是日常浏览器，
  用 active tab 会在你切标签时往错误的页面里打字。
- 并发调用会在文件锁（`~/.gpt_bridge.lock`）上串行，两个请求同时往
  输入框塞字会互相踩。
- 标签页认领条件是「有输入框 **且** 没有登录按钮」。只看输入框会误判 ——
  未登录的营销页也有一个 `form textarea`。
- 失败一律抛明确错误，绝不返回空字符串 —— 否则 Claude 会拿着空回答
  一本正经地开始"反驳 GPT 的观点"。

## 完成检测怎么做的

主信号是**停止按钮的存在性**：生成中发送键变 stop，结束后变回来。
文本连续两轮不变只当兜底，外加硬超时。

刻意没有用「文本 N 秒不变」当主信号 —— GPT 思考时会卡顿，那样会把中途的
停顿误判成答完了，拿到半截回答。
