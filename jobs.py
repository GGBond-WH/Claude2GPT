"""任务状态 —— 落盘，不放进程内存；回答每次都从后端重读。

两个历史教训：

一、状态不能放进程内存。客户端会因为超时、重连重启 server 进程，任务一丢，
    get_gpt_answer 就查不到 job_id，Claude 以为消息没送达于是重发 —— 重发会
    打断 GPT 正在进行的思考。所以任务记录写到 ~/.gpt_bridge/jobs/<id>.json。

二、回答不能"完成一次就冻结"。早期版本在第一次判定完成后把文本存下来，之后
    每次都返回这份存档 —— 可那次判定本身可能就是错的（读到半截），于是半截
    文本被永久冻结，重取多少次都一样。现在每个 job 锚定到它那条提问的消息 id，
    get_gpt_answer 每次都从后端重读这一轮，存档只在后端读不到时兜底。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import bridge

log = logging.getLogger("gpt-bridge.jobs")

STORE = Path.home() / ".gpt_bridge" / "jobs"
# get_gpt_answer 单次最多等多久。MCP 客户端的请求超时是 60s，
# 实测 wait_seconds=60 会报 "Request timed out"，45/50/55 正常 —— 留足余量。
MAX_WAIT = 45.0
KEEP = 200               # 保留最近多少条记录


@dataclass
class Job:
    id: str
    prompt: str
    fresh: bool
    sent_at: float           # time.time()，跨进程可比
    delivered: bool
    status: str              # generating | done | error
    answer: str | None = None
    error: str | None = None
    elapsed: float | None = None
    cid: str | None = None           # 所在会话
    user_msg_id: str | None = None   # 这条提问的消息 id —— 重读回答的锚点
    after_id: str | None = None      # 发送前最后一条提问的 id —— 按文本补锚点时只认它之后的
    final: bool = False              # 后端已确认答完（end_turn=True），之后不用再读后端
    asked: str | None = None         # 提问的前 40 字，回显用

    def thinking_for(self) -> float:
        return max(0.0, time.time() - self.sent_at)


@dataclass
class Reading:
    status: str              # done | generating | error
    text: str | None = None
    elapsed: float | None = None
    phase: str | None = None
    note: str | None = None
    asked: str | None = None  # 这是对哪条提问的回答（前 40 字）


def _path(job_id: str) -> Path:
    return STORE / f"{job_id}.json"


def _save(job: Job) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = _path(job.id).with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(job), ensure_ascii=False))
    tmp.replace(_path(job.id))       # 原子替换，避免读到写了一半的文件


def load(job_id: str) -> Job | None:
    try:
        raw = json.loads(_path(job_id).read_text())
    except (OSError, ValueError):
        return None
    known = {f.name for f in fields(Job)}
    try:                              # 容忍旧版记录多/少字段
        return Job(**{k: v for k, v in raw.items() if k in known})
    except TypeError:
        return None


def _prune() -> None:
    try:
        files = sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for f in files[:-KEEP]:
        f.unlink(missing_ok=True)


def submit(prompt: str, fresh: bool = False) -> Job:
    """发送并确认送达，返回时已经拿到这条提问在后端的消息 id。"""
    sent = bridge.send_and_confirm(prompt, fresh=fresh)
    job = Job(
        id=uuid.uuid4().hex[:8], prompt=prompt[:300], fresh=fresh,
        sent_at=time.time(), delivered=True, status="generating",
        cid=sent.get("cid"), user_msg_id=sent.get("user_msg_id"),
        after_id=sent.get("after_id"),
    )
    _save(job)
    _prune()
    log.info("任务 %s 已发送并确认送达 (%s) 锚点=%s", job.id,
             "新会话" if fresh else "续当前会话", (job.user_msg_id or "待补")[:8])
    return job


def _deadline(wait_seconds: float) -> float:
    return time.monotonic() + max(0.0, min(float(wait_seconds), MAX_WAIT))


def _limited(job: Job, exc: "bridge.RateLimited") -> Reading:
    """被限流且等不到它解除：如实说明，让调用方过一会儿再来，不要重发。"""
    note = (f"ChatGPT 后端暂时限流（HTTP 429），请约 {exc.retry_after:.0f} 秒后再取。"
            "这不影响 GPT 生成，消息也已经送达。")
    if job.answer:
        return Reading(job.status, job.answer, job.elapsed, note=note + "下面是上次读到的内容。")
    return Reading("generating", elapsed=job.thinking_for(), note=note, asked=job.asked)


def _stale(job: Job, why: str) -> Reading:
    """后端读不到时的兜底：有存档给存档并说明，没有就报错。"""
    if job.answer:
        return Reading(job.status, job.answer, job.elapsed,
                       note=f"{why}。下面是上次保存的内容，可能不完整。")
    return Reading("error", note=why)


def poll(job_id: str, wait_seconds: float) -> tuple[Job, Reading] | None:
    """取回答。每次都从后端重读这一轮，不依赖页面渲染，也不会冻结。"""
    job = load(job_id)
    if job is None:
        return None
    # 后端已经给过 end_turn=True 的，回答不会再变，不必再请求 ——
    # 早期的冻结问题出在"DOM 判定的完成"不可靠，后端判定的没有这个问题。
    if job.final and job.answer:
        return job, Reading("done", job.answer, job.elapsed, asked=job.asked)
    deadline = _deadline(wait_seconds)

    while True:
        try:
            r = bridge.read_turn(cid=job.cid, user_msg_id=job.user_msg_id, prompt=job.prompt,
                                 after_id=None if job.user_msg_id else job.after_id,
                                 since=None if job.cid else job.sent_at)
        except bridge.RateLimited as exc:
            if time.monotonic() + exc.retry_after < deadline:
                time.sleep(exc.retry_after)
                continue
            return job, _limited(job, exc)
        except bridge.BridgeError as exc:
            return job, _stale(job, f"刷新失败：{exc}")
        if not r.get("found"):
            if r.get("pending") and job.thinking_for() < 180:
                # 刚发出、会话还没在后端出现：当作还在生成，等一会儿再读
                if time.monotonic() + bridge.API_POLL >= deadline:
                    return job, Reading("generating", elapsed=job.thinking_for(),
                                        phase="等待会话创建", asked=job.prompt[:40])
                time.sleep(bridge.API_POLL)
                continue
            return job, _stale(job, r.get("why") or "找不到这条提问")

        dirty = False
        if not job.cid and r.get("cid"):                  # 旧记录没有锚点，按文本找到后补上
            job.cid, dirty = r["cid"], True
        if not job.user_msg_id and r.get("anchorId"):
            job.user_msg_id, dirty = r["anchorId"], True

        if r["complete"] or r["interrupted"]:
            job.status, job.answer, job.error = "done", r["text"], None
            job.elapsed = r.get("duration") or job.thinking_for()
            job.final, job.asked = bool(r["complete"]), r.get("anchorText")
            _save(job)
            log.info("任务 %s 完成，%d 字，耗时 %.1fs", job_id, len(r["text"]), job.elapsed)
            note = "GPT 这一轮被中断了，下面是它已经输出的部分。" if r["interrupted"] and not r["complete"] else None
            return job, Reading("done", r["text"], job.elapsed, note=note, asked=r.get("anchorText"))

        if dirty:
            _save(job)
        if r.get("sinceLast") and r["sinceLast"] > bridge.GEN_TIMEOUT:
            return job, Reading("error", r["text"] or None, r.get("sinceAsk"),
                                note=f"这一轮已经 {r['sinceLast'] / 60:.0f} 分钟没有新进展，"
                                     "可能出错中断了。下面是已生成的部分。")
        if time.monotonic() + bridge.API_POLL >= deadline:
            return job, Reading("generating", elapsed=r.get("sinceAsk"),
                                phase=r.get("phase"), asked=r.get("anchorText"))
        time.sleep(bridge.API_POLL)


def read_last(wait_seconds: float = 0) -> Reading:
    """读标签页当前会话的最后一轮，不需要 job_id，也不发任何消息。"""
    deadline = _deadline(wait_seconds)
    while True:
        try:
            r = bridge.read_turn()
        except bridge.RateLimited as exc:
            if time.monotonic() + exc.retry_after < deadline:
                time.sleep(exc.retry_after)
                continue
            return Reading("error", note=f"ChatGPT 后端暂时限流（HTTP 429），"
                                         f"请约 {exc.retry_after:.0f} 秒后再读。")
        except bridge.BridgeError as exc:
            return Reading("error", note=str(exc))
        if not r.get("found"):
            return Reading("error", note=r.get("why"))
        if r["complete"] or r["interrupted"]:
            note = "GPT 这一轮被中断了，下面是它已经输出的部分。" if r["interrupted"] and not r["complete"] else None
            return Reading("done", r["text"], r.get("duration"), note=note, asked=r.get("anchorText"))
        if time.monotonic() + bridge.API_POLL >= deadline:
            return Reading("generating", elapsed=r.get("sinceAsk"), phase=r.get("phase"),
                           asked=r.get("anchorText"))
        time.sleep(bridge.API_POLL)
