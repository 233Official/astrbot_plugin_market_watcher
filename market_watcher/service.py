"""Market-check orchestration with baseline and save-before-send guarantees."""

from __future__ import annotations

import asyncio
import hashlib
import time
from copy import deepcopy
from typing import Any, Protocol

from .ai import AiIntroService
from .detect import detect_changes
from .github import GitHubGateway, GitHubMetadataService
from .merge import MERGED_FIELDS, SOURCE_PRIORITY, merge_sources
from .models import (
    DeliveryBatch,
    DeliveryStatus,
    DeliveryTargetState,
    EndpointSnapshot,
    FetchResult,
    RunReport,
    SourceKind,
    SourceState,
    WatcherState,
)
from .normalize import utc_now
from .outbox import (
    Notifier,
    count_exhausted,
    count_pending,
    create_batches,
    deliver_pending,
    merge_targets,
    validate_targets,
)
from .state import StateError

OUTBOX_DIAGNOSTIC_PREFIX = "diagnostic:outbox:"
OUTBOX_DIAGNOSTIC_HOLD = "9999-12-31T23:59:59Z"


class Fetcher(Protocol):
    async def fetch(self, previous: SourceState | None = None) -> FetchResult: ...


class Store(Protocol):
    def load(self) -> WatcherState: ...

    def save(self, state: WatcherState) -> None: ...


