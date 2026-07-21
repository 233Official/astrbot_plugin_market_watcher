"""Strict M1 domain and schema-version 1 persistence models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

RAW_EXCERPT_MAX_BYTES = 8 * 1024


class ModelValidationError(ValueError):
    """Serialized model data does not satisfy the schema contract."""


class SourceKind(str, Enum):
    MARKET = "market"
    COLLECTION_ISSUE = "collection_issue"
    LEGACY_PUBLISH_ISSUE = "legacy_publish_issue"
    GITHUB_DISCOVERY = "github_discovery"


class ChangeKind(str, Enum):
    DISCOVERED = "discovered"
    UPDATED = "updated"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


@dataclass(slots=True)
class SourceEvidence:
    source_kind: SourceKind
    source_record_id: str
    source_url: str
    observed_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "observed_at": self.observed_at,
            "source_kind": self.source_kind.value,
            "source_record_id": self.source_record_id,
            "source_url": self.source_url,
        }

    @classmethod
    def from_dict(cls, value: Any, path: str = "evidence") -> SourceEvidence:
        data = _dict(value, path)
        _required(
            data, {"source_kind", "source_record_id", "source_url", "observed_at"}, path
        )
        return cls(
            source_kind=_source_kind(data["source_kind"], f"{path}.source_kind"),
            source_record_id=_str(data["source_record_id"], f"{path}.source_record_id"),
            source_url=_str(data["source_url"], f"{path}.source_url"),
            observed_at=_str(data["observed_at"], f"{path}.observed_at"),
        )


@dataclass(slots=True)
class SourceObservation:
    source_kind: SourceKind
    source_record_id: str
    source_url: str
    observed_at: str
    fetched_from: str
    canonical_id: str
    repo_url: str | None = None
    repo_owner: str | None = None
    repo_name: str | None = None
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    author: str | None = None
    version: str | None = None
    astrbot_version: str | None = None
    platforms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    market_status: str | None = None
    issue_state: str | None = None
    issue_labels: tuple[str, ...] = ()
    stars: int | None = None
    forks: int | None = None
    archived: bool | None = None
    repo_updated_at: str | None = None
    observation_hash: str = ""
    sparse: bool = False
    raw_excerpt: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_kind"] = self.source_kind.value
        for key in ("platforms", "tags", "issue_labels"):
            data[key] = list(data[key])
        _validate_excerpt(data["raw_excerpt"], "observation.raw_excerpt")
        return data

    @classmethod
    def from_dict(cls, value: Any, path: str = "observation") -> SourceObservation:
        data = _dict(value, path)
        fields = {
            "source_kind",
            "source_record_id",
            "source_url",
            "observed_at",
            "fetched_from",
            "canonical_id",
            "repo_url",
            "repo_owner",
            "repo_name",
            "name",
            "display_name",
            "description",
            "author",
            "version",
            "astrbot_version",
            "platforms",
            "tags",
            "market_status",
            "issue_state",
            "issue_labels",
            "stars",
            "forks",
            "archived",
            "repo_updated_at",
            "observation_hash",
            "sparse",
            "raw_excerpt",
        }
        _required(data, fields, path)
        excerpt = _dict(data["raw_excerpt"], f"{path}.raw_excerpt")
        _validate_excerpt(excerpt, f"{path}.raw_excerpt")
        return cls(
            source_kind=_source_kind(data["source_kind"], f"{path}.source_kind"),
            source_record_id=_str(data["source_record_id"], f"{path}.source_record_id"),
            source_url=_str(data["source_url"], f"{path}.source_url"),
            observed_at=_str(data["observed_at"], f"{path}.observed_at"),
            fetched_from=_str(data["fetched_from"], f"{path}.fetched_from"),
            canonical_id=_str(data["canonical_id"], f"{path}.canonical_id"),
            repo_url=_optional_str(data["repo_url"], f"{path}.repo_url"),
            repo_owner=_optional_str(data["repo_owner"], f"{path}.repo_owner"),
            repo_name=_optional_str(data["repo_name"], f"{path}.repo_name"),
            name=_optional_str(data["name"], f"{path}.name"),
            display_name=_optional_str(data["display_name"], f"{path}.display_name"),
            description=_optional_str(data["description"], f"{path}.description"),
            author=_optional_str(data["author"], f"{path}.author"),
            version=_optional_str(data["version"], f"{path}.version"),
            astrbot_version=_optional_str(
                data["astrbot_version"], f"{path}.astrbot_version"
            ),
            platforms=_str_tuple(data["platforms"], f"{path}.platforms"),
            tags=_str_tuple(data["tags"], f"{path}.tags"),
            market_status=_optional_str(data["market_status"], f"{path}.market_status"),
            issue_state=_optional_str(data["issue_state"], f"{path}.issue_state"),
            issue_labels=_str_tuple(data["issue_labels"], f"{path}.issue_labels"),
            stars=_optional_int(data["stars"], f"{path}.stars"),
            forks=_optional_int(data["forks"], f"{path}.forks"),
            archived=_optional_bool(data["archived"], f"{path}.archived"),
            repo_updated_at=_optional_str(
                data["repo_updated_at"], f"{path}.repo_updated_at"
            ),
            observation_hash=_str(data["observation_hash"], f"{path}.observation_hash"),
            sparse=_bool(data["sparse"], f"{path}.sparse"),
            raw_excerpt=excerpt,
        )


@dataclass(slots=True)
class EndpointSnapshot:
    endpoint: str
    pages_fetched: int = 1
    etag: str | None = None
    last_modified: str | None = None
    observations: dict[str, SourceObservation] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "pages_fetched": self.pages_fetched,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "observations": {
                key: self.observations[key].to_dict()
                for key in sorted(self.observations)
            },
        }

    @classmethod
    def from_dict(cls, value: Any, path: str) -> EndpointSnapshot:
        data = _dict(value, path)
        _required(
            data,
            {"endpoint", "pages_fetched", "etag", "last_modified", "observations"},
            path,
        )
        observations_data = _dict(data["observations"], f"{path}.observations")
        observations = {
            _str(key, f"{path}.observations key"): SourceObservation.from_dict(
                item, f"{path}.observations[{key!r}]"
            )
            for key, item in observations_data.items()
        }
        endpoint = _str(data["endpoint"], f"{path}.endpoint")
        pages_fetched = _int(data["pages_fetched"], f"{path}.pages_fetched")
        if pages_fetched < 1:
            raise ModelValidationError(f"{path}.pages_fetched must be positive")
        if any(
            key != observation.source_record_id
            for key, observation in observations.items()
        ):
            raise ModelValidationError(
                f"{path}.observation keys must match source_record_id"
            )
        return cls(
            endpoint=endpoint,
            pages_fetched=pages_fetched,
            etag=_optional_str(data["etag"], f"{path}.etag"),
            last_modified=_optional_str(data["last_modified"], f"{path}.last_modified"),
            observations=observations,
        )


@dataclass(slots=True)
class FetchResult:
    source_kind: SourceKind
    success: bool
    complete: bool
    observations: list[SourceObservation] = field(default_factory=list)
    endpoint: str | None = None
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    pages_fetched: int = 0
    records_received: int = 0
    records_rejected: int = 0
    not_modified: bool = False
    from_fallback: bool = False
    error_code: str | None = None
    error_summary: str | None = None


@dataclass(slots=True)
class PluginRecord:
    canonical_id: str
    name: str
    repo_url: str | None = None
    repo_owner: str | None = None
    repo_name: str | None = None
    display_name: str | None = None
    description: str | None = None
    author: str | None = None
    version: str | None = None
    astrbot_version: str | None = None
    platforms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    market_status: str | None = None
    issue_state: str | None = None
    issue_labels: tuple[str, ...] = ()
    stars: int | None = None
    forks: int | None = None
    archived: bool | None = None
    repo_updated_at: str | None = None
    github_metadata_fetched_at: str | None = None
    github_metadata_status: str | None = None
    observed_at: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    content_hash: str | None = None
    field_sources: dict[str, SourceEvidence] = field(default_factory=dict)
    evidence: tuple[SourceEvidence, ...] = ()

    def compute_content_hash(self) -> str:
        """Return the M2 update hash for the FSD substantive-field whitelist."""
        payload = {
            key: getattr(self, key)
            for key in (
                "display_name",
                "description",
                "author",
                "version",
                "repo_url",
                "astrbot_version",
                "platforms",
                "market_status",
                "issue_state",
                "issue_labels",
                "archived",
            )
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("platforms", "tags", "issue_labels"):
            data[key] = list(getattr(self, key))
        data["field_sources"] = {
            key: value.to_dict() for key, value in sorted(self.field_sources.items())
        }
        data["evidence"] = [value.to_dict() for value in self.evidence]
        return data

    @classmethod
    def from_dict(cls, value: Any, path: str = "plugin") -> PluginRecord:
        data = _dict(value, path)
        fields = {
            "canonical_id",
            "name",
            "repo_url",
            "repo_owner",
            "repo_name",
            "display_name",
            "description",
            "author",
            "version",
            "astrbot_version",
            "platforms",
            "tags",
            "market_status",
            "issue_state",
            "issue_labels",
            "stars",
            "forks",
            "archived",
            "repo_updated_at",
            "github_metadata_fetched_at",
            "github_metadata_status",
            "observed_at",
            "first_seen_at",
            "last_seen_at",
            "content_hash",
            "field_sources",
            "evidence",
        }
        for key in ("github_metadata_fetched_at", "github_metadata_status"):
            if key not in data:
                data = {**data, key: None}
        _required(data, fields, path)
        field_sources = _dict(data["field_sources"], f"{path}.field_sources")
        evidence = _list(data["evidence"], f"{path}.evidence")
        return cls(
            canonical_id=_str(data["canonical_id"], f"{path}.canonical_id"),
            name=_str(data["name"], f"{path}.name"),
            repo_url=_optional_str(data["repo_url"], f"{path}.repo_url"),
            repo_owner=_optional_str(data["repo_owner"], f"{path}.repo_owner"),
            repo_name=_optional_str(data["repo_name"], f"{path}.repo_name"),
            display_name=_optional_str(data["display_name"], f"{path}.display_name"),
            description=_optional_str(data["description"], f"{path}.description"),
            author=_optional_str(data["author"], f"{path}.author"),
            version=_optional_str(data["version"], f"{path}.version"),
            astrbot_version=_optional_str(
                data["astrbot_version"], f"{path}.astrbot_version"
            ),
            platforms=_str_tuple(data["platforms"], f"{path}.platforms"),
            tags=_str_tuple(data["tags"], f"{path}.tags"),
            market_status=_optional_str(data["market_status"], f"{path}.market_status"),
            issue_state=_optional_str(data["issue_state"], f"{path}.issue_state"),
            issue_labels=_str_tuple(data["issue_labels"], f"{path}.issue_labels"),
            stars=_optional_int(data["stars"], f"{path}.stars"),
            forks=_optional_int(data["forks"], f"{path}.forks"),
            archived=_optional_bool(data["archived"], f"{path}.archived"),
            repo_updated_at=_optional_str(
                data["repo_updated_at"], f"{path}.repo_updated_at"
            ),
            github_metadata_fetched_at=_optional_str(
                data["github_metadata_fetched_at"],
                f"{path}.github_metadata_fetched_at",
            ),
            github_metadata_status=_optional_str(
                data["github_metadata_status"], f"{path}.github_metadata_status"
            ),
            observed_at=_optional_str(data["observed_at"], f"{path}.observed_at"),
            first_seen_at=_optional_str(data["first_seen_at"], f"{path}.first_seen_at"),
            last_seen_at=_optional_str(data["last_seen_at"], f"{path}.last_seen_at"),
            content_hash=_optional_str(data["content_hash"], f"{path}.content_hash"),
            field_sources={
                _str(key, f"{path}.field_sources key"): SourceEvidence.from_dict(
                    item, f"{path}.field_sources[{key!r}]"
                )
                for key, item in field_sources.items()
            },
            evidence=tuple(
                SourceEvidence.from_dict(item, f"{path}.evidence[{index}]")
                for index, item in enumerate(evidence)
            ),
        )


@dataclass(slots=True)
class ChangeEvent:
    event_id: str
    kind: ChangeKind
    canonical_id: str
    current: PluginRecord
    previous: PluginRecord | None
    changed_fields: tuple[str, ...]
    detected_at: str


@dataclass(slots=True)
class DeliveryTargetState:
    target: str
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    last_error_code: str | None = None
    last_attempt_at: str | None = None
    next_retry_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "last_error_code": self.last_error_code,
            "last_attempt_at": self.last_attempt_at,
            "next_retry_at": self.next_retry_at,
            "status": self.status.value,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, value: Any, path: str) -> DeliveryTargetState:
        data = _dict(value, path)
        if "last_attempt_at" not in data:
            data = {**data, "last_attempt_at": None}
        _required(
            data,
            {
                "target",
                "status",
                "attempts",
                "last_error_code",
                "last_attempt_at",
                "next_retry_at",
            },
            path,
        )
        attempts = _int(data["attempts"], f"{path}.attempts")
        if attempts < 0:
            raise ModelValidationError(f"{path}.attempts must not be negative")
        try:
            status = DeliveryStatus(_str(data["status"], f"{path}.status"))
        except ValueError as exc:
            raise ModelValidationError(f"{path}.status is unsupported") from exc
        return cls(
            target=_str(data["target"], f"{path}.target"),
            status=status,
            attempts=attempts,
            last_error_code=_optional_str(
                data["last_error_code"], f"{path}.last_error_code"
            ),
            last_attempt_at=_optional_str(
                data["last_attempt_at"], f"{path}.last_attempt_at"
            ),
            next_retry_at=_optional_str(data["next_retry_at"], f"{path}.next_retry_at"),
        )


@dataclass(slots=True)
class DeliveryBatch:
    batch_id: str
    event_ids: tuple[str, ...]
    message: str
    created_at: str
    targets: dict[str, DeliveryTargetState]
    max_attempts: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "event_ids": list(self.event_ids),
            "max_attempts": self.max_attempts,
            "message": self.message,
            "targets": {
                key: self.targets[key].to_dict() for key in sorted(self.targets)
            },
        }

    @classmethod
    def from_dict(cls, value: Any, path: str) -> DeliveryBatch:
        data = _dict(value, path)
        _required(
            data,
            {
                "batch_id",
                "event_ids",
                "message",
                "created_at",
                "targets",
                "max_attempts",
            },
            path,
        )
        targets_data = _dict(data["targets"], f"{path}.targets")
        max_attempts = _int(data["max_attempts"], f"{path}.max_attempts")
        if max_attempts < 1:
            raise ModelValidationError(f"{path}.max_attempts must be positive")
        targets = {
            _str(key, f"{path}.targets key"): DeliveryTargetState.from_dict(
                item, f"{path}.targets[{key!r}]"
            )
            for key, item in targets_data.items()
        }
        if any(key != target.target for key, target in targets.items()):
            raise ModelValidationError(f"{path}.targets keys must match target")
        for target in targets.values():
            if (
                target.status is DeliveryStatus.FAILED
                and target.attempts >= max_attempts
            ):
                target.status = DeliveryStatus.EXHAUSTED
                target.next_retry_at = None
        return cls(
            batch_id=_str(data["batch_id"], f"{path}.batch_id"),
            event_ids=_str_tuple(data["event_ids"], f"{path}.event_ids"),
            message=_str(data["message"], f"{path}.message"),
            created_at=_str(data["created_at"], f"{path}.created_at"),
            targets=targets,
            max_attempts=max_attempts,
        )


@dataclass(slots=True)
class RunReport:
    status: str
    started_at: str
    run_id: str | None = None
    finished_at: str | None = None
    sources_succeeded: int = 0
    sources_failed: int = 0
    observations: int = 0
    discovered: int = 0
    updated: int = 0
    batches_created: int = 0
    targets_sent: int = 0
    targets_pending: int = 0
    targets_exhausted: int = 0
    invalid_targets: int = 0
    target_error_code: str | None = None
    github_requests_used: int = 0
    github_requests_remaining: int | None = None
    github_error_code: str | None = None
    ai_status: str = "disabled"
    ai_error_code: str | None = None
    phase_durations_ms: dict[str, int] = field(
        default_factory=lambda: {
            phase: 0
            for phase in (
                "fetch",
                "merge",
                "detect",
                "github",
                "ai",
                "save",
                "deliver",
                "overall",
            )
        }
    )
    error_code: str | None = None
    busy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_chinese(self) -> str:
        if self.busy:
            return "市场观察器正在检查，请稍后重试。"
        status_name = {
            "success": "成功",
            "partial": "部分成功",
            "failed": "失败",
            "state_error": "状态不可用",
            "state_save_failed": "状态保存失败",
            "delivery_state_save_failed": "投递状态保存失败",
            "running": "运行中",
        }.get(self.status, self.status)
        github_remaining = (
            str(self.github_requests_remaining)
            if self.github_requests_remaining is not None
            else "未知"
        )
        lines = [
            "市场观察器检查结果",
            f"- 状态：{status_name}",
            f"- 来源成功/失败：{self.sources_succeeded}/{self.sources_failed}",
            f"- 观察记录：{self.observations}",
            f"- 新增/更新：{self.discovered}/{self.updated}",
            f"- 新建批次：{self.batches_created}",
            "- 已发送/待处理/永久失败目标："
            f"{self.targets_sent}/{self.targets_pending}/{self.targets_exhausted}",
            f"- 跳过非法目标：{self.invalid_targets}",
            f"- GitHub 请求已用/剩余：{self.github_requests_used}/{github_remaining}",
        ]
        if self.target_error_code:
            lines.append(f"- 目标错误类别：{self.target_error_code}")
        if self.github_error_code:
            lines.append(f"- GitHub 错误类别：{self.github_error_code}")
        lines.append(f"- AI 导语：{self.ai_status}")
        if self.ai_error_code:
            lines.append(f"- AI 错误类别：{self.ai_error_code}")
        durations = ", ".join(
            f"{key}={value}ms" for key, value in self.phase_durations_ms.items()
        )
        lines.append(f"- 阶段耗时：{durations}")
        if self.error_code:
            lines.append(f"- 错误类别：{self.error_code}")
        return "\n".join(lines)


@dataclass(slots=True)
class SourceState:
    baseline_established: bool = False
    last_success_at: str | None = None
    complete: bool = False
    error_code: str | None = None
    observations: dict[str, SourceObservation] = field(default_factory=dict)
    snapshots: dict[str, EndpointSnapshot] = field(default_factory=dict)

    def snapshot_for(self, endpoint: str) -> EndpointSnapshot | None:
        snapshot = self.snapshots.get(endpoint)
        return snapshot if snapshot and snapshot.endpoint == endpoint else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_established": self.baseline_established,
            "complete": self.complete,
            "error_code": self.error_code,
            "last_success_at": self.last_success_at,
            "observations": {
                key: self.observations[key].to_dict()
                for key in sorted(self.observations)
            },
            "snapshots": {
                key: self.snapshots[key].to_dict() for key in sorted(self.snapshots)
            },
        }

    @classmethod
    def from_dict(cls, value: Any, path: str = "source") -> SourceState:
        data = _dict(value, path)
        _required(
            data,
            {
                "baseline_established",
                "last_success_at",
                "complete",
                "error_code",
                "observations",
                "snapshots",
            },
            path,
        )
        observations = _dict(data["observations"], f"{path}.observations")
        snapshots = _dict(data["snapshots"], f"{path}.snapshots")
        parsed_observations = {
            _str(key, f"{path}.observations key"): SourceObservation.from_dict(
                item, f"{path}.observations[{key!r}]"
            )
            for key, item in observations.items()
        }
        parsed_snapshots = {
            _str(key, f"{path}.snapshots key"): EndpointSnapshot.from_dict(
                item, f"{path}.snapshots[{key!r}]"
            )
            for key, item in snapshots.items()
        }
        if any(
            key != observation.source_record_id
            for key, observation in parsed_observations.items()
        ) or any(
            key != snapshot.endpoint for key, snapshot in parsed_snapshots.items()
        ):
            raise ModelValidationError(f"{path} mapping keys must match nested IDs")
        return cls(
            baseline_established=_bool(
                data["baseline_established"], f"{path}.baseline_established"
            ),
            last_success_at=_optional_str(
                data["last_success_at"], f"{path}.last_success_at"
            ),
            complete=_bool(data["complete"], f"{path}.complete"),
            error_code=_optional_str(data["error_code"], f"{path}.error_code"),
            observations=parsed_observations,
            snapshots=parsed_snapshots,
        )


@dataclass(slots=True)
class GitHubRateLimitState:
    remaining: int | None = None
    reset_at: str | None = None
    status: str = "unknown"
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any, path: str = "github.rate_limit"):
        data = _dict(value, path)
        _required(data, {"remaining", "reset_at", "status", "error_code"}, path)
        remaining = _optional_int(data["remaining"], f"{path}.remaining")
        status = _str(data["status"], f"{path}.status")
        if remaining is not None and remaining < 0:
            raise ModelValidationError(f"{path}.remaining must not be negative")
        if status not in {
            "unknown",
            "ok",
            "auth_failed",
            "rate_limited",
            "permission_denied",
        }:
            raise ModelValidationError(f"{path}.status is unsupported")
        return cls(
            remaining=remaining,
            reset_at=_optional_str(data["reset_at"], f"{path}.reset_at"),
            status=status,
            error_code=_optional_str(data["error_code"], f"{path}.error_code"),
        )


@dataclass(slots=True)
class GitHubRepoCache:
    canonical_id: str
    etag: str | None = None
    stars: int | None = None
    forks: int | None = None
    archived: bool | None = None
    repo_updated_at: str | None = None
    fetched_at: str | None = None
    status: str = "unknown"
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any, path: str):
        data = _dict(value, path)
        fields = {
            "canonical_id",
            "etag",
            "stars",
            "forks",
            "archived",
            "repo_updated_at",
            "fetched_at",
            "status",
            "error_code",
        }
        _required(data, fields, path)
        canonical_id = _str(data["canonical_id"], f"{path}.canonical_id")
        stars = _optional_int(data["stars"], f"{path}.stars")
        forks = _optional_int(data["forks"], f"{path}.forks")
        status = _str(data["status"], f"{path}.status")
        repo_parts = canonical_id.removeprefix("github:").split("/")
        if (
            not canonical_id.startswith("github:")
            or len(repo_parts) != 2
            or not all(repo_parts)
        ):
            raise ModelValidationError(f"{path}.canonical_id must be a GitHub ID")
        if stars is not None and stars < 0:
            raise ModelValidationError(f"{path}.stars must not be negative")
        if forks is not None and forks < 0:
            raise ModelValidationError(f"{path}.forks must not be negative")
        if status not in {"unknown", "fresh", "stale", "failed", "inaccessible"}:
            raise ModelValidationError(f"{path}.status is unsupported")
        return cls(
            canonical_id=canonical_id,
            etag=_optional_str(data["etag"], f"{path}.etag"),
            stars=stars,
            forks=forks,
            archived=_optional_bool(data["archived"], f"{path}.archived"),
            repo_updated_at=_optional_str(
                data["repo_updated_at"], f"{path}.repo_updated_at"
            ),
            fetched_at=_optional_str(data["fetched_at"], f"{path}.fetched_at"),
            status=status,
            error_code=_optional_str(data["error_code"], f"{path}.error_code"),
        )


@dataclass(slots=True)
class GitHubState:
    rate_limit: GitHubRateLimitState = field(default_factory=GitHubRateLimitState)
    repos: dict[str, GitHubRepoCache] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_limit": self.rate_limit.to_dict(),
            "repos": {key: self.repos[key].to_dict() for key in sorted(self.repos)},
        }

    @classmethod
    def from_dict(cls, value: Any, path: str = "state.github"):
        data = _dict(value, path)
        _required(data, {"rate_limit", "repos"}, path)
        repos_data = _dict(data["repos"], f"{path}.repos")
        repos = {
            _str(key, f"{path}.repos key"): GitHubRepoCache.from_dict(
                item, f"{path}.repos[{key!r}]"
            )
            for key, item in repos_data.items()
        }
        if any(key != item.canonical_id for key, item in repos.items()):
            raise ModelValidationError(f"{path}.repos keys must match canonical_id")
        return cls(
            rate_limit=GitHubRateLimitState.from_dict(
                data["rate_limit"], f"{path}.rate_limit"
            ),
            repos=repos,
        )


@dataclass(slots=True)
class WatcherState:
    schema_version: int = 1
    updated_at: str | None = None
    subscriptions: list[str] = field(default_factory=list)
    sources: dict[str, SourceState] = field(default_factory=dict)
    plugins: dict[str, PluginRecord] = field(default_factory=dict)
    id_aliases: dict[str, str] = field(default_factory=dict)
    github: GitHubState = field(default_factory=GitHubState)
    outbox: dict[str, DeliveryBatch] = field(default_factory=dict)
    last_run: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.subscriptions = _subscriptions(self.subscriptions, "state.subscriptions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "github": self.github.to_dict(),
            "id_aliases": {
                key: self.id_aliases[key] for key in sorted(self.id_aliases)
            },
            "last_run": self.last_run,
            "outbox": {key: self.outbox[key].to_dict() for key in sorted(self.outbox)},
            "plugins": {
                key: self.plugins[key].to_dict() for key in sorted(self.plugins)
            },
            "schema_version": self.schema_version,
            "subscriptions": _subscriptions(self.subscriptions, "state.subscriptions"),
            "sources": {
                key: self.sources[key].to_dict() for key in sorted(self.sources)
            },
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> WatcherState:
        data = _dict(value, "state")
        fields = {
            "schema_version",
            "updated_at",
            "subscriptions",
            "sources",
            "plugins",
            "id_aliases",
            "github",
            "outbox",
            "last_run",
        }
        if "github" not in data and "github_cache" in data:
            legacy = _dict(data["github_cache"], "state.github_cache")
            if legacy:
                raise ModelValidationError(
                    "non-empty legacy github_cache is unsupported"
                )
            data = {
                **{key: item for key, item in data.items() if key != "github_cache"},
                "github": GitHubState().to_dict(),
            }
        _required(data, fields, "state")
        version = _int(data["schema_version"], "state.schema_version")
        sources = _dict(data["sources"], "state.sources")
        plugins = _dict(data["plugins"], "state.plugins")
        aliases = _dict(data["id_aliases"], "state.id_aliases")
        outbox = _dict(data["outbox"], "state.outbox")
        parsed_sources = {
            _str(key, "state.sources key"): SourceState.from_dict(
                item, f"state.sources[{key!r}]"
            )
            for key, item in sources.items()
        }
        parsed_plugins = {
            _str(key, "state.plugins key"): PluginRecord.from_dict(
                item, f"state.plugins[{key!r}]"
            )
            for key, item in plugins.items()
        }
        parsed_outbox = {
            _str(key, "state.outbox key"): DeliveryBatch.from_dict(
                item, f"state.outbox[{key!r}]"
            )
            for key, item in outbox.items()
        }
        for key, source in parsed_sources.items():
            try:
                kind = SourceKind(key)
            except ValueError as exc:
                raise ModelValidationError(
                    f"state.sources has unsupported key {key!r}"
                ) from exc
            if any(
                item.source_kind is not kind for item in source.observations.values()
            ):
                raise ModelValidationError(
                    f"state.sources[{key!r}] contains another source kind"
                )
            if any(
                item.source_kind is not kind
                for snapshot in source.snapshots.values()
                for item in snapshot.observations.values()
            ):
                raise ModelValidationError(
                    f"state.sources[{key!r}].snapshots contains another source kind"
                )
        if any(key != plugin.canonical_id for key, plugin in parsed_plugins.items()):
            raise ModelValidationError("state.plugins keys must match canonical_id")
        if any(key != batch.batch_id for key, batch in parsed_outbox.items()):
            raise ModelValidationError("state.outbox keys must match batch_id")
        return cls(
            schema_version=version,
            updated_at=_optional_str(data["updated_at"], "state.updated_at"),
            subscriptions=_subscriptions(data["subscriptions"], "state.subscriptions"),
            sources=parsed_sources,
            plugins=parsed_plugins,
            id_aliases={
                _str(key, "state.id_aliases key"): _str(
                    item, f"state.id_aliases[{key!r}]"
                )
                for key, item in aliases.items()
            },
            github=GitHubState.from_dict(data["github"], "state.github"),
            outbox=parsed_outbox,
            last_run=_dict(data["last_run"], "state.last_run"),
        )


def _required(data: dict[str, Any], fields: set[str], path: str) -> None:
    missing = fields - data.keys()
    extra = data.keys() - fields
    if missing or extra:
        raise ModelValidationError(
            f"{path}: missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _dict(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ModelValidationError(f"{path} must be an object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if type(value) is not list:
        raise ModelValidationError(f"{path} must be an array")
    return value


def _str(value: Any, path: str) -> str:
    if type(value) is not str or not value:
        raise ModelValidationError(f"{path} must be a non-empty string")
    return value


def _optional_str(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _str(value, path)


def _bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ModelValidationError(f"{path} must be a boolean")
    return value


def _optional_bool(value: Any, path: str) -> bool | None:
    return None if value is None else _bool(value, path)


def _int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ModelValidationError(f"{path} must be an integer")
    return value


def _optional_int(value: Any, path: str) -> int | None:
    return None if value is None else _int(value, path)


def _str_tuple(value: Any, path: str) -> tuple[str, ...]:
    return tuple(_str(item, f"{path}[]") for item in _list(value, path))


def _source_kind(value: Any, path: str) -> SourceKind:
    try:
        return SourceKind(_str(value, path))
    except ValueError as exc:
        raise ModelValidationError(f"{path} is unsupported") from exc


def _subscriptions(value: Any, path: str) -> list[str]:
    items = _list(value, path)
    normalized: set[str] = set()
    for index, item in enumerate(items):
        target = _str(item, f"{path}[{index}]").strip()
        if not target or len(target) > 512:
            raise ModelValidationError(
                f"{path}[{index}] must contain 1 to 512 non-whitespace characters"
            )
        normalized.add(target)
    return sorted(normalized)


def _validate_excerpt(value: dict[str, Any], path: str) -> None:
    try:
        size = len(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ModelValidationError(f"{path} must be JSON serializable") from exc
    if size > RAW_EXCERPT_MAX_BYTES:
        raise ModelValidationError(f"{path} exceeds {RAW_EXCERPT_MAX_BYTES} bytes")
