"""Agent runtime: loop, session helpers, and budget tracking.

Peer package to app/api, app/crud, app/models, app/services — see
docs/AGENT_ARCHITECTURE.md §2 for the layering rule (this package may call
app/crud, never bypass it with raw SQL).
"""
