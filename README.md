# Nanobot Standalone

Bản standalone này chỉ giữ phần cốt lõi gồm `nanobot`, `bridge` và `pyproject.toml`.

## Cài đặt

Trong PowerShell:

```powershell
cd E:\CODES2\nanobotalone
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e "."
```

Sau khi cài xong, bạn có thể dùng trực tiếp lệnh `nanobot`.

## Khởi tạo cấu hình

Lần đầu tiên, hãy tạo cấu hình mặc định:

```powershell
nanobot onboard
```

Lệnh này sẽ tạo file:

```text
~/.nanobot/config.json
```

và đồng thời tạo workspace mặc định nếu chưa có.

## Chạy ở cổng mặc định

Bật web dashboard ở cổng mặc định `8899`:

```powershell
nanobot gateway --web
```

Sau đó mở:

```text
http://127.0.0.1:8899
```

Lưu ý:
- `--web` dùng để bật dashboard web.
- Cổng `8899` là giá trị mặc định của `gateway.web.port`.
- Tùy chọn `--port` của lệnh `gateway` là cổng gateway chính, không phải cổng web dashboard.

## Chạy ở cổng tùy chọn

Muốn đổi cổng web dashboard, trước hết cần chạy `nanobot onboard` để tạo `~/.nanobot/config.json`.

Sau đó sửa file `~/.nanobot/config.json` như sau:

```json
{
  "gateway": {
    "web": {
      "enabled": true,
      "port": 9001
    }
  }
}
```

Rồi chạy lại:

```powershell
nanobot gateway --web
```

Ví dụ khi đặt `port` là `9001`, dashboard sẽ chạy tại:

```text
http://127.0.0.1:9001
```

## Workspace mặc định

Workspace mặc định của nanobot là:

```text
~/.nanobot/workspace
```

Nếu bạn chạy:

```powershell
nanobot onboard
```

thì nanobot sẽ dùng workspace mặc định này.

## Workspace tùy chọn

Muốn dùng workspace khác, có 2 cách.

Cách 1: đặt workspace ngay lúc khởi tạo

```powershell
nanobot onboard --workspace E:\WORKSPACES\nanobot1
```

Cách 2: override workspace khi chạy

```powershell
nanobot gateway --web --workspace E:\WORKSPACES\nanobot1
```

Hoặc chat trực tiếp với agent:

```powershell
nanobot agent -m "Hello" --workspace E:\WORKSPACES\nanobot1
```

Bạn cũng có thể dùng đường dẫn tương đối hoặc thư mục home của user hiện tại.

Ví dụ đường dẫn tương đối:

```powershell
nanobot onboard --workspace .\my-workspace
nanobot gateway --web --workspace .\my-workspace
```

Lưu ý: đường dẫn tương đối sẽ được tính theo thư mục hiện tại nơi bạn chạy lệnh.

Ví dụ muốn lưu ở thư mục kiểu `C:\Users\myname\.nanobot2` nhưng không hardcode tên user:

```powershell
nanobot onboard --workspace "$HOME\.nanobot2"
nanobot gateway --web --workspace "$HOME\.nanobot2"
nanobot agent -m "Hello" --workspace "$HOME\.nanobot2"
```

Hoặc dùng cách viết với `~`:

```powershell
nanobot onboard --workspace "~/.nanobot2"
```

Khuyến nghị:
- Dùng đường dẫn tương đối nếu bạn luôn chạy lệnh từ cùng một thư mục.
- Dùng `"$HOME\..."` hoặc `"~/..."` nếu muốn đường dẫn ổn định theo user hiện tại.
- Dùng đường dẫn tuyệt đối nếu bạn chạy qua script, shortcut, task scheduler hoặc service.

## Ghi chú ngắn

- `nanobot onboard --workspace ...` sẽ ghi workspace vào `config.json`.
- `nanobot gateway --workspace ...` và `nanobot agent --workspace ...` là override lúc chạy.
- Workspace thường chứa các thư mục như `skills/`, `memory/`, `sessions/`, `cron/`.

## Launcher nhiều bot

Nếu bạn muốn tạo và chạy nhiều bot local mà không dùng Docker, có thể dùng script:

```powershell
.\nanobot-launcher.ps1
```

Hoặc nếu đang dùng `cmd`:

```cmd
nanobot-launcher.cmd
```

Script này hỗ trợ:
- tạo bot mới với `workspace` riêng
- nhập `web port` riêng cho từng bot
- chấp nhận workspace kiểu tuyệt đối, tương đối, `~/.nanobot2`, hoặc `"$HOME\.nanobot2"`
- lưu danh sách bot đã tạo vào `.launcher\bots.json`
- chạy lại bot cũ và tự dùng đúng `config` cùng `web port` đã lưu
- mở mỗi bot trong một cửa sổ PowerShell riêng để bạn có thể chạy nhiều bot cùng lúc
