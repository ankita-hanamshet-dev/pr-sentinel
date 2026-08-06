"""LangGraph orchestration for the aggregate pipeline: dedup -> ground -> critic ->
(conditional re-critic, max 2) -> score -> format.

CLAUDE.md: the four specialist agents run as separate GitHub Actions jobs, never as
parallel LangGraph branches within one process -- this graph is a linear pipeline
with exactly one cycle (the critic can re-run once), never a fan-out.
"""
