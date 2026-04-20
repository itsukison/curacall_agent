"""Tier derivation for treatment eligibility.

Must stay in sync with lib/treatments/tier.ts.
Tier 1: anyone can book
Tier 2: returning patients only
Tier 3: requires doctor approval in patient_treatment_approvals
"""


def derive_tier(new_patient_bookable: bool, requires_consultation: bool) -> int:
    if requires_consultation:
        return 3
    if not new_patient_bookable:
        return 2
    return 1


TIER_LABELS = {
    1: "初診・再診ともに予約可能",
    2: "再来院の患者のみ予約可能",
    3: "事前の診察・承認が必要",
}

TIER_INLINE_SUFFIX = {
    1: "",
    2: " [再診専用]",
    3: " [要事前相談]",
}
