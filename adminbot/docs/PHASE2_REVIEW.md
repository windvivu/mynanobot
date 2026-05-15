# Phase 2 — Review Feedback

Verdict: **Phase 2 Complete — Approved with notes**

Tất cả backlog items đã được fix đúng. UI đủ để dùng. Hai điểm nhỏ cần patch trước khi Phase 3.

---

## Backlog items: xác nhận đã fix

### F1 — stop() false-positive khi process tự thoát

`process_manager.py` đã refactor `_mark_stopped()` thành helper riêng và thêm re-verify sau khi taskkill thất bại:

```python
if completed.returncode != 0:
    if pid and get_process_identity(pid) is None:
        return self._mark_stopped(bot, exit_code=0)  # đã chết tự nhiên → OK
    raise RuntimeError(...)                           # thật sự lỗi
```

Đúng. Không còn false-positive RuntimeError khi process tự thoát trong khoảng thời gian ngắn.

### F2 — updated_at sai khi batch-refresh

`manager.list_bots()` giờ chỉ stamp `updated_at` cho bot thực sự thay đổi:

```python
if asdict(updated) != before:
    changed = True
    updated.updated_at = now   # chỉ bot này
```

`replace_bots()` trong registry cũng đã bỏ vòng `for bot in bots: bot.updated_at = now`. Đúng và sạch.

---

## Các review notes từ PHASE2_UI_REVIEW.md: xác nhận đã áp dụng

- `cli.py` bind `127.0.0.1`, không phải `0.0.0.0` ✓
- Route handlers dùng `def` (sync), không `async def` ✓
- `AdminbotManager` khởi tạo một lần trong `create_app()`, lưu vào `app.state.manager` ✓
- POST actions dùng PRG pattern, redirect 303 ✓
- `/bots/new` registered trước `/bots/{bot_id}` trong cùng router ✓

---

## Issues tìm thấy

### Issue 1 — `bot_detail` và `log_viewer` không có error handling cho bot_id không tồn tại

`bots.py:58` và `logs.py:32`:

```python
bot = manager.get_bot(bot_id)   # không có try/except
```

Nếu user truy cập `/bots/id-không-tồn-tại`, `get_bot()` raise `RuntimeError`. FastAPI trả về **500 Internal Server Error dạng JSON** trong khi toàn bộ app là HTML. Trông rất xấu và confusing.

Các POST action routes (start/stop/restart) đã có try/except → redirect with error. Nhưng GET routes thì không.

Cần thêm vào `bots.py` và `logs.py`:

```python
try:
    bot = manager.get_bot(bot_id)
except RuntimeError as exc:
    return _redirect(f"/?error={quote_plus(str(exc))}")
```

**Block Phase 3? Không.** Nhưng nên fix trước khi Phase 3 vì Phase 3 có thể thêm routes mới có cùng pattern.

### Issue 2 — `_tail_lines` đọc toàn bộ file vào RAM

`logs.py:16-19`:

```python
lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
return lines[-limit:]
```

File log 200 MB → đọc nguyên vào RAM trước khi lấy 200 dòng cuối. Ổn cho Phase 2 nhưng sẽ là vấn đề nếu bot chạy lâu ngày mà không có log rotation.

Đây cũng là lý do Phase 3 cần "Add log rotation policy or size guardrails" — nếu không có rotation, `_tail_lines` sẽ trở nên chậm trước khi Phase 3 xong.

**Không block Phase 3**, nhưng log rotation nên là task đầu tiên của Phase 3, không phải cuối.

---

## Ghi chú nhỏ (không block, fix bất cứ lúc nào)

- **`.form-panel` CSS class** được dùng trong `create_bot.html` nhưng không định nghĩa trong `adminbot.css`. Degrades gracefully qua `.panel` nhưng là dead class.
- **Log viewer không có refresh button** — kế hoạch Phase 2 đề cập "simple auto-refresh button". Thêm một `<a href="" class="button button-small">Refresh</a>` là đủ.
- **`/api/bots` và `/api/bots/{bot_id}` endpoints chưa có** — plan nói "add JSON endpoints only where the UI benefits". Hiện tại UI không cần, OK để defer.
- **`manager.py:12` vẫn import `utc_now_iso` từ `registry`** thay vì từ `utils`. Hoạt động bình thường nhưng là implicit re-export không cần thiết. Fix cùng lúc với refactor nào đó trong tương lai.
