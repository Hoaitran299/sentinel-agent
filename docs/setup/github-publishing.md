# GitHub token và PR publisher

## Tài khoản riêng

Khuyến nghị tạo GitHub bot/service account riêng cho demo, thêm account đó vào đúng một
repository và tạo fine-grained personal access token. Không dùng token cá nhân có quyền
toàn organization.

Code chấp nhận classic PAT prefix `ghp_` để phục vụ local demo, nhưng fine-grained PAT
prefix `github_pat_` vẫn là lựa chọn khuyến nghị vì giới hạn được repository/permission.

Repository access:

- Chọn **Only select repositories**.
- Chọn `nguyenthanhthuc2000/demo-agent-ai-auto-fix-bug`.

Repository permissions tối thiểu:

- **Contents: Read and write** — push branch `ai-fix/...`.
- **Pull requests: Read and write** — tìm và tạo PR.
- **Metadata: Read-only** — GitHub tự cấp.

Không cần Actions, Administration, Secrets, Environments hoặc Webhooks. Đặt expiration
ngắn, rotate/revoke sau demo và bật branch protection cho `master`.

Đối chiếu chính thức:

- [GitHub — Managing personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [GitHub — Fine-grained token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
- [GitHub — Create a pull request](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)

GitHub xác định endpoint tạo PR cần repository permission **Pull requests: write**. Quyền
**Contents: write** được dùng riêng cho Git push branch trước khi gọi endpoint này.

## Cài token

Trong project backend:

```bash
cp .env.example .env
chmod 600 .env
```

Điền trực tiếp bằng editor, không truyền token trong command line hoặc commit:

```env
AI_FIX_PUBLISH_PR=true
AI_FIX_GITHUB_TOKEN=github_pat_REPLACE_ME
AI_FIX_GITHUB_REPOSITORY=nguyenthanhthuc2000/demo-agent-ai-auto-fix-bug
AI_FIX_GITHUB_API_URL=https://api.github.com
AI_FIX_GITHUB_GIT_URL=https://github.com/nguyenthanhthuc2000/demo-agent-ai-auto-fix-bug.git
AI_FIX_GITHUB_DRAFT=true
```

Sau đó chạy:

```bash
PYTHONPATH=src .venv/bin/python -m autofix_agent preflight --config config/demo.json
```

Preflight kiểm tra token đọc được repository/base ref, sau đó fetch GitHub `master` vào
private ref `refs/ai-fix/base/master`. Worktree được tạo từ remote SHA này nên local
`master` có thể ahead/behind mà không làm PR lẫn commit ngoài ý muốn. Fetch không checkout,
reset hoặc sửa working tree Laravel; orchestrator cũng không push base branch.

## Credential boundary

Claude Code chỉ nhận environment allowlist và test-only Laravel settings. Các tên
`AI_FIX_GITHUB_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, Telegram và production DB đều bị
loại. Sau khi exact tree pass verification, trusted publisher mới đọc token:

1. Fetch remote base vào private ref bằng temporary askpass file mode `0700`.
2. Tạo worktree từ đúng remote SHA, không từ local checkout đang chạy.
3. Xác minh verified commit có parent đúng remote base SHA.
4. Push qua clean HTTPS URL; token chỉ ở environment của process Git.
5. Xóa askpass khi process kết thúc.
6. Tìm open PR cùng `head + base`; reuse nếu đã có.
7. Tạo draft PR nếu chưa có.

Token không xuất hiện trong argv, remote URL, prompt, worktree, state database hoặc report.

## Lỗi thường gặp

- `401 Bad credentials`: token sai/hết hạn/bị revoke/copy thiếu. Test `/user` cũng sẽ trả
  401; đây chưa phải lỗi permission. Thay token và restart watcher.
- `403 Resource not accessible`: thiếu Contents/Pull requests write hoặc bot chưa có repo access.
- `422 Validation Failed`: branch/base sai hoặc PR đã tồn tại; kiểm tra `make status`.
- `fetched GitHub base does not match`: remote đổi trong lúc preflight/fetch; chạy lại.
- Branch protection từ chối push vào `ai-fix/*`: chỉnh rule cho phép bot tạo feature branch,
  nhưng vẫn cấm bot push trực tiếp vào `master`.
