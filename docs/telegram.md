# Thông báo Telegram

Watcher gửi thông báo Telegram ở các mốc quan trọng của pipeline. Notifier là thành phần
trusted của backend: bot token chỉ nằm trong `.env` backend, không bao giờ đi vào Claude,
worktree, argv, report, database hay log (lỗi từ Telegram API được che token).

## 1. Tạo bot và lấy chat id

1. Chat với [@BotFather](https://t.me/BotFather), gửi `/newbot`, lưu token dạng
   `123456789:AA...`.
2. Tạo group (hoặc channel) cho team, thêm bot vào. Với channel, cấp quyền đăng bài cho bot.
3. Gửi một tin bất kỳ trong group, rồi mở
   `https://api.telegram.org/bot<TOKEN>/getUpdates` trên trình duyệt của bạn để lấy
   `chat.id` (group thường có dạng `-100...`). Channel public có thể dùng `@tenchannel`.

Nên dùng bot riêng cho orchestrator, không dùng chung với bot của ứng dụng Laravel.

## 2. Cấu hình `.env` backend

```env
AI_FIX_TELEGRAM_ENABLED=true
AI_FIX_TELEGRAM_BOT_TOKEN=123456789:AA...
AI_FIX_TELEGRAM_CHAT_ID=-1001234567890
AI_FIX_TELEGRAM_EVENTS=started,succeeded,failed,pr_merged,pr_closed
```

Kiểm tra:

```bash
make telegram-test     # gửi tin nhắn thử
PYTHONPATH=src .venv/bin/python -m autofix_agent preflight --config config/demo.json  # in tên bot
```

Restart `make watch` sau khi đổi `.env`.

## 3. Sự kiện

| Event | Khi nào | Nội dung chính |
| --- | --- | --- |
| `started` | Lỗi đạt threshold, agent bắt đầu | incident, message lỗi, số lần |
| `succeeded` | Fix pass verification, đã commit/PR | tóm tắt phân tích, branch, commit, link PR |
| `failed` | Run dừng an toàn ở bất kỳ gate nào | lý do, tên failure report |
| `pr_merged` | Watcher thấy PR đã merge | link PR |
| `pr_closed` | PR bị đóng không merge | link PR |
| `repeated` | Lần **đầu tiên** lỗi lặp lại sau khi đã có fix (tắt mặc định) | gợi ý fix chưa merge/deploy |

`pr_merged`/`pr_closed` cần publisher bật (`AI_FIX_PUBLISH_PR=true`). `repeated` chỉ gửi
một lần cho mỗi incident để không spam khi lỗi tiếp tục xảy ra.

Ngôn ngữ tin nhắn theo `AI_FIX_REPORT_LANGUAGE` (`en`/`vi`/`ja`).

## 4. Hành vi khi lỗi

Gửi Telegram thất bại (mạng, token sai, bot bị kick khỏi group) chỉ in dòng
`Telegram notification failed` ở terminal/timeline; pipeline vẫn tiếp tục. Token sai định
dạng bị từ chối trước khi gửi request. `AI_FIX_TELEGRAM_CHAT_ID` sai định dạng làm config lỗi
ngay khi khởi động.
