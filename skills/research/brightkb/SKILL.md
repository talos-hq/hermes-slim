---
name: brightkb
description: "Ask BrightKB's knowledge base and cite what it returns."
version: 1.0.0
author: "osint observer (@OSINT_OpSec) + Hermes Agent"
license: Unlicense
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, health, nutrition, knowledge-base, citations]
    category: research
    related_skills: [grounded-citations]
---

# BrightKB Skill

BrightKB is a citation-backed knowledge base of pre-extracted facts — no language model, no
generation at query time. It answers health, nutrition, food and natural-medicine questions from
5.7 million sourced facts and returns the sources behind each one. This skill talks to it over its
OpenAI-compatible endpoint; it does not give medical advice and cannot answer outside the loaded
modules.

## When to Use

- A question turns on food, nutrients, herbs, supplements, dosages, or natural remedies and the
  answer must be traceable to a document.
- You want a citation set for a health claim instead of a web search (`web_search`) or a model's
  recollection.
- Not for breaking news, product price or availability, or anything that happened after the
  modules were built (2026-11-22 for the two published modules).

## Prerequisites

- A reachable BrightKB endpoint. This fork defaults to the hosted instance
  `https://brightkb.onrender.com/v1`; a local BrightKB install listens on
  `http://127.0.0.1:27495/v1`. Override with `BRIGHTKB_URL`.
- No API key. Any non-empty string works if a client insists on one.
- The endpoint must have accepted BrightKB's terms of use. Until then every query returns
  `403 terms_not_accepted`.

Check reachability before a session of questions:

```bash
curl -s "${BRIGHTKB_URL:-https://brightkb.onrender.com/v1}/models"
```

## How to Run

```bash
python3 scripts/brightkb.py "What are the benefits of turmeric?"
python3 scripts/brightkb.py --markdown "Is ashwagandha safe with thyroid medication?"
python3 scripts/brightkb.py "What is berberine?" "Does it lower blood sugar?"
```

Questions come back as JSON (default) or Markdown (`--markdown`); several arguments return a list
in the same order.

## Quick Reference

| Action | Call |
|---|---|
| Liveness | `curl -s "$BRIGHTKB_URL/models"` |
| Ask one question | `python3 scripts/brightkb.py "question"` |
| Several questions | `python3 scripts/brightkb.py "q1" "q2"` |
| Markdown answer | `python3 scripts/brightkb.py --markdown "question"` |
| Raw API | `POST $BRIGHTKB_URL/chat/completions`, model `ika-knowledge-base` |

## Procedure

1. Check the endpoint answers `/models`. If it 403s on a query instead, report the terms problem;
   do not retry.
2. Rewrite the user's question as one focused question naming the food, nutrient, herb or topic.
   Split a compound question into one request per subject — only the last user message is read.
3. Run the script and read the JSON: the answer text, the facts, and each fact's sources.
4. Carry the sources into the reply as citations (`grounded-citations` if the answer is long), and
   keep BrightKB's wording for anything quantitative — dosages, limits, interactions.
5. Say plainly when BrightKB has nothing: an empty or thin answer is a real answer, not a reason
   to fall back to a model's guess.

## Pitfalls

- **Only the last user message is read** and nothing is remembered between requests. System
  prompts and conversation history are ignored; put the whole question in the call.
- **One question per request.** "What is X and is it safe?" retrieves badly; ask twice.
- **403 `terms_not_accepted`** means the operator has not accepted BrightKB's terms on that
  installation. There is no workaround from the client side.
- **It never says "I don't know".** An off-topic question still returns `found: true` with facts
  matched on a single word: "Who won the 2026 World Cup?" comes back with facts about ketchup
  cups and Pimm's. `matched_topic` is not a relevance signal — judge the facts yourself and say
  BrightKB has nothing rather than passing the noise through.
- **Facts are fixed at build time.** The published modules were built 2026-11-22; treat anything
  newer as out of scope.
- **Licence limits reuse.** BrightKB's content carries non-commercial terms (PolyForm
  Noncommercial 1.0.0, CC BY-NC 4.0) — quote with attribution, do not republish wholesale.
- **No streaming.** Long questions still return in one response; a timeout means the endpoint is
  cold, not that the question is too hard.

## Verification

- `curl -s "$BRIGHTKB_URL/models"` returns `ika-knowledge-base`.
- A known-good question returns non-empty `facts` with a `sources` entry per fact.
- An off-topic question (e.g. "Who won the 2026 World Cup?") answers 200 with loosely matched
  facts rather than an error or an empty result — recognise it as a miss and say so.
