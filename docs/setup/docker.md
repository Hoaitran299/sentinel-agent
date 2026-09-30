# Chạy hai project bằng Docker và tạo PR thật

Chỉ hỗ trợ Docker. Hai repo phải nằm cạnh nhau (`../sentinel-demo-app`, `../sentinel-agent`);
`compose.yaml` ở `sentinel-demo-app` chạy MySQL, Laravel, watcher và dashboard. Xem thêm
`../sentinel-demo-app/docs/setup/docker.md`.

## Khởi động

```bash
cd sentinel-agent
cp .env.example .env && chmod 600 .env   # token GitHub/Telegram nếu cần

cd ../sentinel-demo-app
docker compose up -d --build
docker compose run --rm watcher claude   # lần đầu: /login rồi /exit
docker compose exec watcher python -m autofix_agent preflight --config config/docker.json
docker compose exec app php artisan demo:user --email=demo@example.com
docker compose logs -f watcher
```

MySQL (`127.0.0.1:3307` từ host, `mysql:3306` trong container):

| Database              | Dùng bởi             | User       | Password   |
|-----------------------|----------------------|------------|------------|
| `sentinel`            | Laravel              | `sentinel` | `sentinel` |
| `ai_fix_orchestrator` | watcher, dashboard   | `sentinel` | `sentinel` |

`AI_FIX_DATABASE_URL` do compose đặt (`mysql://sentinel:sentinel@mysql:3306/ai_fix_orchestrator`),
ghi đè `.env`. Dòng đầu của watcher in `State store connected`.

Laravel: http://localhost:8000 — Dashboard: http://localhost:8787 (incident, stepper tiến
trình, timeline, verification, report và PR theo thời gian thực).

Watcher lần đầu bắt đầu từ cuối log. Giữ `docker compose logs -f watcher` mở, đăng nhập Laravel và gây đúng lỗi
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
docker compose exec watcher python -m autofix_agent status --config config/docker.json
```

Nếu một run fail sau khi đã trigger, orchestrator lưu `var/reports/<run>-failure.md`
gồm command output và diff chẩn đoán trước khi cleanup. Sau khi sửa nguyên nhân hạ tầng,
bấm **Retry incident** trên dashboard (watcher đang chạy sẽ nhận), hoặc retry bằng prefix
hiển thị trong lệnh `status`:

```bash
docker compose exec watcher python -m autofix_agent retry \
  --config config/docker.json \
  --incident 7dfba14a
```

Mỗi retry dùng run ID mới trong tên branch/worktree để không đụng attempt cũ.

Run thật đã tạo:

- branch `ai-fix/incident-7dfba14a-19edd163-0ca10c`;
- commit `d6987267fc11225b4f319aca264bd9a9a772e3f0`;
- [draft PR #1](https://github.com/nguyenthanhthuc2000/demo-agent-ai-auto-fix-bug/pull/1).

## Điều kiện để local tạo PR

- `.env` backend có `AI_FIX_PUBLISH_PR=true` và token hợp lệ (sau khi sửa: `docker compose restart watcher`).
- GitHub `master` tồn tại và token có quyền fetch; local `master` không cần cùng SHA vì
  publisher dùng private remote-base ref.
- Claude Code CLI trong container đã authenticated (`docker compose run --rm watcher claude`).
- Container có mạng để Composer/npm tải dependency.
- Bot có quyền push feature branch và tạo PR.

Nếu muốn test pipeline mà không tạo GitHub side effect, đặt
`AI_FIX_PUBLISH_PR=false`. Hệ thống vẫn tạo local branch, verified commit và report.

## Dừng

```bash
docker compose stop        # giữ dữ liệu
docker compose down -v     # xoá cả MySQL, worktree, credentials Claude
```

Worktree active được cleanup khi run kết thúc; local/remote fix branch và report được giữ
để review. Không dừng watcher khi đang ở stage `FIX` hoặc `VERIFY`.
