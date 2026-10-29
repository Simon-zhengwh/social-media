# Chat Edit Image Attachments Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let poster-design chat edits upload or paste up to five images and pass their URLs to both edit branches.

**Architecture:** Upload through the existing asset API, send asset ids in the edit request, persist them on the edit state/message, and resolve them to URLs inside the existing gateway and Agent edit branches.

**Tech Stack:** React 19, TypeScript, FastAPI, Pydantic, MongoDB, pytest.

---

### Task 1: Backend request and persistence

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/routes/whole_image.py`
- Test: `backend/tests/test_whole_image.py`

**Step 1: Write the failing test**

Add a test that constructs `WholeImageEditRequest` with duplicate `reference_asset_ids`, verifies deduplication and the five-item limit, then calls `create_edit` and asserts the ids are stored in `edit.reference_asset_ids` and the user message contains attachment URLs.

**Step 2: Run test to verify it fails**

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest tests/test_whole_image.py -k reference_asset -v`

Expected: FAIL because the schema field does not exist.

**Step 3: Write minimal implementation**

Add the normalized field to `WholeImageEditRequest`; validate referenced assets before claiming the task; persist the ids; add message attachments; pass the ids into `run_edit` and retry.

**Step 4: Run test to verify it passes**

Run the same pytest command. Expected: PASS.

### Task 2: Pass images to both edit branches

**Files:**
- Modify: `backend/app/services/whole_image.py`
- Test: `backend/tests/test_whole_image.py`

**Step 1: Write the failing test**

Seed a generated task and a separate uploaded reference asset. Call `run_edit` with `reference_asset_ids`. Assert the gateway receives original content, current poster, then the attached image; assert the Agent input contains both URLs and the model input records an `edit_reference` image.

**Step 2: Run test to verify it fails**

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest tests/test_whole_image.py -k attached_image -v`

Expected: FAIL because `run_edit` ignores attachments.

**Step 3: Write minimal implementation**

Load attachment refs in the gateway branch, append them after the current poster, update prompt roles, add them to `_model_input_images`, and append their public URLs to the Agent input.

**Step 4: Run test to verify it passes**

Run the same pytest command. Expected: PASS.

### Task 3: Frontend upload and paste composer

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/components/WholeImageStudio.tsx`
- Modify: `frontend/src/components/WholeChatPanel.tsx`
- Modify: `frontend/src/styles.css`

**Step 1: Write the failing check**

Run `Set-Location frontend; npm run build`. Expected before implementation: no attachment-related compile coverage.

**Step 2: Implement the minimal UI**

Add attachment types, API parameter, upload callback, hidden file input, paste handler, pending previews, send payload, retry preservation, and history thumbnails.

**Step 3: Run checks**

Run: `Set-Location frontend; npm run build; npm run lint`

Expected: both PASS.

### Task 4: Final verification

Run: `Set-Location backend; .\.venv\Scripts\python.exe -m pytest`

Expected: all tests PASS. Then run the two frontend checks again and inspect `git diff` for unrelated changes.
