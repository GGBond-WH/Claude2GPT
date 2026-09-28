"""chatgpt.com 桥接层。

走 Apple Events -> Chromium 的 `execute javascript`，不碰 Accessibility、
不碰剪贴板、不模拟键盘。所有 JS 调用都是瞬时返回的，轮询在 Python 这边做，
这样不会撞上 AppleScript 默认 60 秒的 Apple Event 超时。

目标是 Google Chrome 里那个打开着 chatgpt.com 的标签页。刻意**按 URL 锁定
标签页**而不是用 "active tab"：Chrome 是日常浏览器，你随时会切标签，
用 active tab 会往错误的页面里打字。

在后台标签页里执行 JS 是可以的，不需要把窗口调到前台，所以不抢焦点。

传输约定:
  Python -> JS   整段 JS 源码 UTF-8 后 base64，AppleScript 里只出现
                 base64 字母表和几个符号，彻底绕开引号转义问题。
  JS -> Python   返回 base64(JSON)，同理避免 AppleScript 把换行和引号搞坏。
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

# --- 选择器集中定义 ---------------------------------------------------------
# ChatGPT 前端改版会打断这些。改版时只改这里。
# 每项是回退链，从最精确到最兜底，取第一个命中的。
# App 以 zh-CN 运行，所以 aria-label 同时留中英文两版。
TARGET_APP = "Google Chrome"   # 任何 Chromium 系都行（Edge / Brave 同样的字典）
TAB_MATCH = "chatgpt.com"      # 用来认领标签页的 URL 子串
TAB_URL = "https://chatgpt.com/"

SELECTORS = {
    "composer": [
        # 刻意不放 "form textarea" —— 未登录的营销页也有一个，会被误认成可用标签页
        "#prompt-textarea",
        'div[contenteditable="true"][id="prompt-textarea"]',
        "main form div[contenteditable='true']",
        'main div[contenteditable="true"]',
    ],
    "send": [
        '[data-testid="send-button"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
        "form button[type='submit']",
    ],
    "stop": [
        '[data-testid="stop-button"]',
        'button[aria-label*="Stop"]',
        'button[aria-label*="停止"]',
    ],
    "new_chat": [
        '[data-testid="create-new-chat-button"]',
        'a[aria-label="新聊天"]',
        'a[aria-label*="New chat"]',
    ],
    "assistant": [
        '[data-message-author-role="assistant"]',
        "div.agent-turn",
        '[data-testid^="conversation-turn"]',
    ],
}

POLL_INTERVAL = 0.6      # 秒，DOM 轮询（发送、新建会话）
API_POLL = 1.5           # 秒，后端轮询（取回答）
START_TIMEOUT = 20.0     # 从发送到"看见生成开始"的上限
GEN_TIMEOUT = 900.0      # 一轮里超过这么久没有新消息，视为中断
BUSY_WINDOW = 900.0      # 后端显示"这一轮没结束"时，最近多久内有动静才算真在进行
SEND_BTN_TIMEOUT = 6.0   # 等发送键出现的上限
NEW_CHAT_TIMEOUT = 15.0  # 点新建会话后等页面变空的上限
LOCK_TIMEOUT = 900.0     # 等其他调用释放锁的上限（任务会排队，等待是正常的）

_LOCK = Path.home() / ".gpt_bridge.lock"

# 日志走 stderr —— Claude Desktop 会把它收进
# ~/Library/Logs/Claude/mcp-server-gpt-bridge.log
# 每行带 pid，因为可能同时有多个 server 进程在驱动同一个标签页。
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [pid {os.getpid()}] %(levelname)s %(message)s",
)
log = logging.getLogger("gpt-bridge")


class BridgeError(RuntimeError):
    pass


class Busy(BridgeError):
    """GPT 正在生成 —— 此时发新消息会打断它的思考，让它从头再想一遍。"""


# --- JS 前导 ---------------------------------------------------------------
# 每次调用都会把这段 + 一个具体操作拼起来跑。
_PRELUDE = r"""
var SEL = __SELECTORS__;
function q(k){ for (var i=0;i<SEL[k].length;i++){ var e=document.querySelector(SEL[k][i]); if(e) return e; } return null; }
function qa(k){ for (var i=0;i<SEL[k].length;i++){ var e=document.querySelectorAll(SEL[k][i]); if(e.length) return Array.prototype.slice.call(e); } return []; }
function which(k){ for (var i=0;i<SEL[k].length;i++){ if(document.querySelector(SEL[k][i])) return SEL[k][i]; } return null; }
function D(b){ return new TextDecoder().decode(Uint8Array.from(atob(b), function(c){return c.charCodeAt(0);})); }
function B64(s){
  var b = new TextEncoder().encode(s), r = "", C = 0x8000;
  for (var i=0;i<b.length;i+=C) r += String.fromCharCode.apply(null, b.subarray(i, i+C));
  return btoa(r);
}
function generating(){ return !!q("stop"); }

