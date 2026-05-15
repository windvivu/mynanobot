# Phase 2 UI Plan — Review Feedback

Verdict: **Approved with notes**

Framework, layout, scope đều ổn. Không có blocker. Ba điểm bên dưới cần bổ sung vào `PHASE2_UI_PLAN.md` trước khi bắt đầu implement.

---

## Cần bổ sung trước khi implement

### 1. Bẫy `host="0.0.0.0"` khi copy từ nanobot

`nanobot/web/cli.py` bind `0.0.0.0`. Adminbot plan nói bind `127.0.0.1` — đúng — nhưng nếu dùng `nanobot/web/cli.py` làm template mà không để ý sẽ copy nguyên dòng đó. Adminbot không có auth mặc định nên bind sai là lỗ hổng thật.

Thêm vào `PHASE2_UI_PLAN.md` phần Technical Notes:

> `adminbot/app/web/cli.py` phải bind `127.0.0.1`, không phải `0.0.0.0` như nanobot. Không copy giá trị host từ `nanobot/web/cli.py`.

---

### 2. `AdminbotManager` là sync — cần quyết định strategy trước khi viết route đầu tiên

`list_bots()` đọc file + spawn PowerShell per bot. `start_bot()` / `stop_bot()` spawn subprocess. Tất cả blocking. FastAPI handler mặc định là `async def` — gọi blocking code trực tiếp sẽ block event loop.

Khuyến nghị: dùng **sync route** (`def` thay vì `async def`) — FastAPI tự chạy trong threadpool, không cần thay đổi manager.

Thêm vào `PHASE2_UI_PLAN.md` phần Runtime Model:

> Route handlers dùng `def` (sync), không phải `async def`, để FastAPI tự offload sang threadpool. Không gọi manager methods trực tiếp từ `async def` handler.

---

### 3. `AdminbotManager` phải khởi tạo một lần trong `create_app()`, không phải trong từng route

Nếu không có hướng dẫn rõ, implementer có thể khởi tạo `AdminbotManager(paths)` bên trong mỗi handler — mỗi request sẽ gọi `ensure_runtime_dirs()` và đọc `bots.json` thừa.

Pattern đúng (giống nanobot dùng `app.state`): khởi tạo một lần trong `create_app()`, lưu vào `app.state.manager`. Route handlers đọc từ `request.app.state.manager`.

Thêm vào `PHASE2_UI_PLAN.md` phần Runtime Model:

> `AdminbotManager` được khởi tạo một lần trong `create_app()` và lưu vào `app.state.manager`. Route handlers truy cập qua `request.app.state.manager`.

---

## Ghi chú nhỏ (có thể xử lý trong PR đầu tiên)

- **POST action endpoints** (start/stop/restart): nên dùng PRG pattern — redirect về `GET /bots/{bot_id}` sau khi thành công, trả lỗi bằng cách re-render page với message.
- **Route registration order**: `GET /bots/new` phải được include router trước `GET /bots/{bot_id}`, nếu không "new" bị capture thành bot_id.
- **Log viewer API**: nên trả JSON array of strings cho dễ xử lý JS, ghi rõ trong route spec.
- **CHECKLIST.md**: tick `Choose Manager UI web framework` và `Decide whether to embed dashboards or open separately` vì cả hai đã được quyết định trong plan.
