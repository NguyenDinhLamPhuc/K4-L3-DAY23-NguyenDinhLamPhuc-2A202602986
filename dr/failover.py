"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
from datetime import datetime, timezone
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append một dòng JSONL có timestamp và in ra stdout."""
    ts = time.time()
    line = json.dumps({**kw, "ts": ts,
                       "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat()},
                      ensure_ascii=False)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(line + "\n")
    print(line, flush=True)


def failover(target: str, backend: str, wait: float) -> dict:
    """Thực hiện 5 bước; chỉ cutover sau khi target đã ready."""
    result = {"ok": False, "target": target, "rpo_seconds": None,
              "docs_lost": None, "completed_steps": []}
    step = "1_verify_target"

    def complete(**details):
        emit(step=step, target=target, ok=True, **details)
        result["completed_steps"].append(step)

    try:
        if target not in URL:
            raise ValueError("target must be a or b")
        if backend not in ("fs", "minio"):
            raise ValueError("backend must be fs or minio")
        if not math.isfinite(wait) or wait <= 0:
            raise ValueError("wait must be finite and positive")

        response = httpx.get(f"{URL[target]}/v1/state", timeout=min(2.0, wait))
        response.raise_for_status()
        state = response.json()
        if not isinstance(state, dict) or state.get("region") != target:
            raise ValueError("target state does not match the requested region")
        # Warm/empty targets are expected: restore and scale happen next.
        result["target_state"] = state
        complete(state=state)

        step = "2_restore_snapshot"
        restored = snapshot.get(target, backend)
        primary = "a" if target == "b" else "b"
        target_dir = pathlib.Path(f"state/region-{target}")
        recovery = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                                target_dir / "vectors.sqlite")
        result.update(recovery)
        result["embed_model_version"] = restored["embed_model_version"]
        complete(**recovery, embed_model_version=restored["embed_model_version"])

        step = "3_scale_pool"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "pool_state").write_text("full", encoding="utf-8")
        complete(pool_state="full")

        step = "4_wait_ready"
        deadline = time.monotonic() + wait
        reason = "target did not become ready"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"readiness timeout after {wait}s: {reason}")
            try:
                response = httpx.get(f"{URL[target]}/readyz",
                                     timeout=min(2.0, remaining))
                if response.status_code == 200 and time.monotonic() < deadline:
                    ready_state = response.json()
                    result["restored_state"] = {
                        "region": target,
                        "count": ready_state["vectors"]["count"],
                        "weights": True,  # /readyz checks model weights before returning 200.
                        "pool_state": ready_state["pool_state"],
                    }
                    break
                reason = f"/readyz HTTP {response.status_code}"
            except httpx.RequestError as exc:
                reason = f"{type(exc).__name__}: {exc}"
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0, remaining))
        complete()

        step = "5_dns_cutover"
        active = pathlib.Path("edge/active_region")
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text(target, encoding="utf-8")
        complete()
        result["ok"] = True
    except (Exception, SystemExit) as exc:
        # snapshot.get uses SystemExit when no snapshot exists.
        result.update(failed_step=step, reason=f"{type(exc).__name__}: {exc}")
        emit(step=step, target=target, ok=False, reason=result["reason"])
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
