# Cấu hình và biến môi trường

## File cấu hình không chứa secret

`config/demo.json` định nghĩa đường dẫn source/log, threshold, worktree, Claude binary,
writable/protected paths, verification commands, dashboard và metadata GitHub. File này
có thể commit; connection string MySQL nằm trong `.env`.

Các giá trị chính:

| Key | Mặc định demo | Ý nghĩa |
| --- | --- | --- |
| `source_repo` | Laravel project | Repository chỉ được đọc và dùng làm Git worktree source |
| `log_path` | `storage/logs/laravel.log` | File watcher mở read-only |
| `base_branch` | `master` | GitHub PR base; publish mode fetch vào private ref |
| `mode` | `report_and_fix` | Chế độ mặc định; chỉnh lúc chạy qua dashboard (bảng `settings`) |
| `threshold` | `3` | Ngưỡng mặc định; dashboard ghi đè |
| `window_seconds` | `600` | Cửa sổ gom lỗi mặc định; dashboard ghi đè |
| `database_url_env` | `AI_FIX_DATABASE_URL` | Tên biến `.env` chứa MySQL URL |
| `state_db` | `var/state.sqlite3` | SQLite fallback khi biến trên trống; nguồn mặc định của `import-sqlite` |
| `ui_host` | `127.0.0.1` | Dashboard chỉ cho phép loopback |
| `ui_port` | `8787` | Port dashboard |
| `pr_sync_interval_seconds` | `60` | Chu kỳ watcher hỏi GitHub PR đã merge/đóng chưa |
| `report_language` | `en` | Ngôn ngữ report mặc định; bị `AI_FIX_REPORT_LANGUAGE` ghi đè |
| `workspace_root` | `var/worktrees` | Managed worktree root của backend |
| `bootstrap_dependencies` | `true` | Cài dependency riêng từ lock files |
| `allowed_paths` | profile code/tests | Phạm vi Claude được sửa |
| `protected_paths` | config/dependency/CI/auth | Phạm vi luôn bị chặn |
| `max_changed_files` | `8` | Số file tối đa agent được đổi |
| `max_diff_lines` | `500` | Số dòng diff tối đa |
| `require_new_test` | `true` | Fix bắt buộc thêm regression test mới |
| `include_patterns` | regex lỗi `phone` | Chỉ log entry match mới được tính occurrence |
| `start_at_end` | `true` | Lần chạy đầu bắt đầu ở EOF, không xử lý lỗi cũ |
| `claude_binary` | absolute path `claude` | Phải sửa theo máy/runner user; kiểm tra bằng preflight |
| `claude_timeout_seconds` | `900` | Timeout mỗi lần gọi Claude |
| `claude_max_budget_usd` | `3` | Budget tối đa mỗi lần gọi Claude |
| `verification_commands` | Pint/PHPUnit/npm/diff | Trusted commands; không lấy từ model/log |
| `publish_pull_request` | `false` | Safe default; `.env` có thể bật |
| `github_draft` | `true` | PR local demo được tạo dạng draft |

## `.env` chỉ thuộc backend

Copy `.env.example` thành `.env`. Không đặt token vào Laravel `.env` hoặc
`config/demo.json`.

| Biến | Bắt buộc | Mô tả |
| --- | --- | --- |
| `AI_FIX_DATABASE_URL` | Có | `mysql://user:pass@host:3306/ai_fix_orchestrator`; password URL-encode |
| `AI_FIX_LOG_SOURCE` | Không | `file` (mặc định), `http`, `ssh`, `s3`; biến chi tiết xem [log-sources.md](log-sources.md) |
| `AI_FIX_TELEGRAM_ENABLED` | Không | `true` để gửi thông báo Telegram; xem [telegram.md](telegram.md) |
| `AI_FIX_TELEGRAM_BOT_TOKEN` | Khi bật Telegram | Token bot từ @BotFather |
| `AI_FIX_TELEGRAM_CHAT_ID` | Khi bật Telegram | Chat id số (`-100...`) hoặc `@channel` |
| `AI_FIX_TELEGRAM_EVENTS` | Không | Mặc định `reported,started,succeeded,failed,pr_merged,pr_closed`; thêm `repeated` nếu muốn |
| `AI_FIX_REPORT_LANGUAGE` | Không | `en` (mặc định), `vi` hoặc `ja`; xem mục bên dưới |
| `AI_FIX_PUBLISH_PR` | Có | `true` để push/tạo PR; `false` chỉ tạo local branch/report |
| `AI_FIX_GITHUB_TOKEN` | Khi publish | Fine-grained `github_pat_` khuyến nghị; classic `ghp_` được hỗ trợ |
| `AI_FIX_GITHUB_REPOSITORY` | Khi publish | `owner/repository` |
| `AI_FIX_GITHUB_API_URL` | Khi publish | `https://api.github.com` hoặc GitHub Enterprise API |
| `AI_FIX_GITHUB_GIT_URL` | Khi publish | Clean HTTPS Git URL, tuyệt đối không nhúng token |
| `AI_FIX_GITHUB_DRAFT` | Không | `true` khuyến nghị cho demo |

Parser `.env` chỉ nhận key uppercase đơn giản. Không hỗ trợ shell expansion/command
substitution. Environment của shell có precedence cao hơn `.env`.

Watcher load `.env` một lần khi process khởi động. Sau khi rotate/sửa token phải dừng và
start lại watcher; process đang chạy không hot-reload secret.

## Ngôn ngữ report

`AI_FIX_REPORT_LANGUAGE` chọn ngôn ngữ cho:

- Markdown report thành công và failure report trong `var/reports`;
- tiêu đề và nội dung draft PR (giữ prefix `fix:` theo conventional commit);
- tin nhắn Telegram;
- các trường văn bản Claude trả về (summary, root cause, đề xuất sửa, test, rủi ro), nên
  phần phân tích trong report và trên dashboard cũng theo ngôn ngữ này.

Chấp nhận `en`, `vi`, `ja` và các dạng như `vi-VN`, `ja_JP`. Giá trị khác làm config lỗi
ngay khi khởi động. Không đổi theo ngôn ngữ: commit message, tên branch, path/code/lệnh
trong report, log gốc, output lệnh và thông báo lỗi kỹ thuật (`failure_reason`). Report
cũ không được dịch lại. Đổi ngôn ngữ xong phải restart watcher vì `.env` chỉ được đọc lúc
khởi động.

## State và artifact

- MySQL `ai_fix_orchestrator`: `cursors` (log cursor + heartbeat), `incidents`,
  `occurrences`, `runs`, `run_events` (timeline cho dashboard), `settings` (chế độ/ngưỡng).
- `var/state.sqlite3`: chỉ khi chạy fallback SQLite hoặc state cũ trước khi chuyển MySQL.
- `var/worktrees`: workspace tạm, tự cleanup.
- `var/reports`: Markdown report; giữ lại sau run.

- `var/backups`: bản sao lưu JSON tạo tự động mỗi lần bấm Xóa dữ liệu / chạy `clear`.

Successful report có analysis, fix result, verification và PR URL. Failure report có
command output + preserved diff để chẩn đoán sau khi worktree đã cleanup.

`var/`, `.venv/` và `.env` bị Git ignore. Reset demo nhanh: nút **Xóa dữ liệu** trên dashboard
hoặc `autofix_agent clear --yes` (tự backup, giữ log cursor). Reset toàn bộ kể cả cursor:
dừng watcher, `mysqldump ai_fix_orchestrator > backup.sql`, rồi drop/tạo lại database;
bảng sẽ tự migrate khi start lại.
