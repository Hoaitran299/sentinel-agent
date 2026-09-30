# Dashboard và MySQL state store

## Tổng quan

```text
make watch (watcher, process duy nhất có Claude/Git/GitHub)
  -> ghi incident, occurrence, run, run_events vào MySQL `ai_fix_orchestrator`
make ui (dashboard, http://localhost:8787)
  -> đọc MySQL, poll mỗi 2 giây
  -> nút Retry chỉ đổi incident failed -> queued; watcher sẽ chạy nó
  -> nút "Cho phép fix lại" đổi incident có fix (chờ review/merged/PR đóng) -> closed
```

Dashboard không gọi Claude, không tạo worktree và không dùng GitHub token. Nó chỉ đọc
state và đưa incident vào hàng đợi. Muốn dashboard hoạt động thì không cần watcher đang
chạy, nhưng incident được retry chỉ thật sự chạy khi `make watch` đang mở.

## 1. Tạo database MySQL riêng

Dùng database và user riêng, không dùng chung database/user của Laravel. Cách nhanh nhất:

```bash
make install
make db-init
```

`make db-init` sẽ:

1. hỏi tài khoản MySQL admin (mặc định `root`, password nhập ẩn; để trống nếu không có);
2. nếu `.env` chưa có `AI_FIX_DATABASE_URL`: sinh user `aifix` với password ngẫu nhiên và
   dùng database `ai_fix_orchestrator` trên cùng server admin;
3. `CREATE DATABASE/USER IF NOT EXISTS`, cấp quyền tối thiểu (xem SQL bên dưới) cho
   `aifix@127.0.0.1` và `aifix@localhost`;
4. ghi `AI_FIX_DATABASE_URL` vào `.env` (mode `0600`), không in password ra terminal;
5. đăng nhập bằng user mới và tạo bảng.

Chạy lại an toàn: nếu `.env` đã có URL, lệnh dùng đúng user/password/database đó, đồng bộ
lại password và quyền. Không muốn nhập tay:

```bash
AI_FIX_DB_ADMIN_URL=mysql://root:<password>@127.0.0.1:3306 make db-init
```

Biến tùy chọn `AI_FIX_DB_USER_HOST` đổi host của user (mặc định loopback cho MySQL local,
`%` cho MySQL ở host khác; MySQL trong Docker thường cần `%`). Admin password không được
lưu ở đâu cả.

Tạo thủ công tương đương, ví dụ với MySQL ở `127.0.0.1:3306`:

```sql
CREATE DATABASE ai_fix_orchestrator CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'aifix'@'127.0.0.1' IDENTIFIED BY '<mật-khẩu-mạnh>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON ai_fix_orchestrator.* TO 'aifix'@'127.0.0.1';
```

Nếu user có quyền `CREATE`, orchestrator tự tạo database khi chưa có. Bảng được tạo/migrate
tự động khi `watch`, `ui`, `status`, `preflight` khởi động: `cursors`, `incidents`,
`occurrences`, `runs`, `run_events`.

## 2. Cấu hình

Thêm vào `.env` của backend (mode `0600`):

```env
AI_FIX_DATABASE_URL=mysql://aifix:<url-encoded-password>@127.0.0.1:3306/ai_fix_orchestrator
```

Password có ký tự đặc biệt phải URL-encode (`@` → `%40`, `:` → `%3A`, `/` → `%2F`).
Nếu biến này trống, hệ thống fallback về SQLite `state_db`; chỉ nên dùng cho unit test.
Dòng `State store connected | database=mysql://aifix@...` ở đầu `make watch` cho biết
backend đang dùng database nào (password không bao giờ được in ra).

`AI_FIX_DATABASE_URL` không đi vào Claude hoặc verification commands vì process
environment dùng allowlist.

## 3. Cài dependency và chạy

```bash
make install        # tạo .venv, cài PyMySQL
make db-init        # nếu chưa làm ở bước 1
make test
make import-sqlite  # tùy chọn: chép state SQLite cũ (var/state.sqlite3) sang MySQL
```

Mở hai terminal:

```bash
make watch
make ui             # http://localhost:8787
```

`import-sqlite` idempotent (`INSERT IGNORE`), chạy lại không tạo bản ghi trùng. Chỉ chạy
khi watcher đang dừng.

## 4. Dashboard hiển thị gì

- Pill trạng thái: watcher online / im lặng / chưa chạy, loại database, threshold, base
  branch, repository nhận PR.
- Bộ đếm: tổng incident, đang gom lỗi, đang chạy, chờ review, thất bại.
- Danh sách incident có bộ lọc và thanh tiến độ `count/threshold`.
- Chi tiết incident:
  - stepper `OBSERVE → TRIGGER → ANALYZE → GUARD → FIX → VERIFY → GIT → DONE`, đánh dấu đỏ
    stage bị fail;
  - lý do dừng, commit, branch, link PR;
  - phân tích có cấu trúc của Claude (root cause, đề xuất, test bắt buộc, rủi ro);
  - kết quả từng verification command;
  - timeline mọi stage event của incident;
  - các lần chạy agent và nút xem Markdown report/failure report;
  - các log occurrence đã sanitize;
  - số lần lỗi lặp lại sau khi đã có fix (`+N sau fix`) và giải thích vì sao không chạy lại.
