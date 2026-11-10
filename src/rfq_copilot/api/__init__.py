"""HTTP API foundation: liveness and system metadata only, no business endpoints.

Run with ``uvicorn --factory rfq_copilot.api.app:create_app``. Importing this package
creates no application, reads no environment and touches no database.
"""
