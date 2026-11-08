# UAT Integration Implementation Plan

**Goal:** Merge remote UAT integration into the latest committed poster editor without losing merchant isolation or new editing features.

**Architecture:** Merge `origin/main` (`ceb1ef5`) into candidate `ef5803a` on an isolated integration branch. Keep the candidate's consolidated workflow and artifact editor, and carry forward authenticated merchant routing, scoped Mongo access, AM media storage, background recovery and maintenance controls. Existing source checkouts and untracked documents remain intact during validation.

**Tech Stack:** FastAPI, MongoDB, React, TypeScript, Vite, PowerShell, AKS.

## Tasks

1. Establish candidate baseline with backend pytest and frontend build/lint; fetch remote and merge without committing.
2. Resolve startup, router, profile and UI conflicts, preserving both intended behaviors. Keep retired standalone workflows disabled. Apply merchant routing to replacement and artifact APIs, including generated URLs, preview access and background task recovery.
3. Extend merchant isolation and integration regressions for replacement tasks, artifact downloads/previews, and new agent-test APIs. Verify regressions fail for missing integration, then implement focused fixes.
4. Run backend tests, frontend build/lint and existing frontend tests; inspect the complete merge and request independent code review. Fix all significant findings.
5. Commit the merge, fast-forward source checkouts without overwriting local work, and push the integrated branch/commit. Run the UAT deployment preflight against the pushed commit.
6. Build from sanitized committed Git content, publish UAT images, verify rollout, route, public health and static assets. Record the previous tags for rollback. Use maintenance if required by the actual runtime changes and drain active work before restart.

## Acceptance

- Both parent histories retained; no conflict markers or unmerged paths.
- Merchant authorization, CSRF, scoped records and AM storage remain enforced on all new APIs.
- New workflow, artifact selection, previews and downloads remain functional.
- Relevant regression tests, backend suite and frontend checks pass before publication.
- UAT release uses the exact pushed and verified commit, with healthy API/Web pods and rollback metadata.
