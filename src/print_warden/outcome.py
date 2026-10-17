"""How a print really ended, from the person who saw it.

Klipper knows whether a print completed, not whether it is any good, and a cancelled print says nothing about why.
After every print the phone gets the end photo with **Good** / **Failed**; Failed asks for the reason over two pages
(phones show three buttons). The label lands on the job (`outcome`, `outcome_reason`, who, when); stats and the
"this failed before" warning on `submit_job` read labels first and fall back to how the job ended.

Action ids are `PRINT_WARDEN_OUTCOME_<job id>:<choice>`. A label is a record, not an action: nothing on the printer
changes, so the chat tool `label_print` may set one too.
"""

from __future__ import annotations

from typing import Any

REASONS = ("adhesion", "spaghetti", "layer_shift", "under_extrusion", "other")
CHOICES = ("good", "failed", "more", *REASONS)
Action = tuple[str, str]


def _action(job_id: str, choice: str, label: str) -> Action:
    return (f"PRINT_WARDEN_OUTCOME_{job_id}:{choice}", label)


def tag(job_id: str) -> str:
    return f"print_warden_outcome_{job_id}"


def first(job_id: str, completed: bool) -> list[Action]:
    """Good / Failed after a completed print; straight to the reasons when it was cancelled or errored."""
    if completed:
        return [_action(job_id, "good", "Good"), _action(job_id, "failed", "Failed")]
    return reasons(job_id, 1)


def reasons(job_id: str, page: int) -> list[Action]:
    if page == 1:
        return [
            _action(job_id, "adhesion", "Adhesion"),
            _action(job_id, "spaghetti", "Spaghetti"),
            _action(job_id, "more", "More…"),
        ]
    return [
        _action(job_id, "layer_shift", "Layer shift"),
        _action(job_id, "under_extrusion", "Under-extrusion"),
        _action(job_id, "other", "Other"),
    ]


def parse(payload: str) -> tuple[str, str]:
    """`<job id>:<choice>` from an action id's tail."""
    job_id, _, choice = payload.partition(":")
    if not job_id or choice not in CHOICES:
        raise ValueError(f"bad outcome action {payload!r}")
    return job_id, choice


def label(outcome: str, reason: str | None) -> dict[str, Any]:
    """The job fields for a label; raises on anything but good, or failed with a known reason (or none)."""
    if outcome == "good" and reason is None:
        return {"outcome": "good", "outcome_reason": None}
    if outcome == "failed" and (reason is None or reason in REASONS):
        return {"outcome": "failed", "outcome_reason": reason}
    raise ValueError(
        f"outcome must be 'good', or 'failed' with a reason from {REASONS}; got {outcome!r}/{reason!r}"
    )


def failed(job: dict[str, Any]) -> bool:
    """A label wins over how the job ended (a completed print can still be a failure)."""
    if job.get("outcome"):
        return bool(job["outcome"] == "failed")
    return bool(job.get("state") == "failed")


def reason(job: dict[str, Any]) -> str:
    if job.get("outcome") == "failed":
        return str(job.get("outcome_reason") or "unlabelled")
    return (job.get("error") or "unknown").split(":")[0][:60]
