"""Analytics routes: metrics aggregated from claims and HITL tables."""
from __future__ import annotations  # for Python 3.10-3.11 compatibility

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from api.db import Claim, get_session
from api.security import get_current_user
from src.hitl.queue import _get_db as _hitl_db

router = APIRouter(prefix="/api/analytics", tags=["Analytics"])


def _normalize_decision(claim: Claim) -> str:
    """
    Normalize claim status/final_decision values so analytics can count them correctly.

    The app can store decisions/statuses in multiple ways:
    - approved
    - approved_partial
    - denied
    - completed
    - pending_documents
    - fraud_investigation
    - under_review / hitl
    """

    raw_value = (
        getattr(claim, "final_decision", None)
        or getattr(claim, "status", None)
        or ""
    )

    value = str(raw_value).lower().strip()

    if not value:
        return "pending"

    if "approved_partial" in value or "partial" in value:
        return "approved_partial"

    if "approved" in value or "completed" in value:
        return "approved"

    if "denied" in value or "rejected" in value:
        return "denied"

    if "fraud" in value:
        return "fraud_investigation"

    if "document" in value or "docs" in value:
        return "pending_documents"

    if "review" in value or "hitl" in value:
        return "under_review"

    if "pending" in value:
        return "pending"

    return value


def _load_completed(session: Session) -> list[Claim]:
    """
    Backward-compatible helper used by /pipeline.
    Now treats all final/processed decisions as completed enough for analytics.
    """

    all_claims = session.exec(select(Claim)).all()

    final_or_processed = {
        "approved",
        "approved_partial",
        "denied",
        "fraud_investigation",
        "pending_documents",
    }

    return [c for c in all_claims if _normalize_decision(c) in final_or_processed]


@router.get("/metrics")
def metrics(session: Session = Depends(get_session), _=Depends(get_current_user)):
    """
    Returns overall metrics and breakdowns for claims, including approval rates,
    HITL rates, costs, decision distribution, and pipeline paths.
    """

    all_claims = session.exec(select(Claim)).all()
    total = len(all_claims)

    # Count all claims by normalized decision/status.
    # Do not only count status == "completed"; that was the analytics bug.
    by_decision = Counter(_normalize_decision(c) for c in all_claims)

    approved_count = by_decision.get("approved", 0)
    partial_count = by_decision.get("approved_partial", 0)
    denied_count = by_decision.get("denied", 0)

    final_count = approved_count + partial_count + denied_count

    approval_rate = (approved_count / final_count) if final_count else 0.0

    hitl_rate = (
        sum(1 for c in all_claims if getattr(c, "hitl_required", False)) / total
    ) if total else 0.0

    processed_decisions = {
        "approved",
        "approved_partial",
        "denied",
        "fraud_investigation",
        "pending_documents",
    }

    processed_claims = [
        c for c in all_claims
        if _normalize_decision(c) in processed_decisions
    ]

    processing_times = [
        c.processing_time_sec
        for c in processed_claims
        if getattr(c, "processing_time_sec", None) is not None
    ]

    avg_time = (
        sum(processing_times) / len(processing_times)
    ) if processing_times else 0.0

    total_cost = sum((getattr(c, "total_cost_usd", 0) or 0) for c in all_claims)

    eval_scores = [
        c.evaluation_score
        for c in all_claims
        if getattr(c, "evaluation_score", None) is not None
    ]

    avg_eval = (
        sum(eval_scores) / len(eval_scores)
    ) if eval_scores else 0.0

    by_type = Counter(c.incident_type or "unknown" for c in all_claims)

    path_stats = defaultdict(lambda: {"count": 0, "total_cost": 0.0, "total_agents": 0})

    for c in processed_claims:
        try:
            path_items = json.loads(c.pipeline_path or "[]")
            path_key = " -> ".join(path_items) if path_items else "direct"
        except Exception:
            path_key = "direct"

        s = path_stats[path_key]
        s["count"] += 1
        s["total_cost"] += getattr(c, "total_cost_usd", 0) or 0
        s["total_agents"] += getattr(c, "agent_call_count", 0) or 0

    return {
        "total_claims": total,
        "approval_rate": round(approval_rate, 4),
        "hitl_rate": round(hitl_rate, 4),
        "avg_processing_time_sec": round(avg_time, 2),
        "total_cost_usd": round(total_cost, 4),
        "avg_eval_score": round(avg_eval, 4),
        "claims_by_type": [
            {"type": k, "count": v}
            for k, v in by_type.most_common()
        ],
        "decisions": [
            {"decision": k, "count": v}
            for k, v in by_decision.most_common()
        ],
        "pipeline_paths": [
            {
                "path": p,
                "count": s["count"],
                "avg_cost": round(s["total_cost"] / s["count"], 4),
                "avg_agents": round(s["total_agents"] / s["count"], 2),
            }
            for p, s in sorted(path_stats.items(), key=lambda x: -x[1]["count"])
        ],
    }