- Nút "Cho phép fix lại": ngừng gom lỗi vào incident đã có fix, để lần lỗi tiếp theo được
  phép tạo fix mới. Dùng khi fix đã deploy mà lỗi vẫn còn, hoặc PR bị đóng và muốn thử lại.
- Live activity: 60 event mới nhất của toàn hệ thống.

Trạng thái watcher suy ra từ heartbeat: watcher lưu log cursor ở mỗi lần poll. Trong lúc
agent đang chạy, watcher không poll nên pill hiện "im lặng"; đó là bình thường nếu có
incident ở trạng thái "Agent đang chạy".

## 5. Cài đặt xử lý lỗi

Nút **Cài đặt** trên thanh trên cùng chỉnh các giá trị lưu trong bảng `settings`. Watcher
đọc lại settings ở **mỗi lần poll**, nên thay đổi có hiệu lực ngay, không cần restart.

| Setting | Giá trị | Ý nghĩa |
| --- | --- | --- |
| Chế độ | `report_and_fix` (mặc định) | Đạt ngưỡng → gửi Telegram và chạy AI fix tự động |
| | `fix_only` | Chạy AI fix tự động, **không** gửi Telegram (vẫn xem trên dashboard) |
| | `report_only` | Chỉ gửi Telegram (`reported`), **không** chạy agent. Incident ở trạng thái "Đã báo cáo" |
| Số lần lỗi | 1–1000 | Ngưỡng cùng một lỗi (cùng fingerprint) |
| Trong khoảng | 10–604800 giây | Cửa sổ gom lỗi |

Với incident "Đã báo cáo":

- **Chạy AI fix**: đưa vào hàng đợi, watcher chạy agent (bất kể chế độ hiện tại).
- **Đóng incident**: bỏ qua; lỗi xuất hiện lại sẽ tạo incident mới.
- Lỗi lặp lại được gom vào incident đó, không gửi báo cáo trùng.

Ngưỡng mới áp dụng cả cho incident đang gom lỗi. Mỗi lần lưu, timeline ghi
`Settings updated from dashboard` kèm giá trị cũ → mới. Giá trị mặc định (khi bảng trống)
lấy từ `mode`, `threshold`, `window_seconds` trong `config/demo.json`.

## 6. Xóa dữ liệu

Nút **Xóa dữ liệu** ở góc phải thanh trên cùng xóa toàn bộ incident, occurrence, run và
timeline để bắt đầu demo lại từ đầu.

- Phải gõ `CLEAR` vào hộp xác nhận thì nút mới bấm được.
- Trước khi xóa, toàn bộ state được sao lưu ra `var/backups/state-<thời-gian>.json`
  (mode `0600`).
- Bị từ chối (HTTP 409, không tạo backup) nếu có incident đang ở trạng thái agent đang chạy.
- Log cursor được giữ nên watcher không đọc lại lỗi cũ trong log.
- Tùy chọn xóa luôn report: chỉ xóa file do orchestrator tạo (`<run-id>.md`,
  `<run-id>-failure.md`) trong `var/reports`; file khác được giữ nguyên.
- Không đụng tới branch `ai-fix/*` hay Pull Request trên GitHub.

CLI tương đương:

```bash
PYTHONPATH=src .venv/bin/python -m autofix_agent clear --config config/demo.json --yes [--delete-reports]
```

Khôi phục: file backup chứa đủ các bảng dưới dạng JSON, có thể dùng để tra cứu hoặc nạp lại
thủ công.

## 7. Bảo mật của dashboard

Dashboard không có đăng nhập, nên:

- chỉ bind `127.0.0.1` (config từ chối host khác loopback);
- từ chối request có `Host` header lạ (chống DNS rebinding);
- mọi thao tác ghi (Retry/Chạy AI fix, Cho phép fix lại, Cài đặt, Xóa dữ liệu) cần header `X-AI-Fix-Request: 1`
  và cùng origin (chống CSRF); Xóa dữ liệu còn cần body `{"confirm": "CLEAR"}`;
- CSP `default-src 'self'`, không load script/font từ CDN;
- report chỉ đọc theo run ID hex trong `reports_dir`, không nhận path từ client.

Trên VPS, truy cập qua SSH tunnel thay vì mở port:

```bash
ssh -L 8787:127.0.0.1:8787 aifix@runner-host
```

## 8. Test với MySQL thật

Unit test mặc định chạy bằng SQLite. Để chạy cùng bộ test storage trên MySQL, dùng một
database dùng một lần (test sẽ **drop** các bảng orchestrator):

```bash
docker run -d --rm --name aifix-mysql-test -e MYSQL_ROOT_PASSWORD=testroot \
  -e MYSQL_DATABASE=ai_fix_test -p 127.0.0.1:3307:3306 mysql:8.4
AI_FIX_TEST_DATABASE_URL=mysql://root:testroot@127.0.0.1:3307/ai_fix_test make test-mysql
docker stop aifix-mysql-test
```
