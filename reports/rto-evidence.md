# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T05:07:02` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.1 | dòng `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:86` |
| Health check phát hiện | 16.1 | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 9.0 | `step:2_restore_snapshot` | `reports/failover-events.jsonl:3` |
| Region phụ ready | 15.5 | `step:4_wait_ready` | `reports/failover-events.jsonl:5` |
| DNS cutover | 15.5 | `step:5_dns_cutover` | `reports/failover-events.jsonl:6` |
| **RTO đo được** | **16.6** | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:94` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---:|---:|---|
| RTO — Inference API | 16.6s | 300s (5 phút) | PASS |
| RPO — Vector DB | 2.0s / 1 doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---|---|
| Health-check detect floor | 15.0s | `interval_s=5.0 × threshold=3` trong `reports/health-events.jsonl:2` | Giảm interval hoặc threshold, nhưng cần tránh flapping |
| Snapshot restore | 0.004s | Từ `2_restore_snapshot` đến `3_scale_pool`: `reports/failover-events.jsonl:3-4` | Snapshot gần hơn, storage nhanh hơn |
| GPU pool warm-up | 6.5s | Từ `3_scale_pool` đến `4_wait_ready`: `reports/failover-events.jsonl:4-5` | Giữ pool warm hoặc giảm thời gian khởi động model |
| DNS/LB TTL cache | 1.1s | `t_recovered - t_cutover`: `reports/drill-2-withdr.jsonl:94` và `reports/failover-events.jsonl:6` | Giảm TTL hoặc dùng cơ chế invalidation nhanh hơn |