@router.get("/pipeline")
def pipeline_stats(session: Session = Depends(get_session), _=Depends(get_current_user)):
    """
    Analyzes the pipeline paths taken by processed claims, including frequency
    of different paths and agent usage.
    """

    completed = _load_completed(session)
    agent_calls = Counter()

    for c in completed:
        try:
            path_items = json.loads(c.pipeline_path or "[]")
        except Exception:
            path_items = []

        for agent in path_items:
            agent_calls[agent] += 1

    return {
        "total_completed": len(completed),
        "avg_agents_per_claim": (
            sum((c.agent_call_count or 0) for c in completed) / len(completed)
        ) if completed else 0,
        "total_tokens": sum((c.total_tokens_used or 0) for c in completed),
        "agent_call_counts": [
            {"agent": k, "count": v}
            for k, v in agent_calls.most_common()
        ],
    }


@router.get("/costs")
def costs(
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
    _=Depends(get_current_user),
):
    """
    Returns cost-related metrics for claims created in the last `days` days,
    including total and average costs.
    """

    since = datetime.now(timezone.utc) - timedelta(days=days)
    all_claims = session.exec(select(Claim)).all()

    recent = [
        c for c in all_claims
        if c.created_at and c.created_at >= since.isoformat()
    ]

    daily = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "claims": 0})

    for c in recent:
        day = c.created_at[:10]
        daily[day]["cost"] += c.total_cost_usd or 0
        daily[day]["tokens"] += c.total_tokens_used or 0
        daily[day]["claims"] += 1

    total_recent_cost = sum((c.total_cost_usd or 0) for c in recent)

    return {
        "total_cost_usd": round(total_recent_cost, 4),
        "total_tokens": sum((c.total_tokens_used or 0) for c in recent),
        "avg_cost_per_claim": (
            round(total_recent_cost / len(recent), 4)
        ) if recent else 0,
        "daily": [
            {"date": d, **v}
            for d, v in sorted(daily.items())
        ],
    }


@router.get("/fraud-trends")
def fraud_trends(
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
    _=Depends(get_current_user),
):
    """
    Analyzes fraud scores and risk levels for claims created in the last `days`
    days, including average scores, risk level distributions, and daily trends.
    """

    since = datetime.now(timezone.utc) - timedelta(days=days)

    recent = session.exec(select(Claim)).all()
    recent = [
        c for c in recent
        if c.created_at
        and c.created_at >= since.isoformat()
        and c.fraud_score is not None
    ]

    levels = Counter(c.fraud_risk_level or "unknown" for c in recent)
    daily_avg = defaultdict(list)

    for c in recent:
        daily_avg[c.created_at[:10]].append(c.fraud_score)

    return {
        "avg_fraud_score": (
            round(sum(c.fraud_score for c in recent) / len(recent), 4)
        ) if recent else 0,
        "risk_level_counts": [
            {"level": k, "count": v}
            for k, v in levels.most_common()
        ],
        "daily_avg_score": [
            {"date": d, "avg_score": round(sum(v) / len(v), 4)}
            for d, v in sorted(daily_avg.items())
        ],
    }


@router.get("/hitl")
def hitl_metrics(_=Depends(get_current_user)):
    """
    Returns metrics related to the Human-in-the-Loop queue, including counts of
    pending and resolved items, override rates, and breakdowns by priority.
    """

    conn = _hitl_db()

    try:
        def one(q: str):
            return conn.execute(q).fetchone()[0]

        pending = one("SELECT COUNT(*) FROM hitl_queue WHERE status='pending'")
        resolved = one("SELECT COUNT(*) FROM hitl_queue WHERE status='resolved'")
        overrides = one("SELECT COUNT(*) FROM hitl_queue WHERE override_ai=1")

        by_priority = conn.execute(
            "SELECT priority, COUNT(*) FROM hitl_queue GROUP BY priority"
        ).fetchall()

    finally:
        conn.close()

    return {
        "pending": pending,
        "resolved": resolved,
        "total_overrides": overrides,
        "override_rate": round(overrides / resolved, 4) if resolved else 0.0,
        "by_priority": [
            {"priority": r[0], "count": r[1]}
            for r in by_priority
        ],
    }


@router.get("/evaluations")
def evaluations(
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
    _=Depends(get_current_user),
):
    """
    Returns metrics related to claim evaluations, including average scores,
    pass rates, and recent evaluation details.
    """

    rows = session.exec(
        select(Claim)
        .where(Claim.evaluation_score.is_not(None))
        .order_by(Claim.created_at.desc())
        .limit(limit)
    ).all()

    scores = [
        c.evaluation_score
        for c in rows
        if c.evaluation_score is not None
    ]

    return {
        "count": len(rows),
        "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "pass_rate": (
            round(sum(1 for s in scores if s >= 0.7) / len(scores), 4)
        ) if scores else 0.0,
        "recent": [
            {
                "claim_id": c.claim_id,
                "score": c.evaluation_score,
                "decision": c.final_decision,
                "status": c.status,
                "normalized_decision": _normalize_decision(c),
                "created_at": c.created_at,
            }
            for c in rows
        ],
    }
