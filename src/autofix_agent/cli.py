from __future__ import annotations

import argparse
import signal
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .console import scope, set_sink, stage
from . import settings as runtime_settings
from .log_reader import matches_any
from .log_sources import Cursor, LogTailer, build_source
from .pipeline import AgentPipeline
from .storage import StateStore

# How often the watcher re-resolves the base branch while errors are arriving, so incidents
# observed after a merge/push are tagged with the commit a fix run will actually start from.
SOURCE_REFRESH_SECONDS = 30


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="External Laravel log-driven AI fix orchestrator")
    commands = root.add_subparsers(dest="command", required=True)

    watch = commands.add_parser("watch", help="Tail Laravel logs and run fixes at the threshold")
    watch.add_argument("--config", type=Path, required=True)
    watch.add_argument("--from-start", action="store_true", help="Ignore the saved cursor and read the current log from byte 0")
    watch.add_argument("--once", action="store_true", help="Poll once and exit")

    status = commands.add_parser("status", help="Show recent orchestrator incidents")
    status.add_argument("--config", type=Path, required=True)

    preflight = commands.add_parser("preflight", help="Validate paths, Git base and Claude CLI")
    preflight.add_argument("--config", type=Path, required=True)

    retry_command = commands.add_parser("retry", help="Retry one failed incident with a new run/worktree")
    retry_command.add_argument("--config", type=Path, required=True)
    retry_command.add_argument("--incident", required=True)

    ui = commands.add_parser("ui", help="Serve the local incident dashboard")
    ui.add_argument("--config", type=Path, required=True)
    ui.add_argument("--port", type=int, help="Override ui_port from the config file")

    release = commands.add_parser("release", help="Allow a new auto-fix for an error whose fix is pending or closed")
    release.add_argument("--config", type=Path, required=True)
    release.add_argument("--incident", required=True)

    telegram = commands.add_parser("telegram-test", help="Send a test Telegram notification")
    telegram.add_argument("--config", type=Path, required=True)

    db_init = commands.add_parser("db-init", help="Create the MySQL database/user for the orchestrator and write AI_FIX_DATABASE_URL")
    db_init.add_argument("--config", type=Path, required=True)

    clear = commands.add_parser("clear", help="Back up, then delete incident history (log cursor is kept)")
    clear.add_argument("--config", type=Path, required=True)
    clear.add_argument("--yes", action="store_true", help="Required: confirm deleting all incidents")
    clear.add_argument("--delete-reports", action="store_true", help="Also delete Markdown reports in reports_dir")

    importer = commands.add_parser("import-sqlite", help="Copy an existing SQLite state file into the configured database")
    importer.add_argument("--config", type=Path, required=True)
    importer.add_argument("--from", dest="source", type=Path, help="SQLite file (defaults to state_db)")
    return root


