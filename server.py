"""gpt-bridge MCP server —— 让 Claude 和 GPT 一起讨论科研问题。

工具：
  ask_gpt(prompt)          开新会话提问，确认送达后返回 job_id
  continue_gpt(prompt)     在当前会话追问，确认送达后返回 job_id
  get_gpt_answer(job_id)   取回答，还在思考就返回 pending

Prompt：
  discuss(question)        Claude 与 GPT 就一个科研问题多轮讨论

设计上刻意保证：ask_gpt / continue_gpt 返回时**消息一定已经送达**
（等到 GPT 真的开始生成才返回），所以任何情况下都不需要"重发以防万一"。
重发会打断 GPT 的思考，让它从头再想一遍。
"""

from __future__ import annotations

import anyio
from mcp.server.mcpserver import MCPServer

import bridge
import jobs

def _protocol(max_rounds: str = "5") -> str:
    """讨论协议正文。server instructions 和 discuss prompt 共用同一份。"""
    return f"""## 什么时候停，你自己判断

**不要问用户任何许可性问题** —— "要不要继续""要不要追问某点""需要我查一下吗"
"要我展开吗"全都不行。该做就做，做完把结果给他。需要核实的引用自己去查，
查不了就标注未核实，但不要停下来问要不要查。

### 停止的硬前提：手上不能还留着没发给 GPT 的意见

任何你认为 GPT **说错了、遗漏了、或者前后自相矛盾**的地方，
必须先用 continue_gpt 发给它、看它怎么回应，才算数。

把发现的问题只报告给用户、不发回给 GPT，是这个流程最严重的失败方式 ——
用户要的是双方碰过之后的结论，不是你单方面的读后感。没有经过 GPT 回应的
异议不构成"剩余分歧"，也不能作为停止的理由。

**自相矛盾要优先打。** GPT 前后不一致的地方一定要指出来让它自己解决 ——
这通常是整轮讨论里信息量最大的一次交互。

### 收尾前逐条自查

- 我还有任何一条对 GPT 的意见没发给它吗？→ 有就继续，不许停
- 我提出的质疑，它正面回应了吗？→ 没有就再问一次
- 它自相矛盾的地方，我指出来了吗？→ 没有就现在指

三条都过了，再看下面，满足任一即可收尾：

- 连续一轮没有新的实质信息，也没有新的分歧
- 剩余分歧**双方都已明确表态**，且能归类为"需要新证据或新实验才能解决"
- 已经跑满 {max_rounds} 轮

## 流程

1. 先自己独立分析。写下初步判断，以及你最没把握的那一环。

2. ask_gpt（新会话）请 GPT 独立分析同一问题。
   **不要透露你的判断** —— 否则它会顺着你说，就白问了。

   如果 ChatGPT 开着「记忆」，新会话仍可能带入旧内容。发现它引用了本轮
   没提过的信息就指出来，并在最后说明这削弱了独立性。

3. 把它的回答分四类：
   - 你们一致的
   - 它想到而你没想到的（通常比争论谁对更有价值，别跳过）
   - 你们冲突的
   - **你认为它明显说错、遗漏、或自相矛盾的** —— 这一类必须全部发回给它

4. continue_gpt 逐点推进。优先级：先打自相矛盾，再打你认为它错的地方，
   然后是冲突处 —— 冲突处**先分清是定义不同还是事实判断不同**。
   一次聚焦一两个点。它没有正面回答就指出来再问一次，不要放过。

   **推进一律用 continue_gpt，不要反复开新会话** —— 每开一次新会话就把
   前面的讨论上下文全扔了，多轮讨论就不可能发生。

5. 回到第 3 步，直到触发停止条件。中间不要问用户。

## 收尾：必须做共识确认

想清楚之后，把综合稿用 continue_gpt 发给 GPT，明确要求它逐条表态：
**哪些同意、哪些要修正、哪些仍然不同意以及理由。**

按它的回应修订。仍有实质异议就再确认一轮（最多两轮）。
没有经过这一步的结论不能写进"双方共识"。

## 最终交付

- **双方共识** —— 经 GPT 明确确认同意的结论，这是主体，写充分
- **仍有分歧** —— 各自立场、分歧根源、以及什么证据能解决它
- **待核实** —— GPT 给出的具体文献、数字、年份单独列出并标注「未核实」。
  它编造精确引用是常见故障，不得直接当事实写进共识
- **下一步** —— 该查什么文献、做什么实验、推哪个特例

把 GPT 的关键原话摘出来，让用户能自己判断谁更有理。
不确定就说不确定，不要为了给出干净结论而抹掉分歧。"""


