# Runbook 1 trang — Region chính down (TEMPLATE, sinh viên điền)

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.


| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | Region A `alive=false`, Region B `alive=true`; xác nhận 3 lần liên tiếp | On-call |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | Có dòng `step:2`, `name:"thong_bao_incident"` trong `reports/runbook-run.jsonl`; ghi nhận `t_outage` và `t_notified` | On-call |
| 3 | Restore state ở region phụ | Để `dr/runbook.py` gọi `failover.failover(...)` một lần duy nhất; không chạy `snapshot.py get` thủ công | Có `step:"2_restore_snapshot"` với `ok:true`, `rpo_seconds`, `docs_lost` và `embed_model_version` trong `reports/failover-events.jsonl` | DR operator |
| 4 | Scale pool warm→full | Để `dr/runbook.py` thực hiện; không đổi `pool_state` thủ công | Có `step:"3_scale_pool"` với `pool_state:"full"` và `/readyz` của B trả HTTP 200 ở bước `4_wait_ready` | Platform engineer |
| 5 | DNS/LB cutover | Để `dr/runbook.py` thực hiện sau khi B ready | Có `step:"5_dns_cutover"` với `ok:true`; `curl localhost:8080/edge/state` trả `active_region:"b"` | On-call |
| 6 | Verify golden signals | Để `dr/runbook.py` gửi 10 request thật tới B | Có `requests:10`, `errors:0`, `error_rate:0.0`, và `p95_latency_ms < 5000` trong `reports/runbook-run.jsonl` | SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300 > reports/measure-drill-2.json` | `rto_verdict` khác `null`; lưu RTO, RPO, số document mất và các evidence path vào postmortem | Incident commander |


**Rollback (failover ngược):**

Chỉ trả traffic về Region A khi thỏa mãn tất cả điều kiện:

- Region B đang lỗi, không đạt `/readyz`, hoặc có error rate > 0%.
- Region A đã được khôi phục đầy đủ state, vector DB và model weights.
- `/readyz` của Region A trả HTTP 200 trong 3 lần kiểm tra liên tiếp.
- Đã gửi 10 request thử nghiệm tới Region A với `errors=0`.
- Incident commander hoặc SRE lead đã phê duyệt rollback.
