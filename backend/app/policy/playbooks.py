"""Typed loader for policy/playbooks.yaml.

Fail-loud by design: an invalid playbook raises PlaybookError with a precise
message. There are no silent defaults — a system that gates money decisions
must refuse to run on a config it cannot fully validate.

Zero LLM imports (enforced by test_gate.TestPolicyPurity).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..store.models import ReasonCode

DEFAULT_PLAYBOOK_PATH = Path(__file__).parent / "playbooks.yaml"

_TOP_LEVEL_KEYS = {"version", "defaults", "reason_codes"}
_RULE_KEYS = {"key", "description", "required_fields", "checks"}


class PlaybookError(ValueError):
    """Raised for any structurally or semantically invalid playbook."""


@dataclass(frozen=True)
class EvidenceRule:
    key: str
    description: str
    required_fields: tuple[str, ...]
    checks: tuple[str, ...]


@dataclass(frozen=True)
class PwinBand:
    min_completeness: float
    p_win: float


@dataclass(frozen=True)
class ReasonPlaybook:
    reason_code: str
    required: tuple[EvidenceRule, ...]
    optional: tuple[EvidenceRule, ...]
    p_win_bands: tuple[PwinBand, ...]

    @property
    def rules(self) -> dict[str, EvidenceRule]:
        return {r.key: r for r in (*self.required, *self.optional)}

    @property
    def required_keys(self) -> tuple[str, ...]:
        return tuple(r.key for r in self.required)


@dataclass(frozen=True)
class PlaybookSet:
    version: str
    amount_tolerance_inr: int
    reason_codes: dict[str, ReasonPlaybook]

    def for_reason(self, reason_code) -> ReasonPlaybook:
        code = str(getattr(reason_code, "value", reason_code))
        rp = self.reason_codes.get(code)
        if rp is None:
            raise PlaybookError(
                f"no playbook for reason code '{code}' (playbook {self.version} "
                f"covers: {sorted(self.reason_codes)})")
        return rp


def _rule(raw: dict, where: str) -> EvidenceRule:
    if not isinstance(raw, dict):
        raise PlaybookError(f"{where}: rule must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise PlaybookError(f"{where}: unknown rule keys {sorted(unknown)}")
    for req in ("key", "description", "required_fields", "checks"):
        if req not in raw:
            raise PlaybookError(f"{where}: rule missing '{req}'")
    if not raw["checks"]:
        raise PlaybookError(f"{where}: rule '{raw['key']}' has no checks — "
                            "unverifiable evidence cannot be admissible")
    return EvidenceRule(
        key=str(raw["key"]),
        description=str(raw["description"]),
        required_fields=tuple(map(str, raw["required_fields"])),
        checks=tuple(map(str, raw["checks"])),
    )


def _bands(raw: list, where: str) -> tuple[PwinBand, ...]:
    if not raw:
        raise PlaybookError(f"{where}: p_win_bands missing or empty")
    bands = []
    for b in raw:
        try:
            bands.append(PwinBand(float(b["min_completeness"]), float(b["p_win"])))
        except (KeyError, TypeError, ValueError) as e:
            raise PlaybookError(f"{where}: bad band {b!r}: {e}") from e
    for band in bands:
        if not (0.0 <= band.min_completeness <= 1.0 and 0.0 <= band.p_win <= 1.0):
            raise PlaybookError(f"{where}: band values out of [0,1]: {band}")
    mins = [b.min_completeness for b in bands]
    if mins != sorted(mins, reverse=True) or len(set(mins)) != len(mins):
        raise PlaybookError(f"{where}: bands must be strictly descending by "
                            f"min_completeness, got {mins}")
    if bands[-1].min_completeness != 0.0:
        raise PlaybookError(f"{where}: last band must cover min_completeness 0.0")
    return tuple(bands)


def load_playbooks(path: str | Path = DEFAULT_PLAYBOOK_PATH) -> PlaybookSet:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise PlaybookError("playbook root must be a mapping")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise PlaybookError(f"unknown top-level keys {sorted(unknown)} — "
                            "refusing to guess; fix the playbook")
    version = raw.get("version")
    if not version or not isinstance(version, str):
        raise PlaybookError("playbook 'version' must be a non-empty string")
    defaults = raw.get("defaults") or {}
    tol = defaults.get("amount_tolerance_inr")
    if not isinstance(tol, int) or tol < 0:
        raise PlaybookError("defaults.amount_tolerance_inr must be an int >= 0")

    valid_codes = {rc.value for rc in ReasonCode}
    reason_codes: dict[str, ReasonPlaybook] = {}
    for code, body in (raw.get("reason_codes") or {}).items():
        where = f"reason_codes.{code}"
        if code not in valid_codes:
            raise PlaybookError(f"{where}: '{code}' is not a known ReasonCode")
        required = tuple(_rule(r, where) for r in (body.get("required") or []))
        optional = tuple(_rule(r, where) for r in (body.get("optional") or []))
        if not required:
            raise PlaybookError(f"{where}: at least one required evidence rule needed")
        keys = [r.key for r in (*required, *optional)]
        if len(keys) != len(set(keys)):
            raise PlaybookError(f"{where}: duplicate evidence keys {keys}")
        reason_codes[code] = ReasonPlaybook(
            reason_code=code, required=required, optional=optional,
            p_win_bands=_bands(body.get("p_win_bands"), where),
        )
    if not reason_codes:
        raise PlaybookError("playbook defines no reason codes")
    return PlaybookSet(version=version, amount_tolerance_inr=tol,
                       reason_codes=reason_codes)