class MarketWatcherService:
    def __init__(
        self,
        *,
        store: Store,
        fetchers: dict[SourceKind, Fetcher],
        notifier: Notifier,
        github_gateway: GitHubGateway | None = None,
        github_metadata: GitHubMetadataService | None = None,
        ai_intro: AiIntroService | None = None,
        clock=utc_now,
        monotonic=time.monotonic,
        observer=None,
    ) -> None:
        self.store = store
        self.fetchers = fetchers
        self.notifier = notifier
        self.github_gateway = github_gateway
        self.github_metadata = github_metadata
        self.ai_intro = ai_intro
        self.clock = clock
        self.monotonic = monotonic
        self.observer = observer or (lambda payload: None)
        self.lock = asyncio.Lock()
        self.last_report: RunReport | None = None

    async def prepare_outbox_diagnostic(self, target: str) -> dict[str, int]:
        clean_targets, invalid = validate_targets([target])
        if invalid or len(clean_targets) != 1:
            raise ValueError("invalid diagnostic target")
        normalized_target = clean_targets[0]
        batch_id = _diagnostic_batch_id(normalized_target)
        async with self.lock:
            state = self.store.load()
            if batch_id not in state.outbox:
                state.outbox[batch_id] = DeliveryBatch(
                    batch_id=batch_id,
                    event_ids=(),
                    message=(
                        "【Market Watcher】出站箱跨重启诊断：如果你看到此消息，"
                        "持久化 pending outbox 已通过生产投递链路发送。"
                    ),
                    created_at=self.clock(),
                    targets={
                        normalized_target: DeliveryTargetState(
                            target=normalized_target,
                            next_retry_at=OUTBOX_DIAGNOSTIC_HOLD,
                        )
                    },
                )
                self.store.save(state)
            return _diagnostic_counts(state)

    async def outbox_diagnostic_status(self) -> dict[str, int]:
        async with self.lock:
            return _diagnostic_counts(self.store.load())

    async def deliver_outbox_diagnostic(self) -> dict[str, int]:
        async with self.lock:
            state = self.store.load()
            released = 0
            for batch_id, batch in state.outbox.items():
                if not batch_id.startswith(OUTBOX_DIAGNOSTIC_PREFIX):
                    continue
                for target_state in batch.targets.values():
                    if target_state.status in {
                        DeliveryStatus.PENDING,
                        DeliveryStatus.FAILED,
                    }:
                        target_state.next_retry_at = None
                        released += 1
            if released:
                self.store.save(state)
            sent, _ = await deliver_pending(
                state,
                self.store,
                self.notifier,
                now=self.clock(),
                clock=self.clock,
            )
            result = _diagnostic_counts(state)
            result["released"] = released
            result["sent_this_run"] = sent
            return result

    async def cleanup_outbox_diagnostic(self) -> dict[str, int]:
        async with self.lock:
            state = self.store.load()
            diagnostic_ids = [
                batch_id
                for batch_id in state.outbox
                if batch_id.startswith(OUTBOX_DIAGNOSTIC_PREFIX)
            ]
            for batch_id in diagnostic_ids:
                del state.outbox[batch_id]
            self.store.save(state)
            result = _diagnostic_counts(state)
            result["removed"] = len(diagnostic_ids)
            return result

    async def check(
        self,
        *,
        enabled_sources: set[SourceKind],
        push_targets: object,
        max_items_per_push: int,
        include_star_count: bool = True,
        enable_ai_summary: bool = False,
        llm_provider_id: str = "",
        provider_origin: str | None = None,
        enable_image_card: bool = False,
    ) -> RunReport:
        if self.lock.locked():
            report = RunReport(
                status="busy",
                started_at=self.clock(),
                error_code="run_skipped_busy",
                busy=True,
            )
            report.run_id = _run_id(report.started_at)
            return report
        await self.lock.acquire()
        overall_started = self.monotonic()
        try:
            report = await self._run(
                enabled_sources,
                push_targets,
                max_items_per_push,
                include_star_count,
                enable_ai_summary,
                llm_provider_id,
                provider_origin,
                overall_started,
                enable_image_card=enable_image_card,
            )
            if report.phase_durations_ms["overall"] == 0:
                self._record_phase(report, "overall", overall_started)
            self.last_report = report
            return report
        finally:
            self.lock.release()

    async def _run(
        self,
        enabled_sources: set[SourceKind],
        push_targets: object,
        max_items_per_push: int,
        include_star_count: bool,
        enable_ai_summary: bool,
        llm_provider_id: str,
        provider_origin: str | None,
        overall_started: float,
        enable_image_card: bool = False,
    ) -> RunReport:
        report = RunReport(status="running", started_at=self.clock())
        report.run_id = _run_id(report.started_at)
        try:
            state = self.store.load()
        except (StateError, ValueError, OSError):
            report.status = "state_error"
            report.error_code = "state_load_failed"
            report.finished_at = self.clock()
            return report

        try:
            save_started = self.monotonic()
            self.store.save(state)
            self._record_phase(report, "save", save_started)
        except (StateError, OSError, ValueError):
            report.status = "state_save_failed"
            report.error_code = "state_save_failed"
            report.finished_at = self.clock()
            return report

        try:
            deliver_started = self.monotonic()
            sent, _ = await deliver_pending(
                state, self.store, self.notifier, now=self.clock(), clock=self.clock
            )
            self._record_phase(report, "deliver", deliver_started)
        except (StateError, OSError, ValueError):
            report.status = "delivery_state_save_failed"
            report.error_code = "state_save_failed_after_delivery"
            report.finished_at = self.clock()
            return report
        report.targets_sent = sent
        report.targets_pending = count_pending(state)
        report.targets_exhausted = count_exhausted(state)

        if self.github_gateway:
            self.github_gateway.begin_run(state)

        valid_targets, report.invalid_targets = merge_targets(
            push_targets, state.subscriptions
        )
        if report.invalid_targets:
            report.target_error_code = "invalid_push_targets"

        previous_plugins = deepcopy(state.plugins)
        baseline_sources: set[SourceKind] = set()
        successful = attempted = 0
        source_order = (
            SourceKind.COLLECTION_ISSUE,
            SourceKind.LEGACY_PUBLISH_ISSUE,
            SourceKind.MARKET,
        )
        fetch_started = self.monotonic()
        for kind in source_order:
            if kind not in enabled_sources:
                continue
            fetcher = self.fetchers.get(kind)
            if fetcher is None:
                continue
            attempted += 1
            old_state = state.sources.get(kind.value)
            try:
                result = await fetcher.fetch(old_state)
            except Exception:
                result = FetchResult(kind, False, False, error_code="fetch_exception")
            if result.success and result.complete and result.endpoint:
                successful += 1
                report.sources_succeeded += 1
                report.observations += len(result.observations)
                if not (old_state and old_state.baseline_established):
                    baseline_sources.add(kind)
                _record_aliases(state, old_state, result)
                snapshots = deepcopy(old_state.snapshots) if old_state else {}
                snapshot = EndpointSnapshot(
                    endpoint=result.endpoint,
                    pages_fetched=max(1, result.pages_fetched),
                    etag=result.etag,
                    last_modified=result.last_modified,
                    observations={
                        item.source_record_id: item for item in result.observations
                    },
                )
                snapshots[result.endpoint] = snapshot
                state.sources[kind.value] = SourceState(
                    baseline_established=True,
                    last_success_at=self.clock(),
                    complete=True,
                    observations=snapshot.observations,
                    snapshots=snapshots,
                )
            else:
                report.sources_failed += 1
                if old_state:
                    preserved = deepcopy(old_state)
                    preserved.complete = False
                    preserved.error_code = result.error_code or "fetch_failed"
                    state.sources[kind.value] = preserved
                else:
                    state.sources[kind.value] = SourceState(
                        baseline_established=False,
                        complete=False,
                        error_code=result.error_code or "fetch_failed",
                    )
        self._record_phase(report, "fetch", fetch_started)

        merge_started = self.monotonic()
        previous_plugins = _resolve_previous_plugins(previous_plugins, state.id_aliases)
        state.plugins = merge_sources(
            deepcopy(state.sources), previous_plugins, state.id_aliases
        )
        self._record_phase(report, "merge", merge_started)
        detect_started = self.monotonic()
        preliminary_events = (
            detect_changes(previous_plugins, state.plugins, detected_at=self.clock())
            if attempted and successful
            else []
        )
        preliminary_events = _filter_silent_baselines(
            preliminary_events, baseline_sources
        )
        self._record_phase(report, "detect", detect_started)
        if self.github_metadata:
            github_started = self.monotonic()
            await self.github_metadata.enrich(
                state,
                state.plugins,
                {event.canonical_id for event in preliminary_events},
                include_star_count=include_star_count,
            )
            self._record_phase(report, "github", github_started)

        if SourceKind.GITHUB_DISCOVERY in enabled_sources:
            kind = SourceKind.GITHUB_DISCOVERY
            fetcher = self.fetchers.get(kind)
            if fetcher is not None:
                fetch_started = self.monotonic()
                attempted += 1
                old_state = state.sources.get(kind.value)
                try:
                    result = await fetcher.fetch(old_state)
                except Exception:
                    result = FetchResult(
                        kind, False, False, error_code="fetch_exception"
                    )
                if result.success and result.complete and result.endpoint:
                    successful += 1
                    report.sources_succeeded += 1
                    report.observations += len(result.observations)
                    if not (old_state and old_state.baseline_established):
                        baseline_sources.add(kind)
                    _record_aliases(state, old_state, result)
                    snapshots = deepcopy(old_state.snapshots) if old_state else {}
                    snapshot = EndpointSnapshot(
                        endpoint=result.endpoint,
                        pages_fetched=max(1, result.pages_fetched),
                        etag=result.etag,
                        last_modified=result.last_modified,
                        observations={
                            item.source_record_id: item for item in result.observations
                        },
                    )
                    snapshots[result.endpoint] = snapshot
                    state.sources[kind.value] = SourceState(
                        baseline_established=True,
                        last_success_at=self.clock(),
                        complete=True,
                        observations=snapshot.observations,
                        snapshots=snapshots,
                    )
                else:
                    report.sources_failed += 1
                    if old_state:
                        preserved = deepcopy(old_state)
                        preserved.complete = False
                        preserved.error_code = result.error_code or "fetch_failed"
                        state.sources[kind.value] = preserved
                    else:
                        state.sources[kind.value] = SourceState(
                            baseline_established=False,
                            complete=False,
                            error_code=result.error_code or "fetch_failed",
                        )
                self._record_phase(report, "fetch", fetch_started)
                merge_started = self.monotonic()
                state.plugins = merge_sources(
                    deepcopy(state.sources), state.plugins, state.id_aliases
                )
                self._record_phase(report, "merge", merge_started)
                if self.github_metadata:
                    github_started = self.monotonic()
                    self.github_metadata.apply_cache(
                        state,
                        state.plugins,
                        include_star_count=include_star_count,
                    )
                    self._record_phase(report, "github", github_started)
        events = []
        if attempted and successful:
            detect_started = self.monotonic()
            events = detect_changes(
                previous_plugins, state.plugins, detected_at=self.clock()
            )
            events = _filter_silent_baselines(events, baseline_sources)
            self._record_phase(report, "detect", detect_started)

        report.discovered = sum(event.kind.value == "discovered" for event in events)
        report.updated = sum(event.kind.value == "updated" for event in events)
        intro = None
        ai_started = self.monotonic()
        if not enable_ai_summary:
            report.ai_status = "disabled"
        elif not events:
            report.ai_status = "skipped"
            report.ai_error_code = "ai_no_events"
        elif self.ai_intro is None:
            report.ai_status = "fallback"
            report.ai_error_code = "ai_unavailable"
        else:
            ai_result = await self.ai_intro.generate(
                events,
                enabled=True,
                provider_id=llm_provider_id,
                provider_origin=provider_origin
                or (valid_targets[0] if valid_targets else None),
            )
            report.ai_status = ai_result.status
            report.ai_error_code = ai_result.error_code
            intro = ai_result.intro
        self._record_phase(report, "ai", ai_started)
        batches = create_batches(
            events,
            valid_targets,
            max_items=max_items_per_push,
            created_at=self.clock(),
            intro=intro,
            enable_image_card=enable_image_card,
        )
        inserted = 0
        for batch in batches:
            if batch.batch_id not in state.outbox:
                state.outbox[batch.batch_id] = batch
                inserted += 1
        report.batches_created = inserted
        report.status = (
            "failed"
            if attempted and successful == 0
            else "partial"
            if report.sources_failed
            else "success"
        )
        report.targets_pending = count_pending(state)
        report.targets_exhausted = count_exhausted(state)
        if self.github_gateway:
            report.github_requests_used = self.github_gateway.used
            report.github_requests_remaining = self.github_gateway.remaining
            report.github_error_code = state.github.rate_limit.error_code
        state.last_run = report.to_dict()
        try:
            save_started = self.monotonic()
            self.store.save(state)
            self._record_phase(report, "save", save_started)
        except (StateError, OSError, ValueError):
            report.status = "state_save_failed"
            report.error_code = "state_save_failed"
            return report
        try:
            deliver_started = self.monotonic()
            sent, _ = await deliver_pending(
                state, self.store, self.notifier, now=self.clock(), clock=self.clock
            )
            self._record_phase(report, "deliver", deliver_started)
        except (StateError, OSError, ValueError):
            report.status = "delivery_state_save_failed"
            report.error_code = "state_save_failed_after_delivery"
            return report
        report.targets_sent += sent
        report.targets_pending = count_pending(state)
        report.targets_exhausted = count_exhausted(state)
        # This is the final operational state save. Completion telemetry is
        # checkpointed once afterward and is outside the measured run boundary.
        state.last_run = report.to_dict()
        try:
            save_started = self.monotonic()
            self.store.save(state)
            self._record_phase(report, "save", save_started)
        except (StateError, OSError, ValueError):
            report.status = "delivery_state_save_failed"
            report.error_code = "state_save_failed_after_delivery"
            report.finished_at = self.clock()
            self._record_phase(report, "overall", overall_started)
            return report
        report.finished_at = self.clock()
        state.updated_at = report.finished_at
        self._record_phase(report, "overall", overall_started)
        state.last_run = report.to_dict()
        try:
            self.store.save(state)
        except (StateError, OSError, ValueError):
            report.status = "delivery_state_save_failed"
            report.error_code = "state_save_failed_after_delivery"
        return report

    def _record_phase(self, report: RunReport, phase: str, started: float) -> None:
        duration = max(0, int(round((self.monotonic() - started) * 1000)))
        if phase == "overall":
            measured_phases = sum(
                value
                for name, value in report.phase_durations_ms.items()
                if name != "overall"
            )
            duration = max(1, duration, measured_phases)
        report.phase_durations_ms[phase] += duration
        payload: dict[str, Any] = {
            "run_id": report.run_id,
            "phase": phase,
            "duration_ms": duration,
            "events": report.discovered + report.updated,
            "sources_succeeded": report.sources_succeeded,
            "sources_failed": report.sources_failed,
            "error_code": report.error_code
            or report.ai_error_code
            or report.github_error_code,
        }
        try:
            self.observer(payload)
        except Exception:
            pass


