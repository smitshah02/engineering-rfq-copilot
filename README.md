
# Engineering RFQ Multi-Agent Copilot

A production-oriented engineering RFQ workflow automation
project using multi-agent AI, deterministic business rules,
secure tool integrations, and human approvals.

## Development Status

- Roadmap: 73 days
- Day 1: Architecture completed
- Day 2: Development environment setup

## Technology Stack

- Python 3.12
- uv for dependency management
- FastAPI and Pydantic (planned)
- LangGraph (planned)
- Secure MCP (planned)
- PostgreSQL (planned)
- Docker (planned)

## Dataset

Synthetic Engineering RFQ Dataset v1.1.0.

Status: Independently validated and frozen.

## Local Setup

1. Install uv.
2. Run `uv python install 3.12`.
3. Run `uv sync`.
4. Run `uv run ruff check .`.

## Repository Structure

- docs/ : Architecture and development documentation
- data/ : Approved synthetic dataset
- src/ : Python application source code
- tests/ : Automated tests
