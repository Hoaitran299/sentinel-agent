# Deploy Python orchestrator lên Ubuntu VPS

## Topology

Chạy backend dưới dedicated user/host, tách PHP-FPM và production secrets. Runner chỉ cần:

- read log source;
- clone/mirror Git repository;
- write backend `var/` và managed worktrees;
- outbound tới Claude provider, Composer/npm registries và GitHub;
- GitHub publisher token tối thiểu.

## Runtime dependencies

Python 3.9+, MySQL 8 (local hoặc managed), Git, PHP/Composer tương thích Laravel,
Node/npm tương thích lock file và Claude Code CLI đã authenticated dưới runner user. Xác
minh exact binary paths bằng preflight. Cài dependency Python bằng `make install`.

Tạo database/user riêng bằng `make db-init` (xem [dashboard.md](dashboard.md)); lệnh tự ghi
`AI_FIX_DATABASE_URL` vào `.env`. Không dùng MySQL user của Laravel production.

Log nằm ở app server khác: đặt `AI_FIX_LOG_SOURCE=http|ssh|s3` theo
[log-sources.md](log-sources.md) thay vì mount filesystem.

## Directory example

```text
/srv/ai-fix/backend
/srv/ai-fix/source/laravel
/srv/ai-fix/logs/laravel.log
```

Sửa `config/demo.json` theo paths thật: `source_repo`, `log_path` và đặc biệt
`claude_binary` (config demo đang trỏ tới path nvm trên macOS). Lấy path bằng
`sudo -u aifix -i which claude`. Copy `.env.example` thành `.env`, mode `0600`, điền
publisher token. Không copy Laravel `.env`.

## Preflight

```bash
cd /srv/ai-fix/backend
make test
PYTHONPATH=src .venv/bin/python -m autofix_agent preflight --config config/demo.json
```

## systemd

Tạo `/etc/systemd/system/ai-fix-watcher.service`:

```ini
[Unit]
Description=External AI Fix Watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=aifix
Group=aifix
WorkingDirectory=/srv/ai-fix/backend
ExecStart=/usr/bin/env PYTHONPATH=src /srv/ai-fix/backend/.venv/bin/python -m autofix_agent watch --config config/demo.json
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-fix-watcher
sudo journalctl -u ai-fix-watcher -f
```

Dashboard có thể chạy thành unit thứ hai `ai-fix-dashboard.service` giống hệt, chỉ đổi
`ExecStart` thành `... -m autofix_agent ui --config config/demo.json`. Nó chỉ bind
`127.0.0.1`; truy cập qua `ssh -L 8787:127.0.0.1:8787 aifix@runner-host`.

## Operations

- Backup database `ai_fix_orchestrator` (`mysqldump`) và `var/reports` theo policy.
- Alert disk usage vì each worktree bootstrap dependency riêng.
- Không xóa worktree trong FIX/VERIFY.
- Restart service sau token rotation; `.env` không hot-reload.
- Revoke publisher token khi nghi ngờ lộ.
- Giữ human review/branch protection; service không merge/deploy.
