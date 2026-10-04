"""Measuring quality, so improvements can be shown rather than claimed."""

from .dataset import CASES, EvalCase, case_by_id, cases_by_category, dataset_summary
from .metrics import CaseResult, mean_reciprocal_rank, recall_at_k
from .runner import (
    Comparison,
    EvalReport,
    RunConfiguration,
    compare,
    evaluate_case,
    latest_reports,
    load_report,
    run_evaluation,
    save_report,
)

__all__ = [
    "CASES",
    "CaseResult",
    "Comparison",
    "EvalCase",
    "EvalReport",
    "RunConfiguration",
    "case_by_id",
    "cases_by_category",
    "compare",
    "dataset_summary",
    "evaluate_case",
    "latest_reports",
    "load_report",
    "mean_reciprocal_rank",
    "recall_at_k",
    "run_evaluation",
    "save_report",
]
