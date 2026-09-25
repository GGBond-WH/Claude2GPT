"""任务状态 —— 落盘，不放进程内存。

之前放内存里，客户端一重启 server 进程（超时、重连都会），任务就丢了：
get_gpt_answer 查不到 job_id 就报错，Claude 以为消息没送达，于是重发 ——
而重发会打断 GPT 正在进行的思考，让它从头再想一遍。

现在：
  * 任务记录写到 ~/.gpt_bridge/jobs/<id>.json，进程重启后照样查得到。
  * 没有后台线程。get_gpt_answer 直接看 ChatGPT 页面的实时状态，
    所以"谁在轮询"这件事根本不重要。
  * 发送前会检查页面是否正在生成，正在生成就拒绝发送 —— 从结构上
    杜绝打断 GPT 思考。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import bridge

log = logging.getLogger("gpt-bridge.jobs")

STORE = Path.home() / ".gpt_bridge" / "jobs"
MAX_WAIT = 60.0          # get_gpt_answer 单次最多等多久
KEEP = 200               # 保留最近多少条记录


@dataclass
class Job:
    id: str
    prompt: str
    fresh: bool
    sent_at: float           # time.time()，跨进程可比
    delivered: bool          # 是否已确认"GPT 开始生成了"
    status: str              # generating | done | error
    answer: str | None = None
    error: str | None = None
    elapsed: float | None = None

    def thinking_for(self) -> float:
        return max(0.0, time.time() - self.sent_at)


def _path(job_id: str) -> Path:
    return STORE / f"{job_id}.json"


def _save(job: Job) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = _path(job.id).with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(job), ensure_ascii=False))
    tmp.replace(_path(job.id))       # 原子替换，避免读到写了一半的文件


def load(job_id: str) -> Job | None:
    try:
        return Job(**json.loads(_path(job_id).read_text()))
    except (OSError, ValueError, TypeError):
        return None


def _prune() -> None:
    try:
        files = sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for f in files[:-KEEP]:
        f.unlink(missing_ok=True)


def submit(prompt: str, fresh: bool = False) -> Job:
    """发送并确认送达。会阻塞到"GPT 开始生成"为止（通常 1-2 秒）。

    这个确认是整个设计的关键：返回时已经能肯定消息进去了，
    调用方就没有任何理由去重发。
    """
    job_id = uuid.uuid4().hex[:8]
    sent = bridge.send_and_confirm(prompt, fresh=fresh)
    job = Job(
        id=job_id, prompt=prompt[:200], fresh=fresh,
        sent_at=time.time(), delivered=sent["delivered"], status="generating",
    )
    _save(job)
    _prune()
    log.info("任务 %s 已发送并确认送达 (%s)", job_id,
             "新会话" if fresh else "续当前会话")
    return job


def poll(job_id: str, wait_seconds: float) -> Job | None:
    """取回答。看的是 ChatGPT 页面的实时状态，不依赖任何后台线程。"""
    job = load(job_id)
    if job is None:
        return None
    if job.status in ("done", "error"):
        return job

    deadline = time.monotonic() + max(0.0, min(wait_seconds, MAX_WAIT))
    stable = 0
    last_text = None
    while True:
        try:
            st = bridge.state()
        except bridge.BridgeError as exc:
            job.status, job.error = "error", str(exc)
            _save(job)
            return job

        if st["generating"]:
            stable, last_text = 0, st["last"]
        elif st["last"] and st["last"] == last_text:
            stable += 1
            if stable >= bridge.STABLE_ROUNDS:
                job.status = "done"
                job.answer = st["last"]
                job.elapsed = job.thinking_for()
                _save(job)
                log.info("任务 %s 完成，耗时 %.1fs", job_id, job.elapsed)
                return job
        else:
            stable, last_text = 0, st["last"]

        if time.monotonic() >= deadline:
            return job                      # 还在生成，让调用方稍后再来
        time.sleep(bridge.POLL_INTERVAL)
