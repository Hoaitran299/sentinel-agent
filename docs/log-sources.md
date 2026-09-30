# Nguồn log (local, server khác, S3, Cloudflare R2)

Watcher chỉ cần **đọc** log Laravel. Chọn nguồn bằng `AI_FIX_LOG_SOURCE` trong `.env`
backend; mọi nguồn dùng chung parser, threshold và cursor (lưu trong bảng `cursors`, key
không bao giờ chứa credential). Đổi nguồn xong restart `make watch`; lần đầu với nguồn mới
watcher bắt đầu từ cuối log (không xử lý lỗi cũ).

| `AI_FIX_LOG_SOURCE` | Dùng khi | Poll |
| --- | --- | --- |
| `file` (mặc định) | Laravel và backend cùng máy | mỗi `poll_interval_seconds` (1s) |
| `http` | Log ở server khác, expose qua nginx/HTTP có auth | `AI_FIX_LOG_POLL_SECONDS` (10s) |
| `ssh` | Log ở server khác, runner có SSH key vào server đó | `AI_FIX_LOG_POLL_SECONDS` (10s) |
| `s3` | Log được đẩy lên AWS S3, Cloudflare R2, MinIO, DigitalOcean Spaces... | tối thiểu 10s |

Nguồn remote tạm lỗi (mạng, 5xx, SSH timeout) không làm dừng watcher: terminal/timeline in
`Log source unavailable; retrying` một lần, và `Log source recovered` khi đọc lại được.

## `file`

```env
AI_FIX_LOG_SOURCE=file
AI_FIX_LOG_PATH=            # để trống = log_path trong config/demo.json
```

Theo dõi inode + byte offset, tự đọc lại từ đầu khi log bị rotate.

## `http` — server khác qua HTTP Range

Expose **chỉ file log** qua HTTPS có xác thực, ví dụ nginx trên app server:

```nginx
location = /internal/laravel.log {
    alias /var/www/app/storage/logs/laravel.log;
    default_type text/plain;
    if ($http_authorization != "Bearer <token-dài-ngẫu-nhiên>") { return 401; }
    allow 203.0.113.10;   # IP runner
    deny all;
}
```

```env
AI_FIX_LOG_SOURCE=http
AI_FIX_LOG_HTTP_URL=https://app.example.com/internal/laravel.log
AI_FIX_LOG_HTTP_TOKEN=<token-dài-ngẫu-nhiên>
# hoặc Basic auth:
AI_FIX_LOG_HTTP_USERNAME=
AI_FIX_LOG_HTTP_PASSWORD=
```

Mỗi lần poll: `HEAD` lấy kích thước, rồi `GET Range: bytes=<offset>-` chỉ lấy phần mới
(tối đa 4 MB/lần). File nhỏ lại được coi là đã rotate. Không đặt user/password trong URL
(bị từ chối); query string bị bỏ khỏi label/cursor.

## `ssh` — tail file trên server khác

```env
AI_FIX_LOG_SOURCE=ssh
AI_FIX_LOG_SSH_HOST=10.0.0.5
AI_FIX_LOG_SSH_USER=logreader
AI_FIX_LOG_SSH_PORT=22
AI_FIX_LOG_SSH_KEY=~/.ssh/aifix_log_reader
AI_FIX_LOG_SSH_PATH=/var/www/app/storage/logs/laravel.log
```

Chuẩn bị một lần:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/aifix_log_reader -N ""
ssh-copy-id -i ~/.ssh/aifix_log_reader.pub logreader@10.0.0.5
ssh-keyscan -H 10.0.0.5 >> ~/.ssh/known_hosts
```

Khuyến nghị user `logreader` chỉ có quyền đọc thư mục log. Mỗi poll là **một** lệnh
`ssh -o BatchMode=yes` (không bao giờ hỏi password) chạy `stat` + `tail -c`; hỗ trợ cả GNU
và BSD `stat`. Host key phải có trong `known_hosts`.

## `s3` — AWS S3 / Cloudflare R2 / MinIO

```bash
make install-s3   # cài boto3
```

Hai kiểu:

- **Một object** (`AI_FIX_LOG_S3_KEY`): file log được upload/sync đè định kỳ (vd. cron
  `aws s3 cp storage/logs/laravel.log s3://bucket/app/laravel.log`). Watcher đọc phần tăng
  thêm bằng Range.
- **Prefix** (`AI_FIX_LOG_S3_PREFIX`): log shipper (Vector, Fluent Bit, Promtail...) ghi
  object mới liên tục. Mỗi object được đọc một lần theo thứ tự key, object `.gz` được giải
  nén. Key phải tăng dần theo thời gian (vd. `app/2026/09/28/10-15-00.log.gz`).

AWS S3:

```env
AI_FIX_LOG_SOURCE=s3
AI_FIX_LOG_S3_BUCKET=company-logs
AI_FIX_LOG_S3_PREFIX=laravel/production/
AI_FIX_LOG_S3_REGION=ap-southeast-1
AI_FIX_LOG_S3_ACCESS_KEY_ID=AKIA...
AI_FIX_LOG_S3_SECRET_ACCESS_KEY=...
```

Cloudflare R2 (API S3-compatible, tạo R2 API token quyền **Object Read** cho bucket):

```env
AI_FIX_LOG_SOURCE=s3
AI_FIX_LOG_S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
AI_FIX_LOG_S3_REGION=auto
AI_FIX_LOG_S3_BUCKET=laravel-logs
AI_FIX_LOG_S3_KEY=production/laravel.log
AI_FIX_LOG_S3_ACCESS_KEY_ID=...
AI_FIX_LOG_S3_SECRET_ACCESS_KEY=...
```

MinIO / DigitalOcean Spaces: như R2, đổi `AI_FIX_LOG_S3_ENDPOINT`. Để trống access key thì
boto3 dùng credential chain mặc định (IAM role, `~/.aws/credentials`).

Quyền tối thiểu: `s3:ListBucket` (prefix mode), `s3:GetObject`. Không cần quyền ghi.

> Cloudflare Logpush xuất log HTTP của Cloudflare, không phải Laravel log. Muốn dùng hạ tầng
> Cloudflare, đẩy file Laravel log lên R2 như trên.

## Bảo mật

- Token/secret nguồn log chỉ nằm trong `.env` backend, không đi vào Claude hoặc
  verification (process environment dùng allowlist), không nằm trong cursor/dashboard.
- Nội dung log vẫn đi qua sanitizer như nguồn local trước khi lưu hoặc gửi cho Claude.
- Code để sửa vẫn lấy từ Git (`source_repo`/GitHub), không lấy từ server log.
