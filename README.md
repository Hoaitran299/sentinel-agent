# External AI Bug-Fix Orchestrator

Project này theo dõi log Laravel từ bên ngoài. Laravel không chạy Claude, không tạo
worktree và không cần Redis/queue/agent tables. Toàn bộ threshold, trạng thái, workspace,
verification và report thuộc project này. State lưu trong MySQL riêng
(`ai_fix_orchestrator`) và có dashboard web local để theo dõi/retry.

Companion Laravel application:

```text
/Users/wake/Documents/Projects/demo-agent-ai-auto-fix-bug
```

## Luồng chạy

```text
Laravel log (read-only): file local | server khác qua HTTP/SSH | S3 / Cloudflare R2
  -> log watcher
  -> fingerprint + threshold trong MySQL (mặc định 3 lỗi / 10 phút, chỉnh trên dashboard)
  -> theo chế độ trong bảng settings:
       report_and_fix: báo Telegram + tự fix | fix_only: tự fix | report_only: chỉ báo
  -> branch/worktree riêng từ master
  -> dependency bootstrap riêng, không copy .env
  -> snapshot test + protected paths
  -> Claude read-only analysis
  -> validate schema + proposed paths
  -> Claude write chỉ trong worktree, không có Bash
  -> diff/test-integrity guard
  -> Pint + PHPUnit + npm build + git diff --check
  -> local verified commit + Markdown report
  -> trusted publisher push branch + tạo/reuse GitHub PR
  -> mọi stage ghi vào run_events -> dashboard http://localhost:8787
  -> thông báo Telegram: bắt đầu fix / thành công / thất bại / PR merged-closed
```

Source checkout Laravel không bị agent sửa trực tiếp. Worktree được dọn sau run nhưng
local branch `ai-fix/...` và report được giữ lại để review. Nếu bật publisher, PR được
tạo bởi process trusted riêng; Claude không bao giờ nhận GitHub token.

## Chạy demo

Chạy toàn bộ bằng Docker: xem `docs/docker.md` trong companion Laravel project
(`docker compose up -d --build` từ thư mục Laravel).

Yêu cầu: Python 3.9+, MySQL 8, Git, Composer, Node/npm và Claude Code đã đăng nhập.

```bash
make install   # .venv + PyMySQL
make db-init   # tạo database/user MySQL riêng, ghi AI_FIX_DATABASE_URL vào .env
```

Chi tiết và cách tạo thủ công xem [hướng dẫn dashboard](docs/dashboard.md). Chọn ngôn ngữ report/PR bằng
`AI_FIX_REPORT_LANGUAGE=en|vi|ja` ([chi tiết](docs/configuration.md#ngôn-ngữ-report)).

Thiết lập GitHub publisher:

```bash
cp .env.example .env
chmod 600 .env
# Điền AI_FIX_GITHUB_TOKEN rồi đặt AI_FIX_PUBLISH_PR=true
```

```bash
make test
PYTHONPATH=src .venv/bin/python -m autofix_agent preflight --config config/demo.json
make watch   # terminal 1
make ui      # terminal 2 -> http://localhost:8787
```

Giữ terminal `make watch` mở, sau đó thao tác lỗi profile ba lần trong Laravel. Terminal
và dashboard sẽ hiện các stage `OBSERVE`, `TRIGGER`, `ANALYZE`, `GUARD`, `FIX`, `VERIFY`,
`GIT` và `DONE`. Xem trạng thái gần nhất trong terminal bằng:

```bash
make status
```

Run thất bại lưu failure report và preserved diff trước khi cleanup. Có thể retry bằng nút
**Retry incident** trên dashboard (watcher sẽ nhận và chạy) hoặc bằng CLI:

```bash
PYTHONPATH=src .venv/bin/python -m autofix_agent retry --config config/demo.json --incident <incident-prefix>
```

Watcher mặc định bắt đầu từ cuối log ở lần chạy đầu để không xử lý lại lỗi cũ. Để đọc
lại log hiện tại từ đầu (chỉ dùng kiểm thử):

```bash
PYTHONPATH=src .venv/bin/python -m autofix_agent watch --config config/demo.json --from-start --once
```

## Ranh giới an toàn

- Log, exception và nội dung repository đều là dữ liệu không tin cậy.
- Claude analysis dùng `--restricted`, chỉ có `Read,Grep,Glob`.
- Write chỉ bật sau khi baseline và policy đã sẵn sàng; Claude không có Bash.
- Existing tests, dependency files, config, routes, CI và authentication bị bảo vệ.
- Verification command do orchestrator cố định, không lấy từ Claude/log.
- Process Claude không nhận environment Laravel/Telegram/GitHub.
- Worktree có `.env.testing` riêng và dependency bootstrap từ lock files.
- Không tự push/PR khi verification chưa pass.
- Dashboard chỉ bind loopback, không có quyền Claude/GitHub; Retry chỉ đưa incident vào hàng đợi.
- Mỗi lỗi chỉ được auto-fix một lần: khi PR chưa merge/deploy, lỗi lặp lại chỉ được ghi nhận
  vào incident cũ; mở lại bằng nút "Cho phép fix lại" hoặc lệnh `release`.

## Evidence end-to-end

- Incident `7dfba14a...` đạt threshold `3/3`.
- Verified commit: `d6987267fc11225b4f319aca264bd9a9a772e3f0`.
- Draft PR: [demo-agent-ai-auto-fix-bug#1](https://github.com/nguyenthanhthuc2000/demo-agent-ai-auto-fix-bug/pull/1).
- Laravel checkout không bị agent sửa trực tiếp.

## Tài liệu

- [Kiến trúc](docs/architecture.md)
- [Dashboard và MySQL state store](docs/dashboard.md)
- [Cấu hình và biến môi trường](docs/configuration.md)
- [GitHub token và PR publisher](docs/github-publishing.md)
- [Chạy đồng thời hai project ở local](docs/local-development.md)
- [Cài đặt chế độ xử lý và ngưỡng](docs/dashboard.md#5-cài-đặt-xử-lý-lỗi)
- [Nguồn log: local, server khác, S3, Cloudflare R2](docs/log-sources.md)
- [Thông báo Telegram](docs/telegram.md)
- [Troubleshooting và retry](docs/troubleshooting.md)
- [Deploy runner Ubuntu VPS](docs/deployment-ubuntu-vps.md)
- [Trạng thái implementation](docs/implementation-status.md)
