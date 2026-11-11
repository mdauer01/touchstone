"""Whether an output's name names a governed metric.

One rule for candidate selection and for the name check on a verdict: a name names a
metric when the metric's words occur in it, in order and next to each other
(`revenue`, `eu_revenue`, `revenue_1996`). Words are split on separators and case
changes and compared without case; a few unambiguous abbreviations count as the word
they stand for (`avg_order_value` names `average_order_value`). Every other word is
kept: `gross_revenue` does not name `net_revenue`, and `avg_revenue` still says it is an
average. A match only says which definition to compare with; it says nothing about the
calculation.

Separate from `resolution.normalize_name`, which keys stored concept identities.
"""

from __future__ import annotations

import re

# Unambiguous abbreviations only: each stands for one word in metric names.
ABBREVIATIONS = {"avg": "average"}


def words(name) -> tuple[str, ...]:
    """The words of a name: split on anything but letters and digits and at case
    changes (`avgOrderValue`), lower case, abbreviations expanded."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or ""))
    return tuple(ABBREVIATIONS.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower()))


def names_metric(name, metric) -> bool:
    """True when `name` names `metric`: the metric's words occur in the name's, in
    order and adjacent."""
    have, want = words(name), words(metric)
    if not want:
        return False
    return any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))
