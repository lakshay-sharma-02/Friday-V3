"""Reasoning layer — ensemble-based confidence through model agreement.

The ``EnsembleReasoner`` fires 2-3 cheap models in parallel on the same
prompt and uses their agreement as a confidence signal. When all models
agree on the same answer, that answer carries **high confidence** and can
be stated directly (\"I'd bet on this\"). When they disagree, the result
carries **low confidence** and the answer is hedged explicitly.

This is the direct engineering translation of "I'm fairly sure" vs
"I'd bet the suit on this" — it's a measured property of the ensemble,
not a model performing confidence.
"""

from .engine import EnsembleReasoner, EnsembleResult, AgreementLevel

__all__ = ["EnsembleReasoner", "EnsembleResult", "AgreementLevel"]
