# /// script
# requires-python = ">=3.11"
# ///
"""健康检查 —— 不发任何消息。

必须满足：已登录、找得到输入框和新建会话按钮、后端能读到当前会话。
「发送」「停止」只在特定时刻存在（输入框有字 / 正在生成），只作参考。
回答是从后端读的，不依赖页面渲染，所以这里不检查回答的选择器。
"""

import json
import sys

import bridge

try:
    info = bridge.probe()
except bridge.BridgeError as exc:
    print(f"探测失败: {exc}", file=sys.stderr)
    sys.exit(1)

print(json.dumps(info, ensure_ascii=False, indent=2))
print()
problems = []
if info["loggedOut"]:
    problems.append("未登录 chatgpt.com")
for k in ("composer", "new_chat"):
    if info["matched"][k] is None:
        problems.append(f"选择器 {k} 落空 —— 照实际 DOM 修 bridge.py 的 SELECTORS")
b = info["backend"]
if b is not None and not b["ok"]:
    problems.append(f"后端读取失败: {b['error']}")
if problems:
    print("有问题：\n  " + "\n  ".join(problems))
    sys.exit(2)
if b is None:
    print("基本检查通过（当前是空白新会话，后端读取在第一次提问后才能验证）。")
else:
    print(f"全部通过（后端读到当前会话 {b['messages']} 条消息）。")
