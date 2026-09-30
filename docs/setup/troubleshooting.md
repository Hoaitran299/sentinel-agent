# Troubleshooting và retry

## `GitHub API returned 401: Bad credentials`

Nguyên nhân là authentication, chưa phải repository permission. Kiểm tra mà không in token:

- backend `.env` tồn tại và mode `0600`;
- token đầy đủ, không có whitespace;
- prefix `github_pat_` (khuyến nghị) hoặc `ghp_`;
- token chưa expire/revoke;
- watcher đã restart sau khi sửa `.env`.

Chạy lại:

```bash
PYTHONPATH=src .venv/bin/python -m autofix_agent preflight --config config/demo.json
```

## `Vite manifest not found` trong PHPUnit

Fresh worktree không có `public/build/manifest.json`. Bootstrap hiện chạy:

```text
npm ci --ignore-scripts
npm run build          # baseline manifest trước write
```

Verification vẫn chạy `npm run build` lần nữa sau agent. Nếu lỗi quay lại, kiểm tra
`package-lock.json`, Node/npm version và stdout trong failure report.

## `php artisan test` fail nhưng terminal chỉ hiện stack

Phiên bản hiện tại ưu tiên các dòng `FAIL`, `Exception`, `Expected`, `Failed asserting`,
`Vite manifest` và `Tests:` trong error summary. Full bounded stdout/stderr luôn nằm trong:

```text
var/reports/<run-id>-failure.md
```

## Worktree đã cleanup, xem diff ở đâu?

Failure report lưu cả `git diff` tracked và patch của untracked new files trước cleanup.
Branch attempt được giữ nhưng chưa có commit nếu verification fail; report mới là artifact
chẩn đoán của uncommitted tree.

## Retry incident

```bash
make status
PYTHONPATH=src .venv/bin/python -m autofix_agent retry \
  --config config/demo.json \
  --incident <unique-prefix>
```

Chỉ incident `failed` mới retry được. Run mới re-fetch base, chạy Claude/guards/verification
đầy đủ và dùng suffix branch mới.

## PR không được tạo

Theo thứ tự kiểm tra:

1. `AI_FIX_PUBLISH_PR=true`.
2. Preflight GitHub pass.
3. Verification cả bốn command pass.
4. Parent của verified commit trùng current remote base SHA.
5. Token có Contents + Pull requests write.
6. Branch protection cho phép bot push `ai-fix/*`.

## Không kết nối được MySQL

- `Access denied`: sai user/password, hoặc password có ký tự đặc biệt chưa URL-encode.
- `Unknown database` và user không có quyền `CREATE`: chạy `make db-init` với tài khoản
  admin, hoặc tạo thủ công theo [dashboard.md](dashboard.md).
- `make db-init` báo `Access denied for user 'root'`: sai admin password; MySQL cài qua
  Homebrew/Docker mặc định có thể để trống hoặc dùng `MYSQL_ROOT_PASSWORD`.
- Đăng nhập bằng `aifix` bị `Access denied` dù db-init thành công: server thấy client ở host
  khác (Docker bridge). Chạy lại với `AI_FIX_DB_USER_HOST=% make db-init`.
- `Can't connect`: MySQL chưa chạy hoặc sai host/port; kiểm tra `nc -z 127.0.0.1 3306`.
- `requires PyMySQL`: chạy `make install` và dùng `.venv/bin/python` (Makefile tự chọn).
- Watcher in `database=sqlite:...`: `AI_FIX_DATABASE_URL` chưa được set trong `.env`.

## Dashboard

- Pill "Watcher chưa chạy": chưa có cursor nào; start `make watch`.
- Pill "Watcher im lặng": watcher dừng, hoặc đang chạy agent (không poll trong lúc chạy).
- Bấm Retry nhưng incident đứng ở "Chờ chạy": watcher chưa chạy; start `make watch`.
- `403 Host not allowed`: mở bằng `http://localhost:8787` hoặc `http://127.0.0.1:8787`,
  không qua hostname khác. Trên VPS dùng SSH tunnel.
- Port bận: `PYTHONPATH=src .venv/bin/python -m autofix_agent ui --config config/demo.json --port 8790`.

## Lỗi vẫn xuất hiện nhưng agent không fix lại

Đây là hành vi mong muốn: fingerprint đã có fix đang chờ review, đã merge (có thể chưa
deploy) hoặc PR bị đóng. Terminal in `Error repeated but a fix already exists`, dashboard
hiện `+N sau fix`. Khi fix đã deploy mà lỗi vẫn còn, hoặc muốn agent thử lại:

```bash
PYTHONPATH=src .venv/bin/python -m autofix_agent release --config config/demo.json --incident <prefix>
```

hoặc bấm "Cho phép fix lại" trên dashboard. Trạng thái `merged/dismissed` chỉ được đồng bộ
khi publisher bật (`AI_FIX_PUBLISH_PR=true`); ở local-only mode incident giữ
`waiting_for_review` tới khi release.

## Không nhận được Telegram

- `make telegram-test` báo `401`: token sai hoặc đã bị revoke qua @BotFather.
- `400 chat not found` / `403 bot was kicked`: sai `AI_FIX_TELEGRAM_CHAT_ID` hoặc bot chưa
  nằm trong group/channel (channel cần quyền đăng bài).
- Test được nhưng không có tin khi chạy thật: kiểm tra `AI_FIX_TELEGRAM_EVENTS`, và restart
  `make watch` sau khi sửa `.env`.
- `pr_merged`/`pr_closed` chỉ có khi `AI_FIX_PUBLISH_PR=true`.

## Đạt ngưỡng nhưng agent không chạy

Kiểm tra chế độ trên dashboard (pill "Chế độ"). Ở `report_only` incident chuyển "Đã báo
cáo" và chỉ gửi Telegram; bấm **Chạy AI fix** để sửa. Ở `fix_only` agent vẫn chạy nhưng
không có Telegram.

## Nguồn log remote

- `Log source unavailable; retrying`: watcher vẫn chạy, sẽ tự đọc lại khi nguồn khả dụng.
- `http` báo `401/403`: sai `AI_FIX_LOG_HTTP_TOKEN` hoặc IP runner chưa được allow.
  Server phải hỗ trợ `Range` (nginx static file có sẵn).
- `ssh` báo `Host key verification failed`: chạy `ssh-keyscan -H <host> >> ~/.ssh/known_hosts`
  dưới user chạy watcher. `Permission denied (publickey)`: kiểm tra `AI_FIX_LOG_SSH_KEY`.
- `s3` báo `requires boto3`: `make install-s3`. `AccessDenied`: key cần `s3:GetObject`
  (và `s3:ListBucket` với prefix mode). R2 cần `AI_FIX_LOG_S3_REGION=auto` và endpoint
  `https://<account-id>.r2.cloudflarestorage.com`.
- S3 prefix mode bỏ sót object: key phải tăng dần theo thời gian (watcher đọc theo thứ tự key).

## Watcher không thấy log mới

- Kiểm tra `log_path` trong `config/demo.json`.
- Watcher lần đầu bắt đầu ở EOF; phải phát sinh lỗi sau khi watcher start.
- Kiểm tra include regex có match sanitized header.
- Log rotation phải giữ quyền đọc cho runner user.
- Dùng `--from-start --once` chỉ cho kiểm thử; có thể xử lý nhiều lỗi lịch sử.
