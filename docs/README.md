# Tài liệu sentinel-agent

| Thư mục | Nội dung |
|---------|----------|
| [`build/`](build/) | Thiết kế và xây dựng dự án: kiến trúc, trạng thái implementation |
| [`setup/`](setup/) | Cài đặt, cấu hình và vận hành (chạy bằng Docker) |
| [`qa/`](qa/) | Các câu hỏi và trả lời về kiến trúc dự án |

## build/

- [architecture.md](build/architecture.md) — component, trust boundary, dashboard, worktree, publish, failure
- [implementation-status.md](build/implementation-status.md) — đã làm, giới hạn hiện tại, việc tiếp theo

## setup/

- [docker.md](setup/docker.md) — chạy hai project bằng Docker và tạo PR thật
- [configuration.md](setup/configuration.md) — file config và biến môi trường
- [dashboard.md](setup/dashboard.md) — dashboard, MySQL state store, cài đặt xử lý lỗi
- [github-publishing.md](setup/github-publishing.md) — GitHub token và PR publisher
- [log-sources.md](setup/log-sources.md) — đọc log từ file, HTTP, SSH, S3/R2
- [telegram.md](setup/telegram.md) — thông báo Telegram
- [troubleshooting.md](setup/troubleshooting.md) — lỗi thường gặp và retry
- [deployment-ubuntu-vps.md](setup/deployment-ubuntu-vps.md) — deploy runner lên Ubuntu VPS

## qa/

- [architecture-qa.md](qa/architecture-qa.md) — vì sao DB riêng, không Redis, MySQL thay SQLite

Câu hỏi chung cho cả hai project: `../sentinel-demo-app/docs/qa/technical-qa.md`.
Câu hỏi kiến trúc mới: thêm vào `qa/architecture-qa.md` dạng `### Câu hỏi?` + trả lời ngắn.