def _record_aliases(
    state: WatcherState, old_state: SourceState | None, result: FetchResult
) -> None:
    if not old_state:
        return
    for observation in result.observations:
        old = old_state.observations.get(observation.source_record_id)
        if (
            old
            and old.canonical_id != observation.canonical_id
            and old.canonical_id.startswith("source:")
            and observation.canonical_id.startswith("github:")
        ):
            state.id_aliases[old.canonical_id] = observation.canonical_id


def _filter_silent_baselines(events, baseline_sources: set[SourceKind]):
    return [
        event
        for event in events
        if not (
            event.kind.value == "discovered"
            and event.current.evidence
            and {evidence.source_kind for evidence in event.current.evidence}.issubset(
                baseline_sources
            )
        )
    ]


def _run_id(started_at: str) -> str:
    digest = hashlib.sha256(started_at.encode("utf-8")).hexdigest()[:12]
    return f"run:{digest}"


def _diagnostic_batch_id(target: str) -> str:
    return f"{OUTBOX_DIAGNOSTIC_PREFIX}{hashlib.sha256(target.encode()).hexdigest()}"


def _diagnostic_counts(state: WatcherState) -> dict[str, int]:
    counts = {
        "count": 0,
        "pending": 0,
        "failed": 0,
        "sent": 0,
        "exhausted": 0,
    }
    for batch_id, batch in state.outbox.items():
        if not batch_id.startswith(OUTBOX_DIAGNOSTIC_PREFIX):
            continue
        counts["count"] += 1
        for target_state in batch.targets.values():
            counts[target_state.status.value] += 1
    return counts


