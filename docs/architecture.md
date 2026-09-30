# Kiến trúc external log-driven agent

## Mục tiêu

Tách control plane AI khỏi Laravel web host. Ứng dụng chính chỉ chịu trách nhiệm ghi log
như bình thường. Orchestrator đọc file log bằng quyền read-only và sở hữu mọi side effect
liên quan đến AI, Git, dependency bootstrap, verification và report.

## Component

- `LogSource` + `LogTailer`: đọc byte mới từ `file`, `http` (Range), `ssh` hoặc `s3`
  (AWS/R2/MinIO), lưu cursor (identity + offset) để theo dõi append và rotation; parser
  Laravel dùng chung, không nuốt dòng đang ghi dở. Xem [log-sources.md](log-sources.md).
- `RuntimeSettings`: chế độ (`report_and_fix`/`fix_only`/`report_only`), ngưỡng và cửa sổ,
  lưu trong bảng `settings`, watcher đọc lại mỗi poll.
- `StateStore`: MySQL riêng của orchestrator (SQLite chỉ để test/fallback). Threshold,
  dedupe và claim chạy trong transaction được tuần tự hóa bằng MySQL named lock
  `GET_LOCK` (SQLite: `BEGIN IMMEDIATE`).
- `console.stage`: in stage ra terminal và mirror thành `run_events` gắn `incident_id`/`run_id`.
- `Dashboard`: HTTP server stdlib chỉ bind loopback, đọc state để hiển thị; thao tác ghi
  duy nhất là re-queue incident `failed`.
- `GitHubPublisher`: khi publish bật, fetch `refs/heads/master` vào private ref
  `refs/ai-fix/base/master`; token không đi vào remote URL/argv.
- `GitWorktreeManager`: tạo branch từ exact fetched commit (hoặc local master trong
  local-only mode) và chỉ dọn path là child trực tiếp của managed workspace root.
- `ClaudeCodeRunner`: hai capability tách biệt. Analysis chỉ đọc; fix mới được Edit/Write
  sau khi analysis/policy pass. Cả hai không có Bash/Web/MCP/project customizations.
- `WorkspacePolicy`: snapshot hash của mọi tracked test, giới hạn writable paths, số file
  và số dòng; chặn sửa/xóa test cũ và protected paths.
- `AgentPipeline`: trusted owner của verification commands, commit và report.
- `GitHubPublisher`: nhận verified commit, kiểm tra parent/base SHA, push bằng credential
  riêng và tạo/reuse PR idempotently. Component này không nằm trong tool surface Claude.
- `TelegramNotifier`: trusted notifier trong watcher; gửi thông báo theo event, lỗi gửi không
  làm dừng pipeline, bot token không đi vào Claude/report/database/log.

## Trust boundary

Log có thể chứa prompt injection. Repository cũng có thể chứa instruction độc hại trong
comment, README hoặc test. Vì vậy dữ liệu incident luôn nằm trong envelope được đánh dấu
UNTRUSTED; output analysis phải qua JSON schema và path policy trước khi có write. Sau
write, policy chạy trước verification và chạy lại sau từng command vì formatter có thể
đổi tree. Commit được tạo ngay từ exact tree vừa verify.

Claude process chỉ kế thừa allowlist environment (`HOME`, `PATH`, locale...) và test-only
Laravel variables. Telegram token, production database credential và GitHub credential
không đi vào prompt, worktree hoặc process environment.

## Vì sao không dùng database của Laravel

Nếu agent state nằm trong Laravel, web app phải mang migration, queue job và coupling với
runner. Orchestrator dùng database MySQL riêng (`ai_fix_orchestrator`) với user riêng, tự
migrate schema của mình; Laravel không biết đến các bảng này. Redis không cần thiết vì
watcher hiện tại là một process; MySQL transaction là durable source of truth.

MySQL thay SQLite để dashboard và watcher đọc/ghi đồng thời ổn định, backup bằng công cụ
chuẩn và chuẩn bị cho việc runner/dashboard nằm ở process hoặc host khác nhau. Connection
tự reconnect khi MySQL đóng connection idle (`wait_timeout`).

