"""AgentOrg — a model-agnostic, multi-agent software engineering organization.

The engine is the Python half of the application: it owns orchestration, agent
lifecycle, provider access, context management and observability. The macOS app
talks to it exclusively over the NDJSON protocol in :mod:`engine.protocol`.
"""

__version__ = "0.1.0"
