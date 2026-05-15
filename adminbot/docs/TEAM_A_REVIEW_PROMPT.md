# Team A Review Prompt

Hãy review kế hoạch cho module `adminbot` trong repo Nanobot standalone.

Tài liệu cần đọc:

- `adminbot/PLAN.md`
- `adminbot/CHECKLIST.md`
- `adminbot/README.md`

Bối cảnh:

- Hiện tại launcher nhiều bot đang mở mỗi bot trong một cửa sổ PowerShell riêng.
- Mục tiêu của `adminbot` là thay thế trải nghiệm đó bằng một lớp quản trị chung, có thể copy sang repo Nanobot khác để tái sử dụng.
- `adminbot` phải tách biệt khỏi core `nanobot`, ưu tiên tính portable, quản lý nhiều bot local, và tiến tới một giao diện điều khiển chung thay vì nhiều terminal rời.

Yêu cầu review:

1. Đánh giá xem `PLAN.md` đã rõ scope, ranh giới trách nhiệm, và hướng kiến trúc chưa.
2. Kiểm tra xem mục tiêu portability có thực tế không khi chỉ copy thư mục `adminbot/` sang repo khác.
3. Chỉ ra các rủi ro kỹ thuật sớm, đặc biệt ở:
   - process management trên Windows
   - lưu trạng thái/pid/log
   - tích hợp với `nanobot` qua CLI
   - web manager UI
   - terminal tab tương tác trong tương lai
4. Kiểm tra xem `CHECKLIST.md` đã đủ để triển khai theo phase chưa, còn thiếu hạng mục quan trọng nào không.
5. Đề xuất thay đổi nếu thấy cần, nhưng ưu tiên giữ:
   - `adminbot/` là code package độc lập
   - `.adminbot/` là runtime data
   - phase 1 chỉ cần logs + process control, chưa bắt buộc terminal interactive

Đầu ra mong muốn:

- Danh sách các điểm `Approved`, `Concerns`, `Required changes`
- Nếu có vấn đề, nêu rõ:
  - vấn đề là gì
  - ảnh hưởng thực tế
  - đề xuất chỉnh vào file nào
- Kết luận cuối:
  - `Approve`
  - `Approve with changes`
  - `Needs redesign`
