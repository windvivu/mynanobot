# Team A Final Review Prompt

Hãy thực hiện vòng review cuối cho `adminbot` trước khi tiếp tục sâu vào Phase 4.

Tài liệu và code cần đọc:

- `adminbot/README.md`
- `adminbot/CHECKLIST.md`
- `adminbot/HANDOFF_CHECKLIST.md`
- `adminbot/PHASE4_TERMINAL_PLAN.md`
- `adminbot/app/`

Bối cảnh:

- Phase 1, 2, và 3 đã được implement và review qua nhiều vòng.
- Mục tiêu của vòng này là xác nhận `adminbot` đã đủ sạch về release polish, tài liệu bàn giao, và trạng thái checklist.
- Đồng thời cần xác nhận hướng mở đầu của Phase 4 là hợp lý trước khi đi vào terminal integration.

Yêu cầu review:

1. Kiểm tra `README.md` đã đủ rõ để một người mới trong repo cài và chạy `adminbot` chưa.
2. Kiểm tra `HANDOFF_CHECKLIST.md` có thiếu bước bàn giao quan trọng nào không.
3. Kiểm tra `CHECKLIST.md` có phản ánh đúng trạng thái implementation hiện tại không.
4. Đánh giá xem `PHASE4_TERMINAL_PLAN.md` có chọn đúng thứ tự ưu tiên:
   - logs-first
   - shell-tab fallback
   - PTY/ConPTY evaluation tách riêng
5. Chỉ ra nếu còn risk hoặc tài liệu lệch thực tế ở bất kỳ phần nào.

Đầu ra mong muốn:

- `Release polish approved`
- hoặc `Release polish approved with notes`
- hoặc `Changes required before Phase 4`

Nếu có issue, vui lòng nêu rõ:

- file nào cần chỉnh
- đó là documentation issue, operational risk, hay architectural concern
- có block Phase 4 không
