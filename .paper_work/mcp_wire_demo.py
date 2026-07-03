# MCP 去黑盒演示：不用 mcp 库，手写原始 JSON-RPC 与 teacher-mcp server 对话，打印线上真实报文
import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
PY = r"D:\workplace\book-ai\teacher-mcp\.venv\Scripts\python.exe"

p = subprocess.Popen([PY, "-m", "app.server"], cwd=r"D:\workplace\book-ai\teacher-mcp",
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

def send(obj):
    line = json.dumps(obj, ensure_ascii=False)
    print(f"\n>>> 客户端发: {line[:180]}")
    p.stdin.write((line + "\n").encode("utf-8"))
    p.stdin.flush()

def recv():
    line = p.stdout.readline().decode("utf-8")
    return json.loads(line)

# ① 握手
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "wire-demo", "version": "0"}}})
r = recv()
print(f"<<< 服务端答: server={r['result']['serverInfo']}, 能力={list(r['result']['capabilities'].keys())}")
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

# ② 工具发现：agent 「知道怎么调」的全部来源就是这一步返回的东西
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
r = recv()
tools = r["result"]["tools"]
print(f"<<< 服务端答: 共 {len(tools)} 个工具。挑 resolve_kg 看它到底给了 agent 什么：")
rk = next(t for t in tools if t["name"] == "resolve_kg")
print("    name:", rk["name"])
print("    description(截取):", rk["description"][:120].replace("\n", " "))
print("    inputSchema:", json.dumps({k: (list(v.keys()) if k == "properties" else v)
                                      for k, v in rk["inputSchema"].items() if k in ("type", "properties", "required")},
                                     ensure_ascii=False))

# ③ 工具调用：模型决定调用后，harness 替它发的就是这么一条
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
    "name": "resolve_kg",
    "arguments": {"subject_root": "100", "query": "乘方", "leaves_only": True, "limit": 2}}})
r = recv()
content = r["result"]["content"][0]["text"]
print(f"<<< 服务端答(工具返回,截取): {content[:200]}")

p.terminate()
print("\n=== 演示完 ===")
