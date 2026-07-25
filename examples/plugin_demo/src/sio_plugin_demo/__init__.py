"""An out-of-tree SIO plugin: one connector, one rule, no core changes (PRD M22).

This package exists to make a specific claim falsifiable. The PRD asserts that the platform can be extended
without modifying it, and the only honest way to demonstrate that is a package that lives outside the tree,
depends on nothing but the two public libraries, and is exercised by a test that fails if the claim breaks.

What it adds:

* **a connector** — a fictional tide gauge, polled on an interval, publishing IoT observations;
* **a rule** — a flood warning that fires when the gauge reads above a threshold.

Together they cover the interesting half of the claim: the connector brings data the platform has never seen,
and the rule reasons about it. Neither required a line of change in `services/` or `libs/`.

A tide gauge specifically, because it is *not* something the yard simulator produces. A plugin that adds a
second camera connector proves much less — the platform already knows what a camera is.
"""

from .connector import TideGaugeConnector
from .rules import tide_flood_warning

__all__ = ["TideGaugeConnector", "tide_flood_warning"]
