# Merchant profile implementation plan

**Goal:** Display source-backed merchant information beside existing platform style settings.

**Architecture:** Separate MongoDB merchant_profiles records, explicitly linked by brands.merchant_id. Brand detail returns a read-only display profile. Preserve existing visual settings and generation behavior.

**Tech Stack:** FastAPI, MongoDB, React, TypeScript.

1. Add failing API tests for linked, unlinked and shared merchant profiles.
2. Add a source-preserving, explicit-ID import script and profile projection in brand detail. No merchant guessing by name.
3. Add the merchant reading panel beside current style editing; expose original source fields, show missing facts as missing.
4. Import the supplied SEE JOY record, bind only the existing seejoy style, verify API, relevant backend tests, frontend build and lint.

Accepted layout: 左右对照. Supplied marketing descriptions remain labeled as source descriptions, not independently verified facts. Do not infer consumption occasions, audience ranking, sales performance, or brand assets.