def _resolve_previous_plugins(plugins, aliases):
    grouped = {}
    for canonical_id, plugin in plugins.items():
        visited: set[str] = set()
        target = canonical_id
        while (
            target in aliases
            and target not in visited
            and target.startswith("source:")
            and aliases[target].startswith("github:")
        ):
            visited.add(target)
            target = aliases[target]
        grouped.setdefault(target, []).append(plugin)

    resolved = {}
    for target, records in sorted(grouped.items()):
        copied = deepcopy(sorted(records, key=lambda item: item.canonical_id)[0])
        copied.canonical_id = target
        evidence = {
            (item.source_kind.value, item.source_record_id, item.source_url): item
            for record in records
            for item in record.evidence
        }
        copied.evidence = tuple(
            sorted(
                evidence.values(),
                key=lambda item: (
                    SOURCE_PRIORITY[item.source_kind],
                    item.source_record_id,
                    item.source_url,
                ),
            )
        )
        for field_name in MERGED_FIELDS:
            candidates = []
            for record in records:
                value = getattr(record, field_name)
                if value is None or value == "" or value == ():
                    continue
                source = record.field_sources.get(field_name)
                priority = SOURCE_PRIORITY.get(source.source_kind, 99) if source else 99
                candidates.append((priority, record.canonical_id, value, source))
            if candidates:
                _, _, value, source = min(candidates, key=lambda item: item[:2])
                setattr(copied, field_name, value)
                if source:
                    copied.field_sources[field_name] = source
        copied.first_seen_at = min(
            (record.first_seen_at for record in records if record.first_seen_at),
            default=copied.first_seen_at,
        )
        copied.last_seen_at = max(
            (record.last_seen_at for record in records if record.last_seen_at),
            default=copied.last_seen_at,
        )
        copied.content_hash = copied.compute_content_hash()
        resolved[target] = copied
    return resolved
