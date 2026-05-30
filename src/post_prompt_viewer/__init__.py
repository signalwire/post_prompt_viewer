"""Post Prompt Viewer.

A small FastAPI microservice that ingests SignalWire AI Agent ``post_prompt``
payloads, stores them, and presents the conversation, telemetry, and
recording-derived latency in a clean, drill-down UI.

It is the modern successor to the original ``post.cgi`` collector and
``index.cgi`` viewer (kept in ``reference/`` for guidance).
"""

__version__ = "0.1.0"