def main(argv: Optional[list] = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        config = Config.load(arguments.config)
        if arguments.command == "watch":
            return watch(config, from_start=arguments.from_start, once=arguments.once)
        if arguments.command == "status":
            return status(config)
        if arguments.command == "preflight":
            return preflight(config)
        if arguments.command == "retry":
            return retry(config, arguments.incident)
        if arguments.command == "ui":
            from .dashboard import serve

            return serve(config, port=arguments.port)
        if arguments.command == "release":
            return release_incident(config, arguments.incident)
        if arguments.command == "telegram-test":
            return telegram_test(config)
        if arguments.command == "db-init":
            return database_init(config)
        if arguments.command == "clear":
            return clear_state(config, arguments.yes, arguments.delete_reports)
        if arguments.command == "import-sqlite":
            return import_sqlite(config, arguments.source)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        stage("ERROR", "Command failed", reason="{}: {}".format(error.__class__.__name__, error))
        return 1
    return 0


def open_store(config: Config) -> StateStore:
    return StateStore(config.database)


def watch(config: Config, from_start: bool, once: bool) -> int:
    store = open_store(config)
    set_sink(store.add_event)
    try:
        stage("OBSERVE", "State store connected", database=store.describe())
        source = build_source(config)
        if from_start:
            store.reset_cursor(source.key)
        saved = store.cursor(source.key)
        cursor = Cursor(*saved) if saved else source.initial(config.start_at_end and not from_start)
        store.save_cursor(source.key, cursor.identity, cursor.offset)
        tailer = LogTailer(source, cursor)
        pipeline = AgentPipeline(config, store)
        source_commit = pipeline.resolve_source()
        source_checked_at = time.monotonic()
        runtime = runtime_settings.load(store, config)
        pipeline.apply_settings(runtime)
        stage("OBSERVE", "Watching Laravel log", source=source.label)
        stage("OBSERVE", "Settings", mode=runtime.mode, threshold=runtime.threshold, window="{}s".format(runtime.window_seconds))
        stage("OBSERVE", "Source baseline", branch=config.base_branch, commit=source_commit[:12])

        running = True
        last_pr_sync: Optional[float] = None
        source_failing = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        while running:
            # Settings are re-read every poll so dashboard changes apply without a restart.
            latest = runtime_settings.load(store, config)
            if latest != runtime:
                runtime = latest
                pipeline.apply_settings(runtime)
                stage("OBSERVE", "Settings changed", mode=runtime.mode, threshold=runtime.threshold, window="{}s".format(runtime.window_seconds))
            try:
                events, cursor, polled = tailer.poll()
            except Exception as error:
                # A remote log source being down must not stop the watcher; retry next interval.
                if not source_failing:
                    stage("ERROR", "Log source unavailable; retrying", source=source.label, reason="{}: {}".format(error.__class__.__name__, error))
                source_failing = True
                events, polled = [], False
            else:
                if polled and source_failing:
                    stage("OBSERVE", "Log source recovered", source=source.label)
                    source_failing = False
            if polled:
                store.save_cursor(source.key, cursor.identity, cursor.offset)
            else:
                store.touch_cursor(source.key)
            matching = [event for event in events if matches_any(event, config.include_patterns)]
            if matching and time.monotonic() - source_checked_at >= SOURCE_REFRESH_SECONDS:
                source_checked_at = time.monotonic()
                try:
                    latest_commit = pipeline.resolve_source()
                except Exception as error:
                    stage("ERROR", "Could not refresh source baseline; keeping previous", reason="{}: {}".format(error.__class__.__name__, error))
                else:
                    if latest_commit != source_commit:
                        stage("OBSERVE", "Source baseline changed", branch=config.base_branch, previous=source_commit[:12], commit=latest_commit[:12])
                        source_commit = latest_commit
            for event in matching:
                decision = store.record_event(
                    event,
                    source_commit=source_commit,
                    threshold=runtime.threshold,
                    window_seconds=runtime.window_seconds,
                    mode=runtime.mode,
                )
                with scope(incident_id=decision.incident_id):
                    if decision.suppressed:
                        stage(
                            "OBSERVE",
                            "Error repeated; already reported, waiting for a human decision"
                            if decision.status == "reported"
                            else "Error repeated but a fix already exists; not starting another run",
                            incident=decision.incident_id[:8],
                            status=decision.status,
                            pull_request=decision.pull_request_url or "-",
                        )
                        pipeline.notify_repeat(decision.incident_id, decision.suppressed_count)
                        continue
                    stage(
                        "OBSERVE",
                        "Matching error observed",
                        incident=decision.incident_id[:8],
                        count="{}/{}".format(decision.occurrence_count, decision.threshold),
                    )
                    if decision.reported:
                        stage("TRIGGER", "Threshold reached; report_only mode, waiting for a human to start the fix")
                        pipeline.report(decision.incident_id)
                    if decision.should_trigger:
                        stage("TRIGGER", "Threshold reached; starting isolated agent run")
                        pipeline.run(decision.incident_id)
            if last_pr_sync is None or time.monotonic() - last_pr_sync >= config.pr_sync_interval_seconds:
                last_pr_sync = time.monotonic()
                pipeline.sync_pull_requests()
            # Incidents re-queued from the dashboard are executed here, by the only process
            # that holds Claude/GitHub capabilities.
            for incident_id in store.queued():
                with scope(incident_id=incident_id):
                    stage("TRIGGER", "Running queued incident", incident=incident_id[:8])
                    pipeline.run(incident_id)
            if once:
                break
            time.sleep(config.poll_interval_seconds)
        stage("OBSERVE", "Watcher stopped")
        return 0
    finally:
        set_sink(None)
        store.close()


def status(config: Config) -> int:
    store = open_store(config)
    try:
        rows = store.recent()
        if not rows:
            print("No incidents recorded.")
            return 0
        for row in rows:
            print(
                "{id:.8}  {status:<18} {occurrence_count}/{threshold}  {branch}".format(
                    id=row["id"],
                    status=row["status"],
                    occurrence_count=row["occurrence_count"],
                    threshold=row["threshold"],
                    branch=row["branch_name"] or "-",
                )
            )
            if row["report_path"]:
                print("          report: {}".format(row["report_path"]))
            if row["pull_request_url"]:
                print("          pull request: {}".format(row["pull_request_url"]))
            if row["failure_reason"]:
                print("          failure: {}".format(row["failure_reason"]))
            if row.get("suppressed_count"):
                print("          repeated since handled (not re-run): {}".format(row["suppressed_count"]))
        return 0
    finally:
        store.close()


def preflight(config: Config) -> int:
    pipeline_store = open_store(config)
    try:
        stage("OBSERVE", "State store connected", database=pipeline_store.describe())
        pipeline = AgentPipeline(config, pipeline_store)
        commit = pipeline.resolve_source()
        version = pipeline.claude.preflight()
        github = pipeline.publisher.preflight() if pipeline.publisher.enabled() else None
        telegram = pipeline.notifier.preflight() if pipeline.notifier.enabled() else None
    finally:
        pipeline_store.close()
    if github is not None:
        if github.get("base_sha") != commit:
            raise RuntimeError("fetched GitHub base does not match the API ref")
    stage("DONE", "Preflight passed", branch=config.base_branch, commit=commit[:12], claude=version)
    stage("DONE", "Telegram", bot=telegram or "disabled", events=",".join(config.telegram_events) if telegram else "-")
    if github is not None:
        stage(
            "DONE",
            "GitHub publisher preflight passed",
            repository=github.get("full_name"),
            default_branch=github.get("default_branch"),
            base_sha=str(github.get("base_sha"))[:12],
        )
    return 0


def retry(config: Config, incident_id: str) -> int:
    store = open_store(config)
    set_sink(store.add_event)
    try:
        resolved = store.resolve_prefix(incident_id)
        if not store.requeue_failed(resolved):
            raise RuntimeError("incident is not in failed or reported status")
        pipeline = AgentPipeline(config, store)
        pipeline.apply_settings(runtime_settings.load(store, config))
        with scope(incident_id=resolved):
            stage("TRIGGER", "Starting a new agent run for the incident", incident=resolved[:8])
            pipeline.run(resolved)
        return 0
    finally:
        set_sink(None)
        store.close()


def release_incident(config: Config, incident_id: str) -> int:
    store = open_store(config)
    set_sink(store.add_event)
    try:
        resolved = store.resolve_prefix(incident_id)
        if not store.release(resolved):
            raise RuntimeError("only waiting_for_review, merged or dismissed incidents can be released")
        with scope(incident_id=resolved):
            stage("TRIGGER", "Released; the next occurrences may start a new auto-fix", incident=resolved[:8])
        return 0
    finally:
        set_sink(None)
        store.close()


def telegram_test(config: Config) -> int:
    from .notifier import TelegramNotifier

    if not config.telegram_enabled:
        raise RuntimeError("set AI_FIX_TELEGRAM_ENABLED=true in .env first")
    notifier = TelegramNotifier(config)
    bot = notifier.preflight()
    notifier.send_test()
    stage("DONE", "Telegram test message sent", bot=bot, chat=config.telegram_chat_id)
    return 0


def database_init(config: Config) -> int:
    import os

    from .database import DatabaseUrl
    from .dbsetup import ENV_KEY, ensure_env_url, generated_target, init_database, prompt_admin

    url = os.environ.get(ENV_KEY, "").strip()
    generated = not url
    if generated:
        admin = prompt_admin()
        url = generated_target(host=admin.host, port=admin.port)
        stage("OBSERVE", "{} is not set; generated a dedicated user with a random password".format(ENV_KEY))
    else:
        existing = DatabaseUrl.parse(url)
        admin = prompt_admin(existing.host, existing.port)
    target = DatabaseUrl.parse(url)
    for action in init_database(admin, target):
        stage("DONE", action)
    if generated and ensure_env_url(config.project_root / ".env", url):
        stage("DONE", "Wrote {} to .env (mode 0600)".format(ENV_KEY))

    store = StateStore(target)  # connects as the orchestrator user and creates the tables
    try:
        store.recent(limit=1)
    finally:
        store.close()
    stage("DONE", "State store ready", database=target.describe())
    stage("DONE", "Restart `make watch` and `make ui` so they pick up the MySQL connection")
    return 0


def clear_state(config: Config, confirmed: bool, delete_reports: bool) -> int:
    if not confirmed:
        raise RuntimeError("refusing to clear without --yes")
    from .dashboard import DashboardApi

    status, payload = DashboardApi(config).clear(delete_reports)
    if status.value >= 400:
        raise RuntimeError(payload["error"])
    stage("DONE", "State cleared", backup=payload["backup"], reports_deleted=payload["reports_deleted"], **payload["deleted"])
    return 0


def import_sqlite(config: Config, source: Optional[Path]) -> int:
    path = (source or config.state_db).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("SQLite state file not found: {}".format(path))
    if config.database.dialect != "mysql":
        raise RuntimeError("set AI_FIX_DATABASE_URL to a mysql:// URL before importing")
    legacy = StateStore(path)
    target = open_store(config)
    try:
        copied = target.import_from(legacy)
    finally:
        legacy.close()
        target.close()
    stage("DONE", "SQLite state imported", database=config.database.describe(), **copied)
    return 0
