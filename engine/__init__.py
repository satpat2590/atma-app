"""Gyani/Atma engine — the core system (DB + AI + agentic workflows).

This package is the product. The FastAPI routes and the Hermes cron prompts are
thin skins over these modules; the frontend is whatever shape the user
configures. Nothing in here depends on FastAPI, Telegram, or HTTP framing.

Modules:
    db       — Supabase connection seam
    llm      — provider seam (OpenRouter + Gyani model)
    jev      — TypeSafe Jev decisions client (answer classifier, fail-open)
    context  — pluggable context-source rail (WHOOP / Obsidian / git / Edoras)
    exam     — the exam lifecycle (teach → prove → unlock)
"""
