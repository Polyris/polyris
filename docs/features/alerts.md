# Notifications

Polyris tells you when a task fails via **in-app browser notifications** —
automatic and free, with nothing to configure. On failure the notify Lambda
fans out to every enabled channel, so failures are never silent.

> **Note (ADR #103).** Alerts used to be declared in the DAG with an `alerts=`
> argument. That argument has been **removed** — passing it raises a `TypeError`.
> Alert configuration is not part of the DSL.
