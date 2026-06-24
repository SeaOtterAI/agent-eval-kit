"""Contract types — the objects an agent acts on.

Plain dataclasses (no hard pydantic/runtime dependency) that parse the JSON the
eval API returns, so the SDK and the MCP server agree on one shape. See
``docs/protocol.md`` for the canonical schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Bands, in worsening order. ``ship`` is the only "done" state.
BANDS = ("ship", "route_to_fix", "quarantine", "block")
PASSING_BANDS = ("ship",)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class Anchor:
    """WHERE a flaw is — spatial localization the agent uses to target its fix."""

    kind: str = ""
    bbox: list[float] | None = None          # [x0,y0,x1,y1] normalized [0,1] (image/page)
    point: list[float] | None = None         # [x,y] normalized [0,1]
    timestamp: float | None = None           # seconds (video/audio)
    span: list[int] | None = None            # [start,end] char offsets (text/code)
    cell: str | None = None                  # e.g. "B7" (spreadsheet)
    slide: int | None = None                 # 1-based (deck)
    page: int | None = None                  # 1-based (pdf/document)

    @classmethod
    def from_dict(cls, d: Any) -> Anchor | None:
        if not isinstance(d, dict):
            return None
        return cls(
            kind=str(d.get("kind") or ""),
            bbox=d.get("bbox"), point=d.get("point"),
            timestamp=d.get("timestamp"), span=d.get("span"),
            cell=d.get("cell"), slide=d.get("slide"), page=d.get("page"),
        )

    def human(self) -> str:
        if self.timestamp is not None:
            return f"@{self.timestamp:.1f}s"
        if self.cell:
            return f"cell {self.cell}"
        if self.slide is not None:
            return f"slide {self.slide}"
        if self.page is not None:
            return f"page {self.page}"
        if self.bbox:
            return f"region {self.bbox}"
        if self.span:
            return f"chars {self.span[0]}–{self.span[1]}"
        return self.kind or ""


@dataclass
class Flaw:
    """One reason to block, with severity, evidence, and a spatial anchor."""

    criterion: str = ""
    severity: str = "med"
    evidence: str = ""
    detail: str = ""
    modality: str = ""
    anchor: Anchor | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Flaw:
        # Different critic backends use slightly different keys; accept the common
        # aliases so the agent never has to special-case the source.
        return cls(
            criterion=str(d.get("criterion") or d.get("title") or d.get("name") or d.get("flaw") or ""),
            severity=str(d.get("severity") or "med"),
            evidence=str(d.get("evidence") or d.get("evidence_span") or ""),
            detail=str(d.get("detail") or d.get("description") or d.get("evidence") or ""),
            modality=str(d.get("modality") or ""),
            anchor=Anchor.from_dict(d.get("anchor") or d.get("location")),
        )

    def key(self) -> str:
        """Stable identity for delta diffing across iterations."""
        return f"{self.criterion.strip().lower()}|{(self.anchor.human() if self.anchor else '')}"

    def human(self) -> str:
        where = f" ({self.anchor.human()})" if self.anchor and self.anchor.human() else ""
        return f"[{self.severity}] {self.criterion}{where}: {self.detail}".strip()


@dataclass
class Upgrade:
    """A concrete, actionable fix the agent can apply."""

    action: str = ""
    target_criterion: str = ""
    draft: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> Upgrade:
        if isinstance(d, str):
            return cls(action=d)
        d = d or {}
        return cls(
            action=str(d.get("action") or d.get("suggestion") or ""),
            target_criterion=str(d.get("target_criterion") or d.get("criterion") or ""),
            draft=str(d.get("draft") or ""),
        )


@dataclass
class FeedbackArtifact:
    """A rendered rich-feedback artifact — annotated image / pdf / video markers
    / markdown — that a frontier agent can render for a human AND machine-read."""

    kind: str = ""
    modality: str = ""
    mime: str = ""
    ref: str | None = None
    data_url: str | None = None
    caption: str = ""
    anchors: list[Anchor] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FeedbackArtifact:
        return cls(
            kind=str(d.get("kind") or ""), modality=str(d.get("modality") or ""),
            mime=str(d.get("mime") or d.get("mime_type") or ""),
            ref=d.get("ref"), data_url=d.get("data_url"),
            caption=str(d.get("caption") or ""),
            anchors=[a for a in (Anchor.from_dict(x) for x in (d.get("anchors") or [])) if a],
        )


@dataclass
class Verdict:
    """The critic's authoritative answer. ``ready_to_ship`` is the stop signal."""

    score: float = 0.0
    band: str = "quarantine"
    decision: str = "quarantine"
    flaws: list[Flaw] = field(default_factory=list)
    upgrades: list[Upgrade] = field(default_factory=list)
    rationale: str = ""
    feedback_artifacts: list[FeedbackArtifact] = field(default_factory=list)
    model_id: str = ""
    source: str = ""
    cost_usd: float = 0.0
    locale: str = "en"
    run_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, run_id: str | None = None) -> Verdict:
        d = d or {}
        return cls(
            score=_f(d.get("score")),
            band=str(d.get("band") or "quarantine"),
            decision=str(d.get("decision") or d.get("band") or "quarantine"),
            flaws=[Flaw.from_dict(x) for x in (d.get("flaws") or []) if isinstance(x, dict)],
            upgrades=[Upgrade.from_dict(x) for x in (d.get("upgrades") or [])],
            rationale=str(d.get("rationale") or ""),
            feedback_artifacts=[FeedbackArtifact.from_dict(x)
                                for x in (d.get("feedback_artifacts") or []) if isinstance(x, dict)],
            model_id=str(d.get("model_id") or ""),
            source=str(d.get("source") or ""),
            cost_usd=_f(d.get("cost_usd")),
            locale=str(d.get("locale") or "en"),
            run_id=run_id or d.get("run_id"),
            raw=d,
        )

    @property
    def ready_to_ship(self) -> bool:
        return self.band in PASSING_BANDS

    @property
    def is_blocked(self) -> bool:
        return self.band in ("quarantine", "block")

    def summary(self) -> str:
        head = f"score {self.score:.0f} — {self.band.upper()}"
        if not self.flaws:
            return head + (f"\n{self.rationale}" if self.rationale else "")
        lines = [head, ""] + [f"- {fl.human()}" for fl in self.flaws]
        if self.upgrades:
            lines += ["", "Fixes:"] + [f"- {u.action}" for u in self.upgrades if u.action]
        return "\n".join(lines)


