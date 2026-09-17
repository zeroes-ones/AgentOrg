#!/usr/bin/env python3
"""evals — the behavioural suite: scenarios that test judgments, with a regression gate.

Unit tests prove the engine does what the code says. This suite proves it makes *good decisions*,
which is a different question and the one that matters for a system whose failure mode is confident
wrong output rather than a crash.

Run it with:
    python3 -m engine.evals.runner
    python3 -m engine.evals.runner --freeze        # record the current results as the baseline
"""

from .runner import ScenarioResult, Outcome, compare_to_baseline, freeze_baseline, run_suite

__all__ = ["Outcome", "ScenarioResult", "compare_to_baseline", "freeze_baseline", "run_suite"]
