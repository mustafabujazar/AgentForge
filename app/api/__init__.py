"""HTTP API package for AgentForge.

Routers are imported explicitly from ``app.main`` so that importing this package
stays free of side effects (``app.db.seed`` reuses the tolerant RAG bridge here).
"""
