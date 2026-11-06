"""touchstone — checks executed SQL against approved metric definitions.

Reads a semantic layer's definitions and the query history, and flags results
presented as a governed metric but computed differently, with the evidence
(`touchstone check`). The estate scan and its certification machinery are
parked under `touchstone experimental`.
"""

__version__ = "0.1.0"