@dataclass
class Delta:
    """What changed between two iterations — the agent's progress signal."""

    resolved_flaws: list[Flaw] = field(default_factory=list)
    new_flaws: list[Flaw] = field(default_factory=list)
    persisted_flaws: list[Flaw] = field(default_factory=list)
    score_change: float = 0.0
    ready_to_ship: bool = False

    @classmethod
    def between(cls, prev: Verdict, cur: Verdict) -> Delta:
        prev_keys = {f.key(): f for f in prev.flaws}
        cur_keys = {f.key(): f for f in cur.flaws}
        return cls(
            resolved_flaws=[f for k, f in prev_keys.items() if k not in cur_keys],
            new_flaws=[f for k, f in cur_keys.items() if k not in prev_keys],
            persisted_flaws=[f for k, f in cur_keys.items() if k in prev_keys],
            score_change=round(cur.score - prev.score, 2),
            ready_to_ship=cur.ready_to_ship,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Delta:
        d = d or {}
        return cls(
            resolved_flaws=[Flaw.from_dict(x) for x in (d.get("resolved_flaws") or [])],
            new_flaws=[Flaw.from_dict(x) for x in (d.get("new_flaws") or [])],
            persisted_flaws=[Flaw.from_dict(x) for x in (d.get("persisted_flaws") or [])],
            score_change=_f(d.get("score_change")),
            ready_to_ship=bool(d.get("ready_to_ship")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved_flaws": [f.__dict__ for f in self.resolved_flaws],
            "new_flaws": [f.__dict__ for f in self.new_flaws],
            "persisted_flaws": [f.__dict__ for f in self.persisted_flaws],
            "score_change": self.score_change,
            "ready_to_ship": self.ready_to_ship,
        }


@dataclass
class IterationResult:
    run_id: str
    verdict: Verdict
    delta: Delta
    idx: int = 0
