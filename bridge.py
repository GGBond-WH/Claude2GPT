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

POLL_INTERVAL = 0.6      # 秒
START_TIMEOUT = 20.0     # 从发送到"看见生成开始"的上限
GEN_TIMEOUT = 900.0      # 单轮生成的上限（异步模式下不阻塞客户端，可以放宽）
SEND_BTN_TIMEOUT = 6.0   # 等发送键出现的上限
NEW_CHAT_TIMEOUT = 15.0  # 点新建会话后等页面变空的上限
LOCK_TIMEOUT = 900.0     # 等其他调用释放锁的上限（任务会排队，等待是正常的）
STABLE_ROUNDS = 2        # 停止信号消失后，还要连续几轮文本不变才算真的完事

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


# --- 操作 -------------------------------------------------------------------

def probe() -> dict:
    """诊断用：报告当前页面上各选择器的命中情况。"""
    return _run_js("""
      return {
        url: location.href,
        title: document.title,
        lang: document.documentElement.lang || null,
        matched: {
          composer: which("composer"),
          send: which("send"),
          stop: which("stop"),
          assistant: which("assistant")
        },
        assistantCount: qa("assistant").length,
        generating: generating(),
        lastAnswerPreview: (lastAnswer() || "").slice(0, 200)
      };
    """)


def new_chat() -> str:
    """开一个全新的 ChatGPT 会话，返回新的 URL。

    必须在锁内调用 —— 否则可能把另一个正在等回答的任务的会话切掉。
    """
    _run_js(
        'var b = q("new_chat");'
        'if (!b) throw new Error("找不到新建会话按钮（选择器过期，跑 probe.py）");'
        'b.click(); return 1;'
    )
    deadline = time.monotonic() + NEW_CHAT_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.4)
        st = _run_js(
            'return {url: location.href, n: qa("assistant").length,'
            ' composer: !!q("composer")};'
        )
        if st["composer"] and st["n"] == 0:
            log.info("已开新会话 %s", st["url"])
            return st["url"]
    raise BridgeError("点了新建会话，但页面没有变成空会话")


def _state() -> dict:
    return _run_js("""
      var a = answers();
      return { n: a.length, generating: generating(), last: a.length ? a[a.length-1] : null };
    """)


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
      var a = qa("assistant");
      var before = a.length;
      var beforeText = a.length ? (a[a.length-1].innerText||"").trim() : null;
      var mode = setComposer(el, D("{b64}"));
      return {{ before: before, beforeText: beforeText, mode: mode }};
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


def state() -> dict:
    """ChatGPT 标签页的实时状态。无副作用，随时可调。"""
    return _state()


def send_and_confirm(prompt: str, fresh: bool = False) -> dict:
    """发送一条消息，并阻塞到**确认 GPT 已经开始生成**为止。

    这个确认是整个设计的关键。返回即代表消息一定进去了，调用方就没有
    任何理由去"重发以防万一" —— 而重发会打断 GPT 的思考让它从头再想。

    如果发送时 GPT 正在生成，直接抛 Busy 而不是排队等 —— 排队意味着
    等它答完再插一条，同样会打乱多轮讨论的节奏。
    """
    if not prompt.strip():
        raise BridgeError("prompt 为空")

    with _exclusive():
        pre = _state()
        if pre["generating"]:
            raise Busy(
                "GPT 正在生成回答，现在不能发新消息。"
                "先用 get_gpt_answer 取回上一条的结果。"
            )
        if fresh:
            new_chat()

        sent = _send(prompt)
        before_text = sent["beforeText"]
        log.info("已发送 via=%s mode=%s，等待生成开始…", sent["via"], sent["mode"])

        t0 = time.monotonic()
        while time.monotonic() - t0 < START_TIMEOUT:
            time.sleep(POLL_INTERVAL)
            st = _state()
            if st["generating"] or st["last"] != before_text:
                log.info("已确认送达（GPT 开始生成），耗时 %.1fs",
                         time.monotonic() - t0)
                return {"delivered": True, "via": sent["via"],
                        "mode": sent["mode"]}

        raise BridgeError(
            f"发送后 {START_TIMEOUT:.0f}s 没有观察到 GPT 开始生成"
            f"（发送方式={sent['via']}）。消息可能没进去，或者选择器过期了。"
        )
