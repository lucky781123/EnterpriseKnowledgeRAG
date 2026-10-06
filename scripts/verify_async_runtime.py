"""验证正在运行的本机匿名异步工作台，只上传固定公开样本并归档安全状态。"""

import asyncio
import json
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def verify():
    """复用本项目已登记的 API 地址，验证真实网络202/Worker/发布/问答闭环。"""
    state = json.loads((ROOT / ".local/runtime/service.json").read_text())
    parsed = urlsplit(state["api"])
    if (
        parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
        or not state.get("anonymous") or not state.get("async_enabled")
    ):
        raise RuntimeError("验收必须使用登记的本机匿名异步 API")
    with (ROOT / "config/deepseek.toml").open("rb") as source:
        egress = tomllib.load(source)
    if egress.get("enabled") and "local-demo" in egress.get("allowed_tenants", []):
        raise RuntimeError("本轮工作台验收须保持既有业务租户外发关闭")
    sample = "UI_ASYNC_ACCEPTANCE_CHECK 审批期限为三个工作日。"
    report = {"scope": "local_async_ui_runtime", "passed": False, "online_egress": False}
    async with httpx.AsyncClient(base_url=state["api"], timeout=310, trust_env=False) as client:
        ready = await client.get("/health/product/ready")
        frontend = await client.get("/product-workbench/")
        assert ready.status_code == frontend.status_code == 200
        report["readiness_and_frontend"] = True
        # 元数据固定公开且带时间后缀，避免覆盖用户已有文件或旧演练。
        key = "ui-public-acceptance-" + str(time.time_ns())
        began = time.monotonic()
        accepted = await client.post(
            "/api/v1/ingestion/tasks",
            data={"source_id": key, "title": "公开异步验收样本", "idempotency_key": key},
            files={"file": ("ui-public-acceptance.txt", sample.encode(), "text/plain")},
        )
        assert accepted.status_code == 202
        report["accept_http"] = 202
        report["accept_seconds"] = round(time.monotonic() - began, 3)
        task_id = accepted.json()["task_id"]
        observed = []
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = await client.get(f"/api/v1/ingestion/tasks/{task_id}")
            assert response.status_code == 200
            task = response.json()
            stage = {"stage": task["stage"], "status": task["status"]}
            if not observed or observed[-1] != stage:
                observed.append(stage)
            if task["status"] == "dead_letter":
                raise RuntimeError("公开样本入库失败")
            if task["status"] == "succeeded":
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError("公开样本任务超时")
        report["task_status"] = "succeeded"
        report["observed_states"] = observed
        report["publish_seconds"] = round(time.monotonic() - began, 3)
        query = await client.post("/api/v1/rag/query", json={
            "question": "UI_ASYNC_ACCEPTANCE_CHECK 审批期限是多少？",
            "max_tokens": 2048, "timeout_ms": 300_000,
        })
        payload = query.json()
        report["query_http"] = query.status_code
        report["answer_status"] = payload.get("answer_status")
        report["citation_validation"] = payload.get("citation_validation")
        report["citation_count"] = len(payload.get("citations", []))
        assert query.status_code == 200
        assert payload.get("answer_status") == "answered"
        assert payload.get("citation_validation") == "passed"
        assert "三个工作日" in payload.get("message", "")
        report["passed"] = True
    output = ROOT / ".local/runtime/async-acceptance.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(verify()), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 -- 禁止正文、凭据或HTTP错误细节出现在traceback。
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
