"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
from datetime import datetime, timezone
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước vào JSONL và stdout, trả lại timestamp thực tế."""
    ts = time.time()
    record = {**kw, "ts": ts, "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
              "step": n, "name": name}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(line + "\n")
    print(line, flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Mặc định từ chối nếu không có xác nhận rõ ràng."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Xác nhận outage, thực hiện failover một lần và kiểm tra phục hồi."""
    started = time.monotonic()
    result = {"ok": False, "primary": primary, "target": target,
              "completed_steps": []}

    def finish(reason=None):
        if reason:
            result["reason"] = reason
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        result["measure_rto_command"] = (
            "python tools/measure_rto.py --loadgen reports/drill-2.jsonl --target-rto 300")
        step(7, "post_incident", **result)
        result["completed_steps"].append(7)
        return result

    if primary not in URL or target not in URL or primary == target or backend not in ("fs", "minio"):
        step(1, "xac_nhan_outage", ok=False, reason="Invalid regions or backend")
        return finish("Primary and target must be different valid regions; backend must be fs or minio")

    probes = []
    for attempt in range(3):
        observations = {}
        for region in (primary, target):
            ready, reason = hc.probe(region, timeout=2.0)
            observations[region] = {"ready": ready, "reason": reason}
        probes.append(observations)
        if attempt < 2:
            time.sleep(1.0)
    outage = all(not observation[primary]["ready"] for observation in probes)
    step(1, "xac_nhan_outage", ok=outage, primary=primary, target=target, probes=probes)
    if not outage:
        return finish("Primary outage not confirmed by three consecutive failures")
    result["completed_steps"].append(1)

    # Use the latest unmatched chaos kill for this region; never invent outage time.
    t_outage = None
    now = time.time()
    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    warning = None
    try:
        for line in chaos.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("region") == primary and event.get("ts", now) < now:
                if event.get("action") == "kill":
                    t_outage = event["ts"]
                elif event.get("action") == "restore":
                    t_outage = None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        t_outage = None
        warning = f"Outage timestamp unavailable: {exc}"
    notice = step(2, "thong_bao_incident", primary=primary, target=target,
                  t_outage=t_outage, warning=warning,
                  message=f"Region {primary} outage confirmed; failover to {target} proposed")
    result.update(t_outage=t_outage, t_notified=notice["ts"],
                  notification_delay_s=None if t_outage is None else notice["ts"] - t_outage)
    result["completed_steps"].append(2)
    if not confirm(auto, f"Failover region {primary} -> {target}?"):
        step(3, "scale_gpu_pool", ok=False, skipped=True, reason="Operator declined")
        return finish("Operator declined failover")

    try:
        recovery = fo.failover(target, backend, wait=60.0)
    except Exception as exc:
        step(3, "scale_gpu_pool", ok=False, reason=str(exc))
        return finish(f"Failover failed: {exc}")
    result["failover"] = recovery
    result.update(rpo_seconds=recovery.get("rpo_seconds"), docs_lost=recovery.get("docs_lost"))
    step(3, "scale_gpu_pool", ok=recovery.get("ok", False), result=recovery)
    if not recovery.get("ok"):
        return finish(recovery.get("reason", "Failover failed"))
    result["completed_steps"].append(3)

    state = recovery.get("restored_state", {})
    replica_ok = (state.get("region") == target and state.get("weights") is True
                  and isinstance(state.get("count"), int) and state["count"] > 0)
    step(4, "verify_state_replica", ok=replica_ok, state=state,
         embed_model_version=recovery.get("embed_model_version"))
    if replica_ok:
        result["completed_steps"].append(4)
    cutover_ok = "5_dns_cutover" in recovery.get("completed_steps", [])
    step(5, "dns_cutover", ok=cutover_ok, target=target)
    if cutover_ok:
        result["completed_steps"].append(5)

    latencies, errors = [], 0
    for _ in range(10):
        request_started = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=5.0)
            body = response.json()
            healthy = (response.status_code == 200 and isinstance(body, dict)
                       and body.get("region") == target and "answer" in body and "error" not in body)
            errors += int(not healthy)
        except (httpx.RequestError, ValueError):
            errors += 1
        latencies.append((time.monotonic() - request_started) * 1000)
    # Nearest-rank p95 for ten samples is the maximum.
    signals = {"requests": 10, "errors": errors, "error_rate": errors / 10,
               "p95_latency_ms": round(sorted(latencies)[9], 3)}
    result["golden_signals"] = signals
    step(6, "verify_golden_signals", ok=errors == 0, **signals)
    if errors == 0:
        result["completed_steps"].append(6)
    result["ok"] = replica_ok and cutover_ok and errors == 0
    return finish(None if result["ok"] else "Recovery verification failed")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