INSTRUCTIONS = f"""这个 server 让你能和 GPT（本机 ChatGPT）讨论问题。

工具：ask_gpt（开新会话）/ continue_gpt（续当前会话）发消息，返回时消息
已确认送达；再用 get_gpt_answer(job_id) 取回答，取到「还在思考」就再取一次。
GPT 想几分钟是正常的，任何情况下都不要重发消息。

单次提问（"问一下 GPT X 是什么"）直接用工具即可，不必走下面的流程。

**当用户要求和 GPT 讨论、辩论、或者让 GPT 一起看某个问题时**
（"和 GPT 讨论一下…""问问 GPT 怎么看…""让 GPT 也评估一下…"都算），
按下面的协议走：

{_protocol()}
"""

server = MCPServer(name="gpt-bridge", instructions=INSTRUCTIONS)

_SENT = (
    "✓ 已确认送达（GPT 已开始生成），job_id = {jid}\n"
    'GPT 想清楚需要时间，用 get_gpt_answer("{jid}") 取回答；'
    "返回「还在思考」就再取一次。\n"
    "**不要重发这条消息** —— 它已经送达了，重发会打断 GPT 的思考。"
)


def _send(prompt: str, fresh: bool) -> str:
    try:
        job = jobs.submit(prompt, fresh=fresh)
    except bridge.Busy as exc:
        return f"[没有发送] {exc}"
    except bridge.BridgeError as exc:
        return f"[发送失败] {exc}"
    return _SENT.format(jid=job.id)


@server.tool()
async def ask_gpt(prompt: str) -> str:
    """开一个**全新的** ChatGPT 会话提问。返回时消息已确认送达。

    新会话意味着 GPT 不带这一轮之外的上文 —— 要一个不受你影响的独立视角时
    用这个（交叉验证、找自己论证里的漏洞、看别人怎么切入同一个问题）。

    想在同一个会话里接着聊，用 continue_gpt。

    返回 job_id 后用 get_gpt_answer 取结果。GPT 可能要想一两分钟，
    这是正常的 —— 耐心取几次，不要重发。
    """
    return await anyio.to_thread.run_sync(lambda: _send(prompt, True))


@server.tool()
async def continue_gpt(prompt: str) -> str:
    """在**当前** ChatGPT 会话里接着聊。返回时消息已确认送达。

    GPT 记得这个会话里之前的全部内容，所以不用重复背景。
    多轮讨论、追问某一步的依据、请它展开某个点，都用这个。

    如果 GPT 正在生成，这个工具会拒绝发送并告诉你 —— 那说明上一条还没答完，
    先去 get_gpt_answer 取回来。

    返回 job_id 后用 get_gpt_answer 取结果。
    """
    return await anyio.to_thread.run_sync(lambda: _send(prompt, False))


@server.tool()
async def get_gpt_answer(job_id: str, wait_seconds: int = 30) -> str:
    """取回 ask_gpt / continue_gpt 的回答。

    最多等 wait_seconds 秒（上限 60）。还在思考就返回 pending —— 这时
    **再调用一次这个工具**即可，不要重发原消息。GPT 思考几分钟是正常的。
    """
    job = await anyio.to_thread.run_sync(jobs.poll, job_id, float(wait_seconds))
    if job is None:
        return (
            f"[没有这个 job_id: {job_id}] 可能记录已过期。"
            "不要凭猜测重发消息 —— 先用 get_gpt_answer 试最近的 job_id，"
            "或者直接看 ChatGPT 页面确认上一条的状态。"
        )
    if job.status == "done":
        return job.answer or "[GPT 返回了空回答]"
    if job.status == "error":
        return f"[失败] {job.error}"
    return (
        f"[GPT 还在思考] job {job_id}，已思考 {job.thinking_for():.0f}s。\n"
        "消息**确认已送达**（发送时已观察到 GPT 开始生成）。\n"
        f'再调用一次 get_gpt_answer("{job_id}") 即可，不要重发原消息。'
    )


@server.prompt(
    name="discuss",
    title="和 GPT 讨论科研问题",
    description="Claude 与 GPT 多轮讨论一个科研问题，自主决定何时收敛，"
                "最后产出经双方确认的共识结论。",
)
def discuss(question: str, max_rounds: str = "5") -> str:
    """生成一段讨论主持指令。"""
    return f"""和 GPT 讨论下面这个问题。

{question}

{_protocol(max_rounds)}"""


if __name__ == "__main__":
    server.run()
