"""Operations layer around the pipeline: structured run logs, metrics, local alerts, dashboard, AWS task.

Deliberately a separate top-level package: a top-level ``platform`` package would shadow the stdlib module.
Everything here is SYNTHETIC-data tooling; nothing sends alerts or talks to a network unless you run the
AWS task entrypoint (``python -m retail_ops task``) against infrastructure you provisioned yourself.
"""

__version__ = "0.2.0"
SERVICE = "retail-pipeline"
