"""真实本机查询与CPU采样；报告不保存问题、回答、引用正文或凭据。"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts/experiments/2026-10-03_cpu-query"


def snapshot(api_pid: int) -> dict:
    """只读进程CPU/time；只保留诊断相关进程的名称，不读取进程参数。"""
    result = subprocess.run(
        ["ps", "-axo", "pid=,pcpu=,time=,comm="],
        capture_output=True, text=True, check=True,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        pid, cpu, cpu_time, command = parts
        name = Path(command).name
        if int(pid) == api_pid or any(
            marker in name for marker in (
                "WindowServer", "VirtualMachine", "com.docker.backend", "llama-server",
            )
        ):
            rows.append({
                "pid": int(pid), "name": "rag-api" if int(pid) == api_pid else name,
                "cpu_percent": float(cpu), "cpu_time": cpu_time,
            })
    return {"timestamp": datetime.now(UTC).astimezone().isoformat(), "processes": rows}


async def run(query: bool, label: str) -> int:
    """执行真实HTTP查询并持续采样；脱敏报告与业务通过断言分开记录。"""
    state = json.loads((ROOT / ".local/runtime/service.json").read_text())
    samples = [{"phase": "before", **snapshot(state["pid"])}]
    report = {"label": label, "samples": samples, "query": None}
    if query:
        async with httpx.AsyncClient(timeout=310) as client:
            started = time.monotonic()
            # 不覆盖API默认预算，确保真实验证用户默认路径而不是特殊测试参数。
            pending = asyncio.create_task(client.post(
                f"{state['api']}/api/v1/rag/query",
                json={"question": "试用期都有哪些条文"},
            ))
            while not pending.done():
                samples.append({"phase": "during", **snapshot(state["pid"])})
                await asyncio.wait({pending}, timeout=5)
            response = await pending
            data = response.json()
            provider = data.get("provider_result", {})
            report["query"] = {
                "http_status": response.status_code,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "request_id": data.get("request_id"),
                "trace_id": data.get("trace_id"),
                "answer_status": data.get("answer_status"),
                "citation_validation": data.get("citation_validation"),
                "citations_count": len(data.get("citations", [])),
                "message_chars": len(data.get("message", "")),
                "finish_reason": provider.get("finish_reason"),
                "usage": provider.get("usage"),
                "timings": data.get("timings"),
                "retrieval_trace": data.get("retrieval_trace"),
            }
            report["business_passed"] = (
                response.status_code == 200 and data.get("answer_status") == "answered"
                and data.get("citation_validation") == "passed"
            )
    # 保留三个结束后样本，识别模型是否持续消耗CPU，不根据单一瞬时值下结论。
    for _ in range(3):
        await asyncio.sleep(5)
        samples.append({"phase": "after", **snapshot(state["pid"])})
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"{label}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "report": str(path), "business_passed": report.get("business_passed"),
        "query": report["query"], "last_sample": samples[-1],
    }, ensure_ascii=False, indent=2))
    return 0 if not query or report["business_passed"] else 1


def main() -> int:
    """解析明确的诊断阶段名称；默认只采样，不调用模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="store_true")
    parser.add_argument("--label", choices=["api-stopped", "real-query", "idle"], default="idle")
    args = parser.parse_args()
    return asyncio.run(run(args.query, args.label))


if __name__ == "__main__":
    raise SystemExit(main())
