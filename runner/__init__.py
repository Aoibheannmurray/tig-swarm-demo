"""Hosted fleet runner — Tier 1 of the server-first onboarding plan.

A standalone FastAPI service (`runner.service:app`) that runs contributor
fleets in the cloud so they need zero local install. Deployed as its own
Railway service alongside the coordination server; see runner/README.md and
docs/server-first-onboarding-plan.md §8.
"""
