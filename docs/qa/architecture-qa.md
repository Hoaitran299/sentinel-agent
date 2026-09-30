# Architecture Q&A

Các câu hỏi về quyết định kiến trúc của orchestrator. Câu hỏi chung cho cả hai project
(vì sao tách project, sandbox, secret, retry...) nằm ở
`../sentinel-demo-app/docs/qa/technical-qa.md`.

### Vì sao không dùng database của Laravel?

Nếu agent state nằm trong Laravel, web app phải mang migration, queue job và coupling với
runner. Orchestrator dùng database MySQL riêng (`ai_fix_orchestrator`) và tự migrate schema
của mình; Laravel không biết đến các bảng này. Hai bên cũng không thể dùng chung một
database vì cùng có bảng `incidents`.

Trong Docker, cả hai dùng chung MySQL user `sentinel` cho đơn giản; production nên tách user
để orchestrator không có quyền trên database Laravel.

### Vì sao không dùng Redis?

Watcher hiện tại là một process; MySQL transaction là durable source of truth, nên Redis
không cần thiết.

### Vì sao MySQL thay vì SQLite?

Dashboard và watcher đọc/ghi đồng thời ổn định, backup bằng công cụ chuẩn và chuẩn bị cho
việc runner/dashboard nằm ở process hoặc host khác nhau. Connection tự reconnect khi MySQL
đóng connection idle (`wait_timeout`).
