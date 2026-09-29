"""Every "a reason is mandatory" request body rejects a reason that is only whitespace.

No database, no HTTP: these are the request models themselves. `min_length=1`
counts spaces, so before `RequiredReason` a body of `{"reason": "   "}` passed
validation. For the result override that reached
`ck_result_overrides_reason_not_blank` and surfaced as a 500; for the removal and
disqualification routes it was silently accepted as if it were a justification.

The HTTP-level check for the override routes is in `test_result_overrides.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.schemas import (  # noqa: E402
    JudgeRemovalIn,
    RegistrationRemovalIn,
    ResultOverrideClearIn,
    ResultOverrideIn,
    SubmissionDisqualifyIn,
    TeamMemberRemovalIn,
)


def _build(model, reason: str):
    if model is ResultOverrideIn:
        return model(tier="neither", reason=reason)
    return model(reason=reason)


REASON_MODELS = [
    JudgeRemovalIn,
    RegistrationRemovalIn,
    ResultOverrideClearIn,
    ResultOverrideIn,
    SubmissionDisqualifyIn,
    TeamMemberRemovalIn,
]


@pytest.mark.parametrize("model", REASON_MODELS, ids=lambda m: m.__name__)
@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", " \r\n\t "])
def test_a_blank_reason_is_rejected(model, blank: str) -> None:
    with pytest.raises(ValidationError):
        _build(model, blank)


@pytest.mark.parametrize("model", REASON_MODELS, ids=lambda m: m.__name__)
def test_a_reason_is_trimmed(model) -> None:
    assert _build(model, "  Rule violation found \n").reason == "Rule violation found"


@pytest.mark.parametrize("model", REASON_MODELS, ids=lambda m: m.__name__)
def test_the_length_cap_still_applies(model) -> None:
    assert _build(model, "x" * 500).reason == "x" * 500
    with pytest.raises(ValidationError):
        _build(model, "x" * 501)
