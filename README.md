RoleReach

Automated APM job search pipeline , built and running in production.

What it does

Scrapes 6+ sources daily (Cutshort, Instahyre, JSearch, Google Jobs, HN, direct ATS), enriches leads with hiring manager contacts via Snov.io, drafts personalised cold emails via Claude API, and delivers a morning digest to Telegram — all triggered automatically at 8 AM IST via GitHub Actions.

Stack

Python · Flask · Supabase PostgreSQL · GitHub Actions · Railway · Telegram Bot API · Snov.io · SerpAPI · RapidAPI

Built as

A PM-led build — product decisions, architecture, spec, and iteration owned end to end. AI-assisted development used throughout to ship faster without compromising on thinking depth or decision quality.

Live

Dashboard: https://rolereach-production.up.railway.app