## Dashboard và retry

```text
dashboard POST /api/incidents/<id>/retry
  -> incidents.status: failed -> queued, ghi run_event TRIGGER
watcher loop (sau mỗi lần poll log)
  -> SELECT incident queued -> pipeline.run() -> claim() nguyên tử -> run mới
```

Chỉ watcher giữ capability Claude/Git/GitHub; dashboard không bao giờ chạy agent. Nếu CLI
`retry` và watcher cùng thấy incident `queued`, `claim()` bảo đảm chỉ một process chạy.

## Không fix lặp lại cùng một lỗi

Sau khi agent tạo fix, code đang chạy vẫn còn lỗi cho tới khi PR được merge và deploy, nên
cùng lỗi sẽ tiếp tục xuất hiện trong log. Để không tạo thêm run/branch/PR trùng:

```text
accumulating --(report_only)--> reported --(Chạy AI fix)--> queued
accumulating -> queued -> running -> waiting_for_review --(PR merged)--> merged
                              |                  \--(PR closed)-------> dismissed
                              \-> failed                      (release) -> closed
```

- Khi fingerprint có incident ở `reported`, `waiting_for_review`, `merged` hoặc `dismissed`, occurrence
  mới được gắn vào incident đó (`suppressed_count` tăng) và **không** trigger agent. Việc
  so khớp chỉ theo fingerprint, không theo source commit, vì master có thể đổi trong lúc
  PR còn mở.
- Watcher đồng bộ trạng thái PR mỗi `pr_sync_interval_seconds` (mặc định 60 giây): PR merged
  -> `merged`, PR đóng không merge -> `dismissed`. Lỗi GitHub API chỉ được log, không làm
  dừng watcher.
- Merged vẫn tiếp tục chặn vì merge chưa có nghĩa là đã deploy. Người vận hành bấm
  "Cho phép fix lại" (hoặc `release`) khi chắc chắn fix đã deploy mà lỗi vẫn còn, hoặc
  muốn agent thử lại sau khi PR bị đóng. Incident chuyển `closed`; lần lỗi kế tiếp tạo
  incident mới và đi lại threshold.
- Incident `failed` không chặn: lỗi lặp lại sẽ tạo incident mới và có thể chạy lại.

## Worktree và dependency

Worktree chỉ materialize file được Git track, nên không có `vendor`, `node_modules` hay
`.env`. Orchestrator chạy `composer install --no-scripts`, package discovery có test-only
environment, rồi `npm ci --ignore-scripts`. Nó tạo `.env.testing` tạm bằng key ngẫu nhiên
cho bootstrap rồi xóa trước khi cấp quyền write; verification nhận cùng loại test-only
environment qua process allowlist. Không copy/symlink `.env` Laravel và không dùng writable
dependency tree của web app.

Sau `npm ci`, bootstrap chạy baseline `npm run build` vì Laravel feature tests có thể render
Blade layout và cần `public/build/manifest.json`. Verification vẫn build lại sau khi agent
sửa code, nên artifact baseline không được xem là bằng chứng cho exact tree cuối.

## Publish/PR

Khi publisher bật, nó fetch remote base vào private ref và tạo worktree từ SHA đó; local
checkout/`master` có thể khác mà không đi vào PR. Sau verification và commit, publisher
so parent của verified commit với remote base SHA để tránh PR chứa commit ngoài ý muốn.
Push dùng clean HTTPS URL và temporary `GIT_ASKPASS`; token không xuất hiện trong argv,
Git remote, report hay log. Publisher tìm open PR theo `head + base` trước khi tạo để retry
không sinh PR trùng. Claude không bao giờ nhận credential hoặc tự push.

## Failure và retry

Nếu agent/verification/publish fail, orchestrator ghi `var/reports/<run>-failure.md` chứa
sanitized stdout/stderr, tracked diff và untracked new files trước khi cleanup. Partial
verification được cập nhật vào state store sau từng command pass. Incident chuyển `failed`;
operator có thể retry explicit. Retry tạo run ID, worktree path và branch suffix mới để
không đụng attempt cũ.