// ---- 后端读取 ----
// 不读 DOM 的原因：后台标签页里 ChatGPT 会停止渲染对话（实测一个从未显示过的
// 标签页，生成完毕 5 分钟后 DOM 里仍然一个轮次都没有），而停止按钮的状态照常
// 更新 —— 于是"停止按钮消失 + 文本不再变化"会把渲染到一半的文本当成完整回答。
// 这里改走页面自己加载对话时用的同一个接口，拿到的是权威的原始数据。
function api(path){
  // 登录态令牌只在这个函数里用，绝不返回，所以不会离开浏览器
  var s = new XMLHttpRequest(); s.open("GET", "/api/auth/session", false); s.send();
  var tok = null; try { tok = JSON.parse(s.responseText).accessToken; } catch (e) {}
  if (!tok) throw new Error("拿不到 ChatGPT 登录态（可能已退出登录）");
  var x = new XMLHttpRequest(); x.open("GET", path, false);
  x.setRequestHeader("Authorization", "Bearer " + tok); x.send();
  if (x.status !== 200) throw new Error("ChatGPT 后端返回 HTTP " + x.status);
  return JSON.parse(x.responseText);
}
function convId(){ var m = location.pathname.match(/\/c\/([0-9a-f-]+)/); return m ? m[1] : null; }
function branch(d){
  // 从 current_node 沿 parent 回溯，得到当前显示的这条分支
  var chain = [], id = d.current_node;
  while (id && d.mapping[id]) { var n = d.mapping[id]; if (n.message) chain.push(n.message); id = n.parent; }
  return chain.reverse();
}
function role(m){ return (m.author && m.author.role) || ""; }
function msgText(m){
  var p = (m.content && m.content.parts) || [];
  return p.filter(function(x){ return typeof x === "string"; }).join("");
}
function isProse(m){
  // 一轮回答由很多条消息组成：开场白、搜索调用、工具结果、推理、正文……
  // 只有发给用户看的 assistant 文本才算回答
  var ct = m.content && m.content.content_type;
  return role(m) === "assistant" && (ct === "text" || ct === "multimodal_text")
      && (!m.recipient || m.recipient === "all") && msgText(m).trim().length > 0;
}
function cleanUrl(u){
  return (u || "").replace(/([?&])utm_source=chatgpt\.com(&?)/, function(_, a, b){ return b ? a : ""; });
}
function prose(m, refs){
  // 引用在正文里是私有区字符包起来的标记（U+E200 … U+E201），
  // content_references 给出每个标记对应的来源。换成 [n]，来源统一列在末尾。
  var t = msgText(m);
  ((m.metadata && m.metadata.content_references) || []).forEach(function(r){
    if (!r.matched_text) return;
    var rep = "", items = (r.items || []).filter(function(it){ return it && it.url; });
    if (items.length) {
      rep = items.map(function(it){
        var u = cleanUrl(it.url);
        if (!(u in refs.n)) { refs.list.push({title: it.title || "", url: u}); refs.n[u] = refs.list.length; }
        return "[" + refs.n[u] + "]";
      }).join("");
    } else if (r.alt && !/\]\(https?:/.test(r.alt)) {
      rep = r.alt;                       // 实体一类的标记，alt 就是显示文字
    }
    t = t.split(r.matched_text).join(rep);
  });
  return t.replace(/[^]*/g, "");   // 兜底：残留标记一律去掉
}
function norm60(s){ return (s || "").replace(/\s+/g, "").slice(0, 60); }
function idxOf(chain, id){ for (var i = chain.length - 1; i >= 0; i--) if (chain[i].id === id) return i; return -1; }
function anchorIn(chain, id, prefix, afterId){
  // 找到这个 job 对应的那条提问。优先按消息 id，找不到再按提问文本。
  // afterId：只接受出现在它之后的提问 —— 防止连发两条相同的话时认到旧的那条
  var i, floor = afterId ? idxOf(chain, afterId) : -1;
  if (id) { i = idxOf(chain, id); if (i > floor) return {idx: i, by: "id"}; }
  var want = norm60(prefix);
  if (want) for (i = chain.length - 1; i > floor; i--)
    if (role(chain[i]) === "user" && norm60(msgText(chain[i])) === want) return {idx: i, by: "text"};
  return null;
}
function lastUserIdx(chain){ for (var i = chain.length - 1; i >= 0; i--) if (role(chain[i]) === "user") return i; return -1; }
function describeTurn(chain, idx){
  var turn = [];
  for (var i = idx + 1; i < chain.length && role(chain[i]) !== "user"; i++) turn.push(chain[i]);
  var last = turn.length ? turn[turn.length - 1] : null, ask = chain[idx];
  var refs = {n: {}, list: []};
  var text = turn.filter(isProse).map(function(m){ return prose(m, refs); }).join("\n\n").trim();
  if (refs.list.length) text += "\n\n来源：\n" + refs.list.map(function(r, k){
    return "[" + (k + 1) + "] " + (r.title ? r.title + " — " : "") + r.url; }).join("\n");
  var now = Date.now() / 1000, ct = last && last.content && last.content.content_type;
  var fin = last && last.metadata && last.metadata.finish_details;
  return {
    anchorId: ask.id, anchorText: msgText(ask).replace(/\s+/g, " ").slice(0, 40),
    // 权威的完成信号：这一轮最后一条是 end_turn=true 的 assistant 消息
    complete: !!(last && role(last) === "assistant" && last.end_turn === true),
    interrupted: !!(fin && fin.type === "interrupted"),
    lastRole: last ? role(last) : null, lastStatus: last ? last.status : null,
    phase: !last ? "等待回应"
         : (role(last) === "tool" || (last.recipient && last.recipient !== "all")) ? "正在调用工具（多半是联网搜索）"
         : (ct === "thoughts" || ct === "reasoning_recap") ? "正在推理" : "正在输出",
    nMsgs: turn.length,
    sinceAsk: ask.create_time ? now - ask.create_time : null,
    sinceLast: (last || ask).create_time ? now - (last || ask).create_time : null,
    duration: (last && last.create_time && ask.create_time) ? last.create_time - ask.create_time : null,
    text: text
  };
}
function loggedOut(){
  return Array.prototype.slice.call(document.querySelectorAll("button,a"))
    .some(function(e){ return /^(log ?in|sign ?up|\u767b\u5f55|\u514d\u8d39\u6ce8\u518c)$/i.test((e.innerText||"").trim()); });
}
function answers(){ return qa("assistant").map(function(e){ return (e.innerText||"").trim(); }); }
function lastAnswer(){ var a = answers(); return a.length ? a[a.length-1] : null; }
function setComposer(el, text){
  el.focus();
  var tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "INPUT") {
    var proto = tag === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
    el.dispatchEvent(new Event("input", {bubbles:true}));
    return "textarea";
  }
  var sel = window.getSelection(), range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges(); sel.addRange(range);
  document.execCommand("insertText", false, text);
  el.dispatchEvent(new InputEvent("input", {bubbles:true}));
  return "contenteditable";
}
"""


def _wrap(body: str) -> str:
    """把操作体包成带 try/catch、返回 base64(JSON) 的 IIFE。"""
    prelude = _PRELUDE.replace("__SELECTORS__", json.dumps(SELECTORS, ensure_ascii=False))
    return (
        "(function(){"
        + prelude
        + "try{ var __r = (function(){" + body + "})();"
        + ' return B64(JSON.stringify({ok:true, data:__r}));'
        + "}catch(e){ return B64(JSON.stringify({ok:false, error:String((e&&e.stack)||e)})); }"
        + "})()"
    )


def _osascript(script: str, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["osascript"], input=script, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"osascript 超时（{timeout}s）") from exc


def _payload(body: str) -> str:
    """把操作体编成 AppleScript 字符串里安全的 eval 表达式。"""
    js_b64 = base64.b64encode(_wrap(body).encode("utf-8")).decode("ascii")
    # 只有 base64 字母表和 JS 标点，没有双引号，所以不需要任何转义。
    return (
        "eval(new TextDecoder().decode("
        f"Uint8Array.from(atob('{js_b64}'),function(c){{return c.charCodeAt(0);}})))"
    )


def _decode(proc: subprocess.CompletedProcess) -> object:
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "已关闭" in err or "turned off" in err.lower():
            raise BridgeError(
                f"{TARGET_APP} 未开启「允许 Apple 事件中的 JavaScript」。"
                "菜单栏：查看 > 开发者 > 允许 Apple 事件中的 JavaScript"
                "（注意不是设置页里的 JavaScript 内容设置，是菜单栏那个）。"
            )
        raise BridgeError(f"AppleScript 失败: {err or proc.returncode}")
    out = (proc.stdout or "").strip()
    if not out:
        raise BridgeError("JS 无返回值")
    try:
        result = json.loads(base64.b64decode(out).decode("utf-8"))
    except Exception as exc:
        raise BridgeError(f"无法解析 JS 返回值: {out[:200]!r}") from exc
    if not result.get("ok"):
        raise BridgeError(f"JS 执行出错: {result.get('error')}")
    return result["data"]


def _locate() -> list[tuple[int, int, str]]:
    """列出所有 chatgpt.com 标签页的 (窗口序号, 标签序号, 标签id)。

    序号是位置性的 —— 关掉一个标签页，它后面所有标签页的序号都会前移。
    所以对外认身份一律用 id，序号只在当次调用里立即使用。
    """
    script = f'''
tell application "{TARGET_APP}"
  set acc to ""
  repeat with w from 1 to count of windows
    repeat with t from 1 to count of tabs of window w
      if (URL of tab t of window w) contains "{TAB_MATCH}" then
        set acc to acc & w & "," & t & "," & (id of tab t of window w) & ";"
      end if
    end repeat
  end repeat
  return acc
end tell
'''
    proc = _osascript(script, 30.0)
    if proc.returncode != 0:
        raise BridgeError(f"枚举标签页失败: {(proc.stderr or '').strip()}")
    out = []
    for chunk in (proc.stdout or "").strip().split(";"):
        if chunk:
            w, t, tid = chunk.split(",", 2)
            out.append((int(w), int(t), tid))
    return out


def _exec_on(w: int, t: int, body: str, timeout: float = 30.0):
    script = (
        f'tell application "{TARGET_APP}" to return '
        f'execute tab {t} of window {w} javascript "{_payload(body)}"'
    )
    return _decode(_osascript(script, timeout))


_TAB_ID: str | None = None


def _find_by_id(tab_id: str) -> tuple[int, int] | None:
    """按 id 找回标签页当前的位置。找不到说明它被关了。"""
    for w, t, tid in _locate():
        if tid == tab_id:
            return (w, t)
    return None


def _resolve(force: bool = False) -> tuple[int, int]:
    """挑一个真正能用的标签页，返回它当前的 (窗口序号, 标签序号)。

    刻意不是"第一个 chatgpt.com 标签页" —— 未登录的营销页 URL 也是 chatgpt.com，
    残留的僵尸标签页会把请求吞掉。而且不能只看"有没有输入框"：营销页也有一个
    textarea，会误判。这里要求既有输入框、又没有登录按钮。

    认下来之后缓存的是 **tab id 而不是序号**。序号是位置性的：用户关掉前面
    任何一个标签页，后面的序号全部前移，缓存的序号就会静默指向另一个页面 ——
    于是消息发到 A 页、轮询却在 B 页，表现为"发出去了但永远等不到回答"。
    """
    global _TAB_ID

    if _TAB_ID and not force:
        pos = _find_by_id(_TAB_ID)
        if pos:
            return pos
        _TAB_ID = None  # 标签页被关了，重新找

    rejected = []
    candidates = _locate()
    if not candidates:
        raise BridgeError(
            f"{TARGET_APP} 里没有 {TAB_MATCH} 标签页。开一个并登录后重试。"
        )

    for w, t, tid in candidates:
        try:
            info = _exec_on(
                w, t,
                'return {composer: !!q("composer"), out: loggedOut(), title: document.title};',
            )
        except BridgeError:
            continue
        if info["composer"] and not info["out"]:
            _TAB_ID = tid
            log.info("认领标签页 窗口%d 序号%d id=%s", w, t, tid)
            return (w, t)
        rejected.append(f"窗口{w}标签{t}[{'未登录' if info['out'] else '无输入框'}]")

    raise BridgeError(
        f"找到 {len(candidates)} 个 {TAB_MATCH} 标签页，但没有一个可用"
        f"（{', '.join(rejected)}）。请在其中一个标签页里登录 ChatGPT 后重试。"
    )


def _run_js(body: str, timeout: float = 30.0):
    """在可用的 chatgpt.com 标签页上执行 JS。标签页移动过就重新解析一次。"""
    w, t = _resolve()
    try:
        return _exec_on(w, t, body, timeout)
    except BridgeError:
        w, t = _resolve(force=True)
        return _exec_on(w, t, body, timeout)


def _js_call(body: str, **args):
    """带参数执行 JS。参数以 JSON 形式注入为 ARGS，JS 里用 ARGS.xxx 取。"""
    return _run_js(f"var ARGS = {json.dumps(args, ensure_ascii=False)};\n" + body)


# --- 操作 -------------------------------------------------------------------

def probe() -> dict:
    """诊断用：报告页面上关键元素的命中情况，以及后端能否读到当前会话。"""
    return _run_js("""
      var backend = null, cid = convId();
      if (cid) {
        try { var c = branch(api("/backend-api/conversation/" + cid));
              backend = {ok: true, messages: c.length}; }
        catch (e) { backend = {ok: false, error: String(e.message || e)}; }
      }
      return {
        url: location.href, title: document.title, visibility: document.visibilityState,
        loggedOut: loggedOut(),
        matched: { composer: which("composer"), new_chat: which("new_chat"),
                   send: which("send"), stop: which("stop") },
        backend: backend
      };
    """)


def new_chat() -> str:
    """开一个全新的 ChatGPT 会话，返回新的 URL。

    必须在锁内调用 —— 否则可能把另一个正在等回答的任务的会话切掉。

    判据是 URL 里不再有 /c/<会话id>，而不是"页面上回答条数为 0"：后台标签页
    根本不渲染对话，条数恒为 0，旧判据会在新会话还没打开时就放行，
    把消息发进旧会话里。
    """
    _run_js(
        'var b = q("new_chat");'
        'if (!b) throw new Error("找不到新建会话按钮（选择器过期，跑 probe.py）");'
        'b.click(); return 1;'
    )
    deadline = time.monotonic() + NEW_CHAT_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.4)
        st = _run_js('return {url: location.href, cid: convId(), composer: !!q("composer")};')
        if st["composer"] and not st["cid"]:
            log.info("已开新会话 %s", st["url"])
            return st["url"]
    raise BridgeError("点了新建会话，但页面没有切到新会话")


def _send(prompt: str) -> dict:
    """把 prompt 填进输入框并发出去。

    分两步做是有原因的：ChatGPT 的发送键在输入框为空时根本不存在（那个位置
    显示的是语音按钮），填入文本后由 React 异步换上。所以不能在填入的同一个
    JS tick 里找发送键 —— 那时它还没渲染出来，会静默落到回车回退分支。
    """
    b64 = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
    info = _run_js(f'''
      var el = q("composer");
      if (!el) throw new Error("找不到输入框（选择器全部落空，跑 probe.py 重新校准）");
      return {{ mode: setComposer(el, D("{b64}")) }};
    ''')

    deadline = time.monotonic() + SEND_BTN_TIMEOUT
    while time.monotonic() < deadline:
        clicked = _run_js(
            'var b = q("send");'
            'if (!b || b.disabled) return {done:false};'
            'b.click(); return {done:true};'
        )
        if clicked["done"]:
            return {**info, "via": "button"}
        time.sleep(0.25)

    # 回退：直接在输入框上敲回车
    _run_js(
        'var el = q("composer"); el.focus();'
        'var ev = {key:"Enter", code:"Enter", keyCode:13, which:13,'
        ' bubbles:true, cancelable:true};'
        'el.dispatchEvent(new KeyboardEvent("keydown", ev));'
        'el.dispatchEvent(new KeyboardEvent("keyup", ev));'
        'return 1;'
    )
    return {**info, "via": "enter"}


@contextmanager
def _exclusive():
    """跨进程独占 ChatGPT 标签页。"""
    _LOCK.touch(exist_ok=True)
    with open(_LOCK, "r+") as lock:
        t0 = time.monotonic()
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("另一个调用正占着锁，等待中…")
            while True:
                if time.monotonic() - t0 > LOCK_TIMEOUT:
                    raise BridgeError(
                        f"等锁超过 {LOCK_TIMEOUT:.0f}s。可能有卡死的进程占着 "
                        f"{_LOCK}。用 `pkill -f 'python.*server[.]py'` 清掉后重试。"
                    )
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_turn(cid: str | None = None, user_msg_id: str | None = None,
              prompt: str | None = None, after_id: str | None = None) -> dict:
    """从 ChatGPT 后端读取一轮问答。无副作用，随时可调。

    定位：给了 user_msg_id 就按 id 找那条提问；否则按 prompt 文本找；
    都没给就取当前会话的最后一轮。cid 不给就用标签页当前打开的会话 ——
    给了的话，即使标签页已经切到别的会话也能读。

    返回 found / complete / text / phase 等。complete 以后端的 end_turn 为准。
    """
    return _js_call('''
      var cid = ARGS.cid || convId();
      if (!cid) return {found: false, why: "标签页当前不在任何会话里"};
      var chain = branch(api("/backend-api/conversation/" + cid));
      var a = (ARGS.id || ARGS.prompt) ? anchorIn(chain, ARGS.id, ARGS.prompt, ARGS.after) : null;
      if (!a) {
        if (ARGS.id || ARGS.prompt) return {found: false, cid: cid, why: "在会话里找不到这条提问"};
        var li = lastUserIdx(chain);
        if (li < 0) return {found: false, cid: cid, why: "会话里还没有提问"};
        a = {idx: li, by: "last"};
      }
      var t = describeTurn(chain, a.idx);
      t.found = true; t.cid = cid; t.by = a.by;
      return t;
    ''', cid=cid, id=user_msg_id, prompt=(prompt or "")[:300], after=after_id)


def tail_state() -> dict:
    """当前会话最后一轮是否还在进行。发送前的 busy 检查用。

    停止按钮 **或** 后端显示这一轮没结束，任一成立就算忙。只看停止按钮不够：
    GPT 在搜索和推理的间隙，页面状态并不总是可靠，尤其是后台标签页。
    """
    t = _js_call('''
      var stop = generating(), cid = convId();
      if (!cid) return {stop: stop, found: false};
      var chain = branch(api("/backend-api/conversation/" + cid));
      var li = lastUserIdx(chain);
      if (li < 0) return {stop: stop, found: false};
      var t = describeTurn(chain, li); t.stop = stop; t.found = true; t.text = null;
      return t;
    ''')
    mid = (t.get("found") and not t["complete"] and not t["interrupted"]
           and (t["sinceLast"] is None or t["sinceLast"] < BUSY_WINDOW))
    t["busy"] = bool(t["stop"] or mid)
    t["why"] = ("页面显示正在生成" if t["stop"]
                else f"后端显示这一轮还没结束（{t['phase']}）" if mid else None)
    return t


def send_and_confirm(prompt: str, fresh: bool = False) -> dict:
    """发送一条消息，并阻塞到**确认后端已经收到这条提问**为止。

    返回即代表消息一定进去了，调用方就没有任何理由去"重发以防万一" ——
    而重发会打断 GPT 的思考让它从头再想。返回值里带着这条提问的消息 id，
    之后取回答时按 id 精确定位，不受页面渲染和虚拟化影响。

    如果发送时上一轮还没结束，直接抛 Busy 而不是排队等。
    """
    if not prompt.strip():
        raise BridgeError("prompt 为空")

    with _exclusive():
        tail = tail_state()
        if tail["busy"]:
            raise Busy(
                f"GPT 还在回答上一条（{tail['why']}），现在不能发新消息。"
                "先用 get_gpt_answer 或 read_last_gpt_answer 取回上一条的结果。"
            )
        after = None if fresh else (tail.get("anchorId") if tail.get("found") else None)
        if fresh:
            new_chat()

        sent = _send(prompt)
        log.info("已发送 via=%s mode=%s，等待后端确认…", sent["via"], sent["mode"])

        t0, saw_stop = time.monotonic(), False
        while time.monotonic() - t0 < START_TIMEOUT:
            time.sleep(POLL_INTERVAL)
            try:
                r = read_turn(prompt=prompt, after_id=after)
            except BridgeError:
                r = {}
            if r.get("found"):
                log.info("已确认送达（后端收到提问 %s），耗时 %.1fs",
                         r["anchorId"][:8], time.monotonic() - t0)
                return {"delivered": True, "cid": r["cid"], "user_msg_id": r["anchorId"],
                        "via": sent["via"]}
            saw_stop = saw_stop or bool(_run_js("return generating();"))

        if saw_stop:
            # 页面已经在生成，但后端里还没查到这条提问。消息肯定进去了，
            # 锚点留空，之后取回答时按提问文本补上。
            log.info("已确认送达（页面在生成），后端锚点稍后按文本补")
            return {"delivered": True, "cid": None, "user_msg_id": None, "via": sent["via"]}

        raise BridgeError(
            f"发送后 {START_TIMEOUT:.0f}s 既没在后端查到这条提问、也没看到 GPT 开始生成"
            f"（发送方式={sent['via']}）。消息可能没进去，或者选择器过期了。"
        )
