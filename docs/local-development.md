# Chạy hai project ở local và tạo PR thật

## Terminal 1 — Laravel

```bash
cd /Users/wake/Documents/Projects/demo-agent-ai-auto-fix-bug
php artisan serve
```

Nếu frontend cần dev server, mở thêm terminal:

```bash
cd /Users/wake/Documents/Projects/demo-agent-ai-auto-fix-bug
npm run dev
```

Laravel chỉ ghi `storage/logs/laravel.log`; không chạy Claude, watcher hay publisher.

## Terminal 2 — Orchestrator backend

```bash
cd /Users/wake/Documents/Projects/demo-agent-ai-auto-fix-bug-backend
make install   # lần đầu
make db-init   # lần đầu: tạo MySQL database/user, ghi AI_FIX_DATABASE_URL vào .env
make test
PYTHONPATH=src .venv/bin/python -m autofix_agent preflight --config config/demo.json
make watch
```

`.env` backend phải có `AI_FIX_DATABASE_URL` trỏ tới MySQL riêng của orchestrator; xem
[dashboard.md](dashboard.md). Dòng đầu của watcher in `State store connected`.

## Terminal 3 — Dashboard

```bash
cd /Users/wake/Documents/Projects/demo-agent-ai-auto-fix-bug-backend
make ui
```

Mở http://localhost:8787 để xem incident, stepper tiến trình, timeline, verification,
report và PR theo thời gian thực.

Watcher lần đầu bắt đầu từ cuối log. Giữ terminal mở, đăng nhập Laravel và gây đúng lỗi
profile ba lần trong 600 giây. Các stage mong đợi:

```text
OBSERVE  1/3
OBSERVE  2/3
OBSERVE  3/3
TRIGGER
ANALYZE  read-only
GUARD    analysis/path policy
FIX      isolated worktree only
GUARD    diff/test integrity
VERIFY   Pint, PHPUnit, npm build, git diff --check
GIT      commit, push
DONE     pull request URL, report path
```

Xem lại trạng thái:

```bash
make status
```

Nếu một run fail sau khi đã trigger, orchestrator lưu `var/reports/<run>-failure.md`
gồm command output và diff chẩn đoán trước khi cleanup. Sau khi sửa nguyên nhân hạ tầng,
bấm **Retry incident** trên dashboard (watcher đang chạy sẽ nhận), hoặc retry bằng prefix
hiển thị trong `make status`:

```bash
PYTHONPATH=src .venv/bin/python -m autofix_agent retry \
  --config config/demo.json \
  --incident 7dfba14a
```

Mỗi retry dùng run ID mới trong tên branch/worktree để không đụng attempt cũ.

Run thật đã tạo:

- branch `ai-fix/incident-7dfba14a-19edd163-0ca10c`;
- commit `d6987267fc11225b4f319aca264bd9a9a772e3f0`;
- [draft PR #1](https://github.com/nguyenthanhthuc2000/demo-agent-ai-auto-fix-bug/pull/1).

## Điều kiện để local tạo PR

- `.env` backend có `AI_FIX_PUBLISH_PR=true` và token hợp lệ.
- GitHub `master` tồn tại và token có quyền fetch; local `master` không cần cùng SHA vì
  publisher dùng private remote-base ref.
- Claude Code CLI đã authenticated.
- Composer/npm có thể tải dependency.
- Bot có quyền push feature branch và tạo PR.

Nếu muốn test pipeline mà không tạo GitHub side effect, đặt
`AI_FIX_PUBLISH_PR=false`. Hệ thống vẫn tạo local branch, verified commit và report.

## Dừng

Nhấn `Ctrl-C` ở watcher, dashboard và Laravel server. Worktree active được cleanup khi run kết thúc;
local/remote fix branch và report được giữ để review. Không xóa `var/worktrees` thủ công
khi terminal đang ở stage `FIX` hoặc `VERIFY`.
