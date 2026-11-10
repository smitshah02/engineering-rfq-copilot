"""Public API contracts (Pydantic v2). Structural validation only.

These models check shape, types and wire formats of untrusted input. Manufacturing,
catalog, inventory, capacity, pricing and approval decisions stay in
``rfq_copilot.services``; a structurally valid request may still be unsupported.
No module here imports the database layer.
"""
