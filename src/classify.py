"""Role family, seniority and remote-status classification. Spec Section 6.4.

All rules live in config/taxonomy.yaml. This module only compiles and applies them, so
that improving a heuristic is a config change plus a rebuild, and the whole history can
be reclassified without a discontinuity in the series (spec 3.5).

Every classification records *how* it was reached where that is not obvious — see
`remote_source` — because a remote-share number that looks wrong in December is
undebuggable otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.models import RemoteFinding, RemoteSource, RoleFamily, Seniority
from src.normalize import clean_text, normalize_title

DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "config" / "taxonomy.yaml"


@dataclass(frozen=True)
class _FamilyRule:
    family: RoleFamily
    include: tuple[re.Pattern[str], ...]
    exclude: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class _SeniorityRule:
    level: Seniority
    patterns: tuple[re.Pattern[str], ...]


def _compile(patterns: list[str] | None) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in (patterns or []))


class Classifier:
    """Compiled taxonomy. Build once per run; `classify_*` is then pure and cheap."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.version: str = str(config["version"])

        self._families = tuple(
            _FamilyRule(
                family=RoleFamily(entry["name"]),
                include=_compile(entry.get("include")),
                exclude=_compile(entry.get("exclude")),
            )
            for entry in config["role_families"]
        )

        seniority_cfg = config["seniority"]
        self._seniority_default = Seniority(seniority_cfg.get("default", "unknown"))
        self._seniority = tuple(
            _SeniorityRule(level=Seniority(entry["name"]), patterns=_compile(entry["patterns"]))
            for entry in seniority_cfg["rules"]
        )

        remote_cfg = config["remote_inference"]
        self._remote_negative = _compile(remote_cfg.get("negative_patterns"))
        self._remote_positive = _compile(remote_cfg.get("positive_patterns"))
        self._workplace_map: dict[str, bool] = {
            str(k).strip().lower(): bool(v)
            for k, v in (remote_cfg.get("workplace_type_map") or {}).items()
        }
        self._metadata_names: frozenset[str] = frozenset(
            str(n).strip().lower() for n in (remote_cfg.get("metadata_field_names") or [])
        )

    # ------------------------------------------------------------------ role family

    def role_family(self, title: str) -> RoleFamily:
        """First matching family wins; an `exclude` hit vetoes and falls through.

        Order is load-bearing and defined by the config: "Data Science Manager" contains
        "data scien", so ds_manager is tested before product_ds.
        """
        normalized = normalize_title(title)
        for rule in self._families:
            if any(p.search(normalized) for p in rule.include) and not any(
                p.search(normalized) for p in rule.exclude
            ):
                return rule.family
        return RoleFamily.OTHER

    # -------------------------------------------------------------------- seniority

    def seniority(self, title: str) -> Seniority:
        """Most-senior-first, because people leadership dominates: "Senior Manager" is a
        manager. A title with no marker at all falls back to the configured default."""
        normalized = normalize_title(title)
        for rule in self._seniority:
            if any(p.search(normalized) for p in rule.patterns):
                return rule.level
        return self._seniority_default

    # ----------------------------------------------------------------------- remote

    def is_workplace_type_field(self, field_name: str | None) -> bool:
        return bool(field_name) and str(field_name).strip().lower() in self._metadata_names

    def remote_from_workplace_type(self, value: object) -> RemoteFinding | None:
        """Map a board's custom "Workplace Type" value. Returns None if unrecognized,
        so an unexpected value degrades to location inference rather than guessing."""
        if isinstance(value, bool):
            return RemoteFinding(is_remote=value, remote_source=RemoteSource.METADATA_FIELD)
        text = clean_text(str(value)) if value is not None else None
        if not text:
            return None
        mapped = self._workplace_map.get(text.lower())
        if mapped is None:
            return None
        return RemoteFinding(is_remote=mapped, remote_source=RemoteSource.METADATA_FIELD)

    def remote_from_location(self, *parts: str | None) -> RemoteFinding:
        """Infer remote status from location text. **Tri-state on purpose.**

        A negative match vetoes a positive one, because "Remote (Hybrid - 3 days in
        office)" is hybrid. An ambiguous location yields None, never False: a plain
        "United States" says nothing about remote status, and recording that honestly
        is what keeps the remote-share denominator meaningful and the error rate
        measurable. This is the dominant path for Greenhouse, where the "Workplace Type"
        metadata field exists on roughly one board in six.
        """
        haystack = " ".join(filter(None, (clean_text(p) for p in parts))).lower()
        if not haystack:
            return RemoteFinding(is_remote=None, remote_source=RemoteSource.UNKNOWN)
        if any(p.search(haystack) for p in self._remote_negative):
            return RemoteFinding(is_remote=False, remote_source=RemoteSource.LOCATION_STRING)
        if any(p.search(haystack) for p in self._remote_positive):
            return RemoteFinding(is_remote=True, remote_source=RemoteSource.LOCATION_STRING)
        return RemoteFinding(is_remote=None, remote_source=RemoteSource.UNKNOWN)


def load_classifier(path: Path | None = None) -> Classifier:
    return _load_cached(str(path or DEFAULT_TAXONOMY_PATH))


@lru_cache(maxsize=4)
def _load_cached(path: str) -> Classifier:
    with open(path, encoding="utf-8") as handle:
        return Classifier(yaml.safe_load(handle))
