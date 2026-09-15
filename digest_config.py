"""Session policy shared by the CLI, selection, previews and telemetry."""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class DigestConfig:
    session: str
    cap: int
    quotas: Mapping[str, int]


SESSION_CONFIGS = MappingProxyType({
    "morning": DigestConfig("morning", 10, MappingProxyType({"ran": 2, "research": 2, "virt": 2})),
    "afternoon": DigestConfig("afternoon", 5, MappingProxyType({"ran": 1, "research": 1, "virt": 1})),
})
MIN_GLOBAL_SLOTS = MappingProxyType({"morning": 4, "afternoon": 2})


def validate_config(session: str, cap: int, quotas: Mapping[str, int]) -> None:
    if not isinstance(session, str) or session not in SESSION_CONFIGS:
        raise ValueError(f"Unknown digest session: {session!r}")
    if type(cap) is not int or cap <= 0:
        raise ValueError("Digest cap must be a positive integer")
    if not isinstance(quotas, Mapping):
        raise ValueError("Digest quotas must be a category mapping")
    for category, count in quotas.items():
        if not isinstance(category, str) or not category:
            raise ValueError("Quota categories must be non-empty strings")
        if type(count) is not int or count < 0:
            raise ValueError(f"Quota for {category!r} must be a non-negative integer")
    total = sum(quotas.values())
    if total > cap:
        raise ValueError("Total quota exceeds digest cap")
    if cap - total < MIN_GLOBAL_SLOTS[session]:
        raise ValueError(f"{session} must reserve at least {MIN_GLOBAL_SLOTS[session]} global slots")


def get_config(session: str = "morning", *, cap: int | None = None,
               quotas: Mapping[str, int] | None = None) -> DigestConfig:
    """Resolve defaults and validate; return an immutable snapshot for this run."""
    if not isinstance(session, str) or session not in SESSION_CONFIGS:
        raise ValueError(f"Unknown digest session: {session!r}")
    base = SESSION_CONFIGS[session]
    cap = base.cap if cap is None else cap
    quotas = base.quotas if quotas is None else quotas
    validate_config(session, cap, quotas)
    return DigestConfig(session, cap, MappingProxyType(dict(quotas)))
