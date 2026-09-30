# External AI Bug-Fix Orchestrator

Project này theo dõi log Laravel từ bên ngoài. Laravel không chạy Claude, không tạo
worktree và không cần Redis/queue/agent tables. Toàn bộ threshold, trạng thái, workspace,
verification và report thuộc project này. State lưu trong MySQL database riêng
(`ai_fix_orchestrator`, user/pass `sentinel`/`sentinel`) và có dashboard web local để theo dõi/retry.

Companion Laravel application:

```text
../sentinel-demo-app
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

## Chạy demo (Docker)

Project chỉ hỗ trợ chạy bằng Docker. `compose.yaml` nằm ở companion Laravel
(`../sentinel-demo-app`) và chạy cả MySQL, Laravel, watcher, dashboard; source project này
được bind-mount vào `/app`. Hướng dẫn đầy đủ: `../sentinel-demo-app/docs/setup/docker.md`.

```bash
cp .env.example .env && chmod 600 .env   # điền AI_FIX_GITHUB_TOKEN / Telegram nếu cần

cd ../sentinel-demo-app
docker compose up -d --build
docker compose run --rm watcher claude   # lần đầu: /login rồi /exit
docker compose exec watcher python -m autofix_agent preflight --config config/docker.json
docker compose logs -f watcher
```

- MySQL: database `ai_fix_orchestrator`, user `sentinel`, password `sentinel`
  (`AI_FIX_DATABASE_URL` do compose đặt). Laravel dùng database `sentinel` cùng user.
- Dashboard: http://localhost:8787
- Bật tạo PR: đặt `AI_FIX_PUBLISH_PR=true` trong `.env` rồi `docker compose restart watcher`.
- Chọn ngôn ngữ report/PR bằng `AI_FIX_REPORT_LANGUAGE=en|vi|ja`
  ([chi tiết](docs/setup/configuration.md#ngôn-ngữ-report)).

Thao tác lỗi profile ba lần trong Laravel; log watcher và dashboard sẽ hiện các stage
`OBSERVE`, `TRIGGER`, `ANALYZE`, `GUARD`, `FIX`, `VERIFY`, `GIT` và `DONE`.

```bash
docker compose exec watcher python -m autofix_agent status --config config/docker.json
docker compose exec watcher python -m autofix_agent retry --config config/docker.json --incident <incident-prefix>
docker compose exec watcher python -m unittest discover -s tests   # test suite
```

Run thất bại lưu failure report và preserved diff trước khi cleanup; có thể retry bằng nút
**Retry incident** trên dashboard hoặc lệnh `retry` ở trên. Watcher mặc định bắt đầu từ
cuối log ở lần chạy đầu để không xử lý lại lỗi cũ.

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

Xem [docs/README.md](docs/README.md). Tài liệu chia 3 nhóm:

- **Build** — thiết kế và xây dựng dự án:
  [kiến trúc](docs/build/architecture.md),
  [trạng thái implementation](docs/build/implementation-status.md)
- **Setup** — cài đặt, cấu hình, vận hành:
  [Docker](docs/setup/docker.md),
  [cấu hình và biến môi trường](docs/setup/configuration.md),
  [dashboard và MySQL state store](docs/setup/dashboard.md),
  [GitHub token và PR publisher](docs/setup/github-publishing.md),
  [nguồn log](docs/setup/log-sources.md),
  [Telegram](docs/setup/telegram.md),
  [troubleshooting và retry](docs/setup/troubleshooting.md),
  [deploy Ubuntu VPS](docs/setup/deployment-ubuntu-vps.md)
- **Q&A** — câu hỏi về kiến trúc: [architecture Q&A](docs/qa/architecture-qa.md)
