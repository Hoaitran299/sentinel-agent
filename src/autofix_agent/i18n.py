from __future__ import annotations

from typing import Any, Dict


DEFAULT_LANGUAGE = "en"

# Name given to Claude so its human-readable analysis fields match the report language.
LANGUAGE_NAMES = {
    "en": "English",
    "vi": "Vietnamese (tiếng Việt)",
    "ja": "Japanese (日本語)",
}

MESSAGES: Dict[str, Dict[str, str]] = {
    "en": {
        "report_title": "AI fix report",
        "failure_title": "AI fix failure report",
        "incident": "Incident",
        "run": "Run",
        "source_commit": "Source commit",
        "branch": "Branch",
        "verified_commit": "Verified commit",
        "occurrences": "Occurrences",
        "changed_files": "Changed files",
        "failure": "Failure",
        "analysis": "Analysis",
        "fix_result": "Fix agent result",
        "verification": "Verification",
        "preserved_diff": "Preserved diff",
        "command_output": "Command output",
        "pull_request": "Pull request",
        "publisher_note": "PR publishing is performed only by the trusted publisher when enabled.",
        "credential_note": "Claude Code never receives GitHub credentials.",
        "pr_title": "fix: auto-fix incident {incident}",
        "pr_heading": "Automated fix report",
        "pr_source_branch": "Source branch",
        "pr_summary_heading": "Analysis summary",
        "pr_summary_fallback": "Automated verified fix",
        "pr_gate_note": "This PR was pushed by the trusted publisher after local policy and verification gates passed.",
        "pr_credential_note": "Claude Code did not receive GitHub credentials.",
        "tg_test": "Telegram notifications are working",
        "tg_reported": "Error threshold reached (report only)",
        "tg_reported_hint": "Report-only mode: no AI fix was started. Use \"Run AI fix\" on the dashboard to fix it.",
        "tg_repository": "Repository",
        "tg_started": "Error threshold reached — AI agent started fixing",
        "tg_succeeded": "Fix verified and ready for review",
        "tg_failed": "AI fix stopped safely",
        "tg_pr_merged": "Fix pull request merged",
        "tg_pr_closed": "Fix pull request closed without merge",
        "tg_repeated": "Error still occurs although a fix exists",
        "tg_repeated_hint": "The fix is probably not merged or deployed yet. No new agent run was started.",
        "tg_error": "Error",
        "tg_summary": "Summary",
        "tg_reason": "Reason",
        "tg_report": "Report",
    },
    "vi": {
        "report_title": "Báo cáo AI fix",
        "failure_title": "Báo cáo AI fix thất bại",
        "incident": "Incident",
        "run": "Lần chạy",
        "source_commit": "Commit nguồn",
        "branch": "Branch",
        "verified_commit": "Commit đã verify",
        "occurrences": "Số lần lỗi",
        "changed_files": "File thay đổi",
        "failure": "Lỗi",
        "analysis": "Phân tích",
        "fix_result": "Kết quả của agent sửa lỗi",
        "verification": "Kiểm chứng",
        "preserved_diff": "Diff được giữ lại",
        "command_output": "Output của lệnh",
        "pull_request": "Pull request",
        "publisher_note": "PR chỉ được tạo bởi trusted publisher khi tính năng này được bật.",
        "credential_note": "Claude Code không bao giờ nhận GitHub credential.",
        "pr_title": "fix: tự động sửa incident {incident}",
        "pr_heading": "Báo cáo sửa lỗi tự động",
        "pr_source_branch": "Branch nguồn",
        "pr_summary_heading": "Tóm tắt phân tích",
        "pr_summary_fallback": "Bản sửa tự động đã được kiểm chứng",
        "pr_gate_note": "PR này được trusted publisher push sau khi đã qua các bước kiểm tra policy và verification.",
        "pr_credential_note": "Claude Code không nhận GitHub credential.",
        "tg_test": "Thông báo Telegram đã hoạt động",
        "tg_reported": "Lỗi đạt ngưỡng (chỉ báo cáo)",
        "tg_reported_hint": "Chế độ chỉ báo cáo: chưa chạy AI fix. Bấm \"Chạy AI fix\" trên dashboard nếu muốn sửa.",
        "tg_repository": "Repository",
        "tg_started": "Lỗi đạt ngưỡng — AI agent bắt đầu sửa",
        "tg_succeeded": "Bản sửa đã được kiểm chứng, chờ review",
        "tg_failed": "AI fix đã dừng an toàn",
        "tg_pr_merged": "Pull request sửa lỗi đã được merge",
        "tg_pr_closed": "Pull request sửa lỗi bị đóng, không merge",
        "tg_repeated": "Lỗi vẫn xảy ra dù đã có bản sửa",
        "tg_repeated_hint": "Nhiều khả năng bản sửa chưa được merge hoặc deploy. Hệ thống không chạy agent lại.",
        "tg_error": "Lỗi",
        "tg_summary": "Tóm tắt",
        "tg_reason": "Lý do",
        "tg_report": "Report",
    },
    "ja": {
        "report_title": "AI 修正レポート",
        "failure_title": "AI 修正失敗レポート",
        "incident": "インシデント",
        "run": "実行",
        "source_commit": "ソースコミット",
        "branch": "ブランチ",
        "verified_commit": "検証済みコミット",
        "occurrences": "発生回数",
        "changed_files": "変更ファイル",
        "failure": "失敗理由",
        "analysis": "分析",
        "fix_result": "修正エージェントの結果",
        "verification": "検証",
        "preserved_diff": "保存された差分",
        "command_output": "コマンド出力",
        "pull_request": "プルリクエスト",
        "publisher_note": "PR の作成は、有効化されている場合に信頼済みパブリッシャーのみが行います。",
        "credential_note": "Claude Code が GitHub の認証情報を受け取ることはありません。",
        "pr_title": "fix: インシデント {incident} の自動修正",
        "pr_heading": "自動修正レポート",
        "pr_source_branch": "ソースブランチ",
        "pr_summary_heading": "分析の概要",
        "pr_summary_fallback": "検証済みの自動修正",
        "pr_gate_note": "この PR は、ローカルのポリシーチェックと検証をすべて通過した後に信頼済みパブリッシャーによって push されました。",
        "pr_credential_note": "Claude Code は GitHub の認証情報を受け取っていません。",
        "tg_test": "Telegram 通知は正常に動作しています",
        "tg_reported": "エラーがしきい値に達しました（報告のみ）",
        "tg_reported_hint": "報告のみモードのため AI 修正は開始されていません。修正する場合はダッシュボードの「AI 修正を実行」を使用してください。",
        "tg_repository": "リポジトリ",
        "tg_started": "エラーがしきい値に達しました — AI エージェントが修正を開始",
        "tg_succeeded": "修正が検証され、レビュー待ちです",
        "tg_failed": "AI 修正は安全に停止しました",
        "tg_pr_merged": "修正プルリクエストがマージされました",
        "tg_pr_closed": "修正プルリクエストがマージされずにクローズされました",
        "tg_repeated": "修正済みにもかかわらずエラーが発生しています",
        "tg_repeated_hint": "修正がまだマージまたはデプロイされていない可能性があります。新しいエージェント実行は開始されていません。",
        "tg_error": "エラー",
        "tg_summary": "概要",
        "tg_reason": "理由",
        "tg_report": "レポート",
    },
}


def normalize_language(value: str) -> str:
    language = value.strip().lower().replace("_", "-").split("-", 1)[0]
    if language not in MESSAGES:
        raise ValueError("report language must be one of: {}".format(", ".join(sorted(MESSAGES))))
    return language


def t(language: str, key: str, **values: Any) -> str:
    template = MESSAGES.get(language, MESSAGES[DEFAULT_LANGUAGE]).get(key) or MESSAGES[DEFAULT_LANGUAGE][key]
    return template.format(**values) if values else template


def language_instruction(language: str) -> str:
    return (
        "Write every human-readable text field of the result in {}. Keep file paths, code "
        "identifiers, commands, and quoted log text unchanged.".format(LANGUAGE_NAMES[language])
    )
