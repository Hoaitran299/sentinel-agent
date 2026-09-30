from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claude import ClaudeCodeRunner
from .config import Config
from .console import scope, stage
from .github import GitHubPublisher
from .i18n import t
from .notifier import TelegramNotifier
from .policy import PolicyBaseline, WorkspacePolicy
from .process import CommandError, ProcessExecutor, safe_environment
from .log_reader import sanitize
from .storage import StateStore
from .worktree import GitWorktreeManager, Workspace


class AgentPipeline:
    def __init__(self, config: Config, store: StateStore):
        self.config = config
        self.store = store
        self.executor = ProcessExecutor()
        self.worktrees = GitWorktreeManager(config, self.executor)
        self.policy = WorkspacePolicy(config, self.executor)
        self.claude = ClaudeCodeRunner(config, self.executor)
        self.publisher = GitHubPublisher(config, self.executor)
        self.notifier = TelegramNotifier(config)

    def apply_settings(self, runtime: Any) -> None:
        # fix_only mode runs the agent silently (dashboard/timeline only, no Telegram).
        self.notifier.muted = not runtime.sends_notifications

    def report(self, incident_id: str) -> None:
        self.notifier.notify("reported", self.store.incident(incident_id))

    def notify_repeat(self, incident_id: str, suppressed_count: int) -> None:
        # Only the first repeat after a fix is announced, to avoid flooding the chat.
        if suppressed_count == 1:
            self.notifier.notify("repeated", self.store.incident(incident_id))

    def resolve_source(self) -> str:
        if not self.publisher.enabled():
            return self.worktrees.resolve_source()
        github = self.publisher.preflight()
        fetched = self.publisher.fetch_base(self.config.source_repo)
        if fetched != github.get("base_sha"):
            raise RuntimeError("fetched GitHub base does not match the API ref")
        return fetched

    def sync_pull_requests(self) -> None:
        """Move incidents whose PR was merged/closed out of `waiting_for_review`."""
        if not self.publisher.enabled():
            return
        for row in self.store.open_pull_requests():
            with scope(incident_id=row["id"]):
                try:
                    state = self.publisher.pull_request_state(row["pull_request_url"])
                except Exception as error:
                    stage("GIT", "Could not read pull request state", incident=row["id"][:8], reason=self._safe_reason(error))
                    continue
                if state == "open":
                    continue
                outcome = "merged" if state == "merged" else "dismissed"
                if self.store.set_review_outcome(row["id"], outcome):
                    message = (
                        "Pull request merged; repeats stay attached until the fix is deployed"
                        if outcome == "merged"
                        else "Pull request closed without merge; auto-fix stays paused for this error"
                    )
                    stage("DONE", message, incident=row["id"][:8], url=row["pull_request_url"])
                    self.notifier.notify("pr_merged" if outcome == "merged" else "pr_closed", self.store.incident(row["id"]))

    def run(self, incident_id: str) -> None:
        run_id = self.store.claim(incident_id)
        if run_id is None:
            stage("OBSERVE", "Incident is already claimed", incident=incident_id[:8])
            return
        with scope(incident_id=incident_id, run_id=run_id):
            self._execute(incident_id, run_id)

    def _execute(self, incident_id: str, run_id: str) -> None:
        incident = self.store.incident(incident_id)
        workspace: Optional[Workspace] = None
        current_status = "analysis_failed"
        analysis: Dict[str, Any] = {}
        self.notifier.notify("started", incident)
        try:
            stage("ANALYZE", "Claude Code preflight", incident=incident_id[:8])
            version = self.claude.preflight()
            stage("ANALYZE", "Claude Code is available", version=version)

            current_source = self.resolve_source()
            if current_source != incident["source_commit"]:
                raise RuntimeError(
                    "{} moved after this incident was observed; refusing a non-reproducible fix".format(self.config.base_branch)
                )
            stage("GIT", "Creating branch and isolated worktree from {}".format(self.config.base_branch), commit=current_source[:12])
            workspace = self.worktrees.create(incident_id, incident["fingerprint"], current_source, run_id)
            self.worktrees.bootstrap(workspace)

            baseline = self.policy.capture(workspace.path)
            stage("GUARD", "Pre-write test/protected-path baseline captured", tests=len(baseline.tracked_tests))

            stage("ANALYZE", "Claude is checking the bug in read-only mode")
            analysis = self.claude.analyze(workspace.path, incident)
            self.policy.validate_analysis(analysis)
            self.store.update_run(run_id, "fixing", analysis=analysis)
            stage("GUARD", "Analysis schema and proposed paths accepted")

            current_status = "fix_failed"
            stage("FIX", "Write access enabled only inside the isolated worktree")
            fix_result = self.claude.fix(workspace.path, incident, analysis, self.config.allowed_paths)
            changed = self.policy.enforce(workspace.path, baseline)
            stage("GUARD", "Diff scope and test-integrity checks passed", files=len(changed))

            current_status = "verification_failed"
            self.store.update_run(run_id, "verifying")
            verification = self._verify(workspace.path, baseline, run_id)
            changed = self.policy.enforce(workspace.path, baseline)

            stage("GIT", "Committing the exact verified tree", branch=workspace.branch)
            self.executor.run(["git", "add", "--"] + changed, cwd=workspace.path, timeout=30)
            self.executor.run(
                [
                    "git",
                    "-c",
                    "user.name=AI Fix Agent",
                    "-c",
                    "user.email=ai-fix-agent@localhost",
                    "commit",
                    "-m",
                    "fix: validate profile phone input",
                ],
                cwd=workspace.path,
                timeout=120,
            )
            commit_sha = self.executor.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace.path, timeout=30
            ).stdout.strip()
            report_path = self._write_report(
                incident=incident,
                run_id=run_id,
                workspace=workspace,
                analysis=analysis,
                fix_result=fix_result,
                verification=verification,
                changed=changed,
                commit_sha=commit_sha,
            )
            pull_request_url = None
            if self.publisher.enabled():
                current_status = "publish_failed"
                self.store.update_run(run_id, "publishing")
                stage("GIT", "Pushing verified branch with trusted publisher", branch=workspace.branch)
                publish_result = self.publisher.publish(
                    workspace=workspace.path,
                    branch=workspace.branch,
                    commit_sha=commit_sha,
                    incident_id=incident_id,
                    analysis=analysis,
                )
                pull_request_url = publish_result.pull_request_url
                with report_path.open("a", encoding="utf-8") as report:
                    report.write("\n- {}: {}\n".format(t(self.config.report_language, "pull_request"), pull_request_url))
                stage(
                    "DONE",
                    "Pull request {}".format("created" if publish_result.created else "already exists"),
                    url=pull_request_url,
                )
            self.store.update_run(
                run_id,
                "waiting_for_review",
                verification=verification,
                finished_at=time.time(),
            )
            self.store.complete_incident(
                incident_id,
                workspace.branch,
                commit_sha,
                str(report_path),
                pull_request_url,
            )
            stage("DONE", "Fix verified and committed", branch=workspace.branch, commit=commit_sha[:12])
            stage("DONE", "Report written", report=report_path)
            self.notifier.notify(
                "succeeded",
                incident,
                summary=analysis.get("summary"),
                branch=workspace.branch,
                commit=commit_sha,
                pull_request_url=pull_request_url,
                report=report_path.name,
            )
        except Exception as error:
            reason = self._safe_reason(error)
            failure_report = self._write_failure_report(incident, run_id, workspace, error)
            self.store.update_run(run_id, current_status, failure_reason=reason, finished_at=time.time())
            self.store.fail_incident(
                incident_id,
                reason,
                report_path=str(failure_report) if failure_report else None,
                branch_name=workspace.branch if workspace else None,
            )
            stage("ERROR", "Agent run stopped safely", reason=reason)
            if failure_report:
                stage("ERROR", "Failure diagnostics preserved", report=failure_report)
            self.notifier.notify(
                "failed",
                incident,
                reason=reason,
                summary=analysis.get("summary"),
                branch=workspace.branch if workspace else None,
                report=failure_report.name if failure_report else None,
            )
        finally:
            self.worktrees.cleanup(workspace)

    def _verify(self, workspace: Path, baseline: PolicyBaseline, run_id: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        environment = safe_environment()
        for command in self.config.verification_commands:
            stage("VERIFY", "Running trusted verification", command=" ".join(command))
            started = time.monotonic()
            result = self.executor.run(
                command,
                cwd=workspace,
                timeout=self.config.claude_timeout_seconds,
                environment=environment,
            )
            self.policy.enforce(workspace, baseline)
            results.append(
                {
                    "command": command,
                    "returncode": result.returncode,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "stdout_tail": result.stdout[-2000:],
                }
            )
            self.store.update_run(run_id, "verifying", verification=results)
        return results

    def _write_failure_report(
        self,
        incident: dict,
        run_id: str,
        workspace: Optional[Workspace],
        error: Exception,
    ) -> Optional[Path]:
        self.config.reports_dir.mkdir(parents=True, exist_ok=True)
        report = self.config.reports_dir / "{}-failure.md".format(run_id)
        language = self.config.report_language
        sections = [
            "# {}".format(t(language, "failure_title")),
            "",
            "- {}: `{}`".format(t(language, "incident"), incident["id"]),
            "- {}: `{}`".format(t(language, "run"), run_id),
            "- {}: `{}`".format(t(language, "source_commit"), incident["source_commit"]),
            "- {}: `{}`".format(t(language, "failure"), self._safe_reason(error)),
        ]
        if workspace is not None:
            sections.append("- {}: `{}`".format(t(language, "branch"), workspace.branch))
            tracked = self.executor.run(
                ["git", "diff", "--no-color"], cwd=workspace.path, timeout=30, check=False
            ).stdout
            status = self.executor.run(
                ["git", "status", "--porcelain=v1"], cwd=workspace.path, timeout=30, check=False
            ).stdout
            untracked_patches = []
            for row in status.splitlines():
                if not row.startswith("?? "):
                    continue
                relative = row[3:]
                candidate = workspace.path / relative
                if candidate.is_file():
                    result = self.executor.run(
                        ["git", "diff", "--no-index", "--no-color", "/dev/null", relative],
                        cwd=workspace.path,
                        timeout=30,
                        check=False,
                    )
                    untracked_patches.append(result.stdout)
            patch = sanitize(tracked + "\n" + "\n".join(untracked_patches), limit=100000)
            sections.extend(["", "## {}".format(t(language, "preserved_diff")), "", "```diff", patch, "```"])
        if isinstance(error, CommandError) and error.result is not None:
            output = sanitize(
                "STDOUT:\n{}\n\nSTDERR:\n{}".format(error.result.stdout, error.result.stderr),
                limit=50000,
            )
            sections.extend(["", "## {}".format(t(language, "command_output")), "", "```text", output, "```"])
        report.write_text("\n".join(sections) + "\n", encoding="utf-8")
        return report

    def _write_report(
        self,
        incident: dict,
        run_id: str,
        workspace: Workspace,
        analysis: dict,
        fix_result: dict,
        verification: list,
        changed: list,
        commit_sha: str,
    ) -> Path:
        self.config.reports_dir.mkdir(parents=True, exist_ok=True)
        report = self.config.reports_dir / "{}.md".format(run_id)
        language = self.config.report_language
        report.write_text(
            "\n".join(
                [
                    "# {}".format(t(language, "report_title")),
                    "",
                    "- {}: `{}`".format(t(language, "incident"), incident["id"]),
                    "- {}: `{}`".format(t(language, "source_commit"), incident["source_commit"]),
                    "- {}: `{}`".format(t(language, "branch"), workspace.branch),
                    "- {}: `{}`".format(t(language, "verified_commit"), commit_sha),
                    "- {}: `{}`".format(t(language, "occurrences"), incident["occurrence_count"]),
                    "- {}: `{}`".format(t(language, "changed_files"), ", ".join(changed)),
                    "",
                    "## {}".format(t(language, "analysis")),
                    "",
                    "```json",
                    json.dumps(analysis, indent=2, sort_keys=True),
                    "```",
                    "",
                    "## {}".format(t(language, "fix_result")),
                    "",
                    "```json",
                    json.dumps(fix_result, indent=2, sort_keys=True),
                    "```",
                    "",
                    "## {}".format(t(language, "verification")),
                    "",
                    "```json",
                    json.dumps(verification, indent=2, sort_keys=True),
                    "```",
                    "",
                    t(language, "publisher_note"),
                    t(language, "credential_note"),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return report

    @staticmethod
    def _safe_reason(error: Exception) -> str:
        value = sanitize("{}: {}".format(error.__class__.__name__, str(error)), limit=6000)
        value = value.replace("\n", " ")
        return value[:1000]
