# /// script
# requires-python = ">=3.11"
# ///
"""选择器校准 —— 跑通 ask_gpt 之前先跑这个。

它不发消息，只报告当前 ChatGPT 页面上 bridge.SELECTORS 各项的命中情况。
哪一项是 null，就说明那条回退链全落空了，需要照实际 DOM 改 bridge.py。
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
missing = [k for k, v in info["matched"].items() if v is None]
if missing:
    print(f"落空的选择器: {', '.join(missing)} —— 需要照实际 DOM 修 bridge.py 的 SELECTORS")
    sys.exit(2)
print("四类选择器全部命中，可以试 ask_gpt。")
