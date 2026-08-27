"""Pure retention-policy primitives for the accepted METADATA_EXPAND phase.

This module deliberately performs no MinIO call and does not declare storage
enforcement successful.  It resolves an approved data-category rule and
calculates the deadline that a later exact-version enforcement package must
request and independently read back from storage.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, Mapping


PERMANENT: Final = "permanent"
LONG_TERM_10Y: Final = "long_term_10y"
SHORT_90D: Final = "short_90d"
CANONICAL_RETENTION_CLASSES: Final = (
    PERMANENT,
    LONG_TERM_10Y,
    SHORT_90D,
)

RETENTION_POLICY_VERSION: Final = "smartcoat_retention_2026_08_v1"
RETENTION_POLICY_DOCUMENT: Final = (
    "docs/architecture/decisions/"
    "ADR-0002-retention-semantics-and-enforcement-contract.md"
)
RETENTION_POLICY_DOCUMENT_SHA256: Final = (
    "307ce9d9484b3819d16c5178a3dc61fb56e257376779e679e4923b1e7f5beb37"
)


class RetentionPolicyError(ValueError):
    """Base class for fail-closed policy resolution errors."""


class ClassificationPending(RetentionPolicyError):
    """The category has no approved rule and cannot be silently defaulted."""


class PolicyVersionUnavailable(RetentionPolicyError):
    """The requested immutable policy version is not the accepted version."""


@dataclass(frozen=True)
class CategoryRule:
    data_category: str
    retention_class: str
    records_purpose: str
    legal_basis_classification: str

    @property
    def legal_hold_required(self) -> bool:
        return self.retention_class == PERMANENT


@dataclass(frozen=True)
class RetentionAssignmentPlan:
    data_category: str
    retention_class: str
    retention_policy_version: str
    accepted_storage_at_utc: datetime
    expected_retain_until_utc: datetime
    legal_hold_required: bool
    records_purpose: str
    legal_basis_classification: str


def _rule(
    data_category: str,
    retention_class: str,
    records_purpose: str,
    legal_basis_classification: str,
) -> CategoryRule:
    if retention_class not in CANONICAL_RETENTION_CLASSES:
        raise RuntimeError("policy registry contains a non-canonical retention class")
    return CategoryRule(
        data_category,
        retention_class,
        records_purpose,
        legal_basis_classification,
    )


_RULES: Final[Mapping[str, CategoryRule]] = MappingProxyType(
    {
        rule.data_category: rule
        for rule in (
            _rule(
                "LAB_NOTE",
                PERMANENT,
                "R&D evidentiary record",
                "approved_non_personal_evidence",
            ),
            _rule(
                "TEST_RESULT",
                PERMANENT,
                "R&D evidentiary record",
                "approved_non_personal_evidence",
            ),
            _rule(
                "FORMULATION_SCREEN",
                PERMANENT,
                "R&D evidentiary record",
                "approved_non_personal_evidence",
            ),
            _rule(
                "MATERIAL_DOCUMENT",
                PERMANENT,
                "R&D evidentiary record",
                "approved_non_personal_evidence",
            ),
            _rule(
                "TRIAL_VIDEO",
                PERMANENT,
                "R&D trial-video evidence",
                "approved_non_personal_evidence",
            ),
            _rule(
                "PLATFORM_OPERATIONAL_LOG",
                SHORT_90D,
                "Platform operational health",
                "approved_operational_record",
            ),
            _rule(
                "PLATFORM_DEBUG_LOG",
                SHORT_90D,
                "Platform troubleshooting",
                "approved_operational_record",
            ),
        )
    }
)


def approved_rules() -> Mapping[str, CategoryRule]:
    """Return the immutable rules for the accepted policy version."""

    return _RULES


def normalize_storage_timestamp(value: datetime) -> datetime:
    """Normalize an exact-version storage instant to whole-second UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise RetentionPolicyError("storage timestamp must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0)


def add_calendar_years(value: datetime, years: int) -> datetime:
    """Add UTC calendar years, clamping leap day to the target month's end."""

    normalized = normalize_storage_timestamp(value)
    if years <= 0:
        raise RetentionPolicyError("calendar duration must be positive")
    target_year = normalized.year + years
    target_day = min(
        normalized.day,
        calendar.monthrange(target_year, normalized.month)[1],
    )
    return normalized.replace(year=target_year, day=target_day)


def retain_until_for(retention_class: str, accepted_storage_at_utc: datetime) -> datetime:
    """Calculate the declared deadline; this is not enforcement evidence."""

    accepted = normalize_storage_timestamp(accepted_storage_at_utc)
    if retention_class in {PERMANENT, LONG_TERM_10Y}:
        return add_calendar_years(accepted, 10)
    if retention_class == SHORT_90D:
        return accepted + timedelta(hours=2160)
    raise RetentionPolicyError("retention class is not canonical")


def resolve_category_rule(
    data_category: str,
    *,
    retention_policy_version: str = RETENTION_POLICY_VERSION,
) -> CategoryRule:
    """Resolve only an approved category under the exact immutable policy."""

    if retention_policy_version != RETENTION_POLICY_VERSION:
        raise PolicyVersionUnavailable("retention policy version is not approved")
    normalized = data_category.strip().upper()
    rule = _RULES.get(normalized)
    if rule is None:
        raise ClassificationPending(
            "data category has no approved retention assignment"
        )
    return rule


def plan_assignment(
    data_category: str,
    accepted_storage_at_utc: datetime,
    *,
    retention_policy_version: str = RETENTION_POLICY_VERSION,
) -> RetentionAssignmentPlan:
    """Create a deterministic plan for later exact-version enforcement."""

    rule = resolve_category_rule(
        data_category,
        retention_policy_version=retention_policy_version,
    )
    accepted = normalize_storage_timestamp(accepted_storage_at_utc)
    return RetentionAssignmentPlan(
        data_category=rule.data_category,
        retention_class=rule.retention_class,
        retention_policy_version=retention_policy_version,
        accepted_storage_at_utc=accepted,
        expected_retain_until_utc=retain_until_for(
            rule.retention_class,
            accepted,
        ),
        legal_hold_required=rule.legal_hold_required,
        records_purpose=rule.records_purpose,
        legal_basis_classification=rule.legal_basis_classification,
    )
