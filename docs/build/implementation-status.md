# Implementation Status

## Implemented và verified

- [x] Read-only Laravel log tailer với persisted cursor.
- [x] Nguồn log: file local, HTTP Range, SSH, S3/Cloudflare R2/MinIO (key hoặc prefix, `.gz`).
- [x] Bảng `settings` + dashboard Cài đặt: chế độ `report_and_fix`/`fix_only`/`report_only`,
  ngưỡng, cửa sổ; watcher áp dụng không cần restart; incident `reported` + nút Chạy AI fix.
- [x] Sanitization/fingerprint/filter.
- [x] MySQL state store riêng (SQLite fallback cho test); threshold/window/dedupe; không Redis.
- [x] Auto-migrate schema + `import-sqlite` idempotent từ state cũ.
- [x] `make db-init`: tạo database/user MySQL riêng, sinh password, ghi `.env`, idempotent.
- [x] Thông báo Telegram (started/succeeded/failed/pr_merged/pr_closed/repeated), đa ngôn ngữ,
  `make telegram-test`, token được che trong mọi lỗi.
- [x] `run_events` timeline cho mọi stage.
- [x] Dashboard web local: overview, stepper, timeline, verification, report, PR, Retry.
- [x] Dashboard hardening: loopback-only, Host check, CSRF header, strict CSP.
- [x] Report/PR/phân tích Claude đa ngôn ngữ: `AI_FIX_REPORT_LANGUAGE=en|vi|ja`.
- [x] Chống fix lặp lại: lỗi tái diễn khi fix chưa merge/deploy được gắn vào incident cũ;
  đồng bộ PR merged/closed; lệnh/nút release.
- [x] Nút Xóa dữ liệu trên dashboard (+ `clear --yes`): xác nhận gõ `CLEAR`, auto backup
  JSON, chặn khi agent đang chạy, giữ log cursor, tùy chọn xóa report.
- [x] Claude CLI preflight.
- [x] Remote GitHub base fetch qua private ref.
- [x] Independent Composer/npm/Vite bootstrap.
- [x] Existing-test hash baseline trước write.
- [x] Restricted read-only structured analysis.
- [x] Scoped write không Bash/Web.
- [x] Protected-path, file-count, diff-line và new-test guard.
- [x] Trusted Pint/PHPUnit/npm/diff verification.
- [x] Exact-tree commit.
- [x] Trusted push và idempotent draft PR creation.
- [x] Failure report + preserved diff + partial verification.
- [x] Explicit retry với unique run/worktree/branch.
- [x] 98 backend tests pass (7 test storage chạy thêm trên MySQL 8.4 khi có `AI_FIX_TEST_DATABASE_URL`).
- [x] End-to-end PR #1.

## Current limitations

- Single watcher process; chưa có distributed lock cho nhiều host.
- Chưa có adapter Loki/Sentry/CloudWatch/Elasticsearch (có file/http/ssh/s3).
- S3 prefix mode yêu cầu key tăng dần theo thời gian.
- Worktree/Claude restricted mode chưa thay thế container/VM sandbox.
- PAT được hỗ trợ; GitHub App chưa implement.
- Không auto-merge/deploy.
- Dashboard không có đăng nhập; chỉ dùng qua loopback/SSH tunnel.
- Run bị kẹt ở `running` sau khi watcher crash chưa được tự phát hiện.

## Recommended next work

- systemd/health heartbeat và stale-run detection;
- retention/orphan branch cleanup;
- container/user/network resource isolation;
- GitHub App credential broker;
- multi-repository policy model.
