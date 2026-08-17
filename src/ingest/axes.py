"""Near-miss axis detection.

A near-miss question is only legitimate if the corpus genuinely lacks the
answer. Withholding a label SECTION is not enough to guarantee that: metformin's
`dosage_and_administration` carries a full "Pediatric Dosage" paragraph even
with `pediatric_use` withheld, so a pediatric question about metformin is
answerable from the shipped text. Absence is therefore measured over the
assembled corpus, not inferred from section names (ARCHITECTURE.md §10 — the gate measures retrieval strength, not source
correctness, so the corpus's own claims about what it lacks must be verified).
"""

from __future__ import annotations

import re

NEAR_MISS_AXES: dict[str, str] = {
    # STEMS, not literal phrases. Literal matching failed twice: "hepatic
    # impairment" missed metformin's "history of liver disease" and amoxicillin's
    # "Liver: A moderate rise in AST", so the scan reported hepatic absent while
    # the corpus discussed it. A false ABSENCE is the dangerous direction — it
    # ships a near-miss that is actually answerable, counted as a false decline
    # at every operating point.
    "pediatric": r"pediatr|paediatr|\bchild|infant|neonat|adolescen|juvenile|newborn",
    "overdose": r"overdos|poison|toxicit|\btoxic\b|acute ingestion",
    "pregnancy": r"pregnan|lactat|nursing mother|breast-?feed|teratogen|fetal|foetal|\bfetus",
    "geriatric": r"geriatr|elderly|older patient|advanced age|\b65 years",
    "hepatic": r"hepat|\bliver\b",
    "renal": r"renal|kidney|creatinine|eGFR|nephro",
    "carcinogen": r"carcinogen|mutagen|tumou?r|malignan",
    "immunization": r"immuni[sz]|vaccin",
    "driving": r"driving|operate machinery|machinery",
    "storage": r"\bstorage\b|store at|room temperature|excursions permitted",
}


def verified_absent_axes(text: str) -> list[str]:
    """Axes with no keyword anywhere in `text`, sorted for stable manifests."""
    lowered = text.lower()
    return sorted(
        axis for axis, pattern in NEAR_MISS_AXES.items() if not re.search(pattern, lowered)
    )
