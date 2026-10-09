# Postmortem — DR Drill Lab 23 (TEMPLATE)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)


| ISO time | Sự kiện | Evidence |
|---|---|---|
| `2026-10-09T05:26:39` | outage bắt đầu | `chaos/chaos-events.jsonl:3` |
| `2026-10-09T05:26:39.674672+00:00` | user đầu tiên bị ảnh hưởng | `reports/drill-2-withdr.jsonl:86` |
| `2026-10-09T05:26:55.646615+00:00` | health check alert Region A unhealthy | `reports/health-events.jsonl:2` |
| `2026-10-09T05:26:55.024914+00:00` | operator xác nhận/thực thi failover sang Region B | `reports/runbook-run.jsonl:3` |
| `2026-10-09T05:26:56.157121+00:00` | resolved, request đầu tiên thành công từ Region B | `reports/drill-2-withdr.jsonl:94` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `16.6s` · gap: `283.4s`
- RPO mục tiêu: 300s · đo được: `2.0s` (`1` doc bị mất) · gap: `298.0s`
- **Bước tốn nhiều giây nhất:** `Health-check detection` — mất khoảng `16.1s` từ lúc outage bắt đầu đến khi Region A bị đánh dấu `UNHEALTHY`; detection floor là `5s × 3 = 15s`.

## 3. Root cause (5 whys)

1. Vì sao người dùng nhận lỗi?  
   Region A bị outage nhưng edge vẫn tiếp tục route traffic tới Region A.

2. Vì sao edge chưa chuyển sang Region B ngay?  
   Hệ thống phải chờ health checker phát hiện Region A không ready sau 3 lần kiểm tra liên tiếp.

3. Vì sao Region B chưa thể phục vụ ngay khi được chọn?  
   Region B ban đầu không có vector data, model weights và cần restore snapshot, scale GPU pool rồi warm-up.

4. Vì sao timeline failover có cảnh báo `t_cutover < t_detect`?  
   Failover/cutover được kích hoạt trước khi health checker ghi nhận trạng thái `UNHEALTHY` của Region A.

5. Vì sao hệ thống cho phép cutover trước health alert?  
   Quy trình hiện chưa có cơ chế bắt buộc failover phải chờ tín hiệu `UNHEALTHY` hợp lệ từ health checker và chưa có circuit breaker bảo vệ chống failover sớm hoặc flapping.

Root cause chính là quy trình orchestration chưa liên kết chặt chẽ health-check detection với quyền thực hiện DNS cutover. Trong outage thật, operator có thể chuyển traffic quá sớm, khiến cả hai region cùng trả lỗi hoặc làm timeline RTO không đáng tin cậy. Đây là khoảng trống của hệ thống và quy trình, không phải lỗi cá nhân.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Bắt buộc failover chỉ được cutover sau khi health checker ghi nhận Region A `UNHEALTHY` trong 3 lần liên tiếp; thêm circuit breaker chống flapping | SRE / DR owner | 1 ngày | `0s` trực tiếp; loại bỏ cutover sớm và timeline RTO không hợp lệ |
| 2 | Giảm health-check interval từ `5s` xuống `1s`, vẫn giữ threshold `3` và theo dõi flapping | Platform engineer | 2 ngày | Giảm detection floor tối đa `12s` (`15s` xuống `3s`) |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của tôi là `5s × 3 = 15s`. So với RTO đo được `16.6s`, detection floor chiếm khoảng `90.4%` RTO (`15 / 16.6 × 100`). Thực tế health check ghi nhận sau `16.1s`, tương đương khoảng `97.0%` RTO.

2. Nếu hạ interval xuống `1s` và giữ threshold là `3`, detection floor giảm từ `15s` xuống `3s`, tức RTO lý thuyết giảm tối đa khoảng `12s`, còn khoảng `4.6s` nếu các bước khác không đổi. Đổi lại, hệ thống sẽ probe thường xuyên hơn, tăng nguy cơ false positive và failover flapping khi mạng hoặc service chập chờn. Cần hysteresis, circuit breaker hoặc cooldown để kiểm soát rủi ro này.

3. Trong drill, `docs_lost=1` nghĩa là một document mới nhất chưa được replicate sang Region B trước thời điểm restore. Với khách hàng, document đó có thể chưa xuất hiện trong tìm kiếm hoặc inference của Region B, khiến câu trả lời bị thiếu hoặc cũ. Nếu outage kéo dài 6 giờ và Region A mất dữ liệu vĩnh viễn, `docs_lost` phải đại diện cho toàn bộ document đã ghi ở Region A nhưng chưa được replicate; đó là dữ liệu khách hàng mất vĩnh viễn và là tác động kinh doanh trực tiếp của RPO.