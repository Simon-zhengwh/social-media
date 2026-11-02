# Food Photo Preservation Implementation Plan

**Goal:** Reduce food redraws through clearer generation and edit prompts while retaining whole-image generation.

**Architecture:** Keep the existing references, models, storage, versions, and dual edit branches. Add one shared food-photo preservation instruction; identify the current poster as the edit base instead of a style-only reference. Apply the same preservation instructions to the whole-image Agent edit branch without changing the standalone poster-edit input builder.

**Tech Stack:** Python, FastAPI, existing Core AI client, pytest/mongomock.

## Approved design

The user selected prompt and edit-constraint fixes, accepting that these reduce redraw probability without guaranteeing unchanged pixels. Original food photos or complete plates may be cropped, uniformly scaled, or moved as units; their internal food, tableware, lighting, and arrangements must be preserved unless the user explicitly requests a specific change. Typography and decoration retain design freedom outside protected photo content.

Edits must use the selected current poster as the base. Original content images remain available for checking fidelity, rather than prompting a reconstruction. Both edit branches receive the shared food-preservation rule and the current instruction. Old campaign copy and earlier edit requests are not replayed. Original image order, model-input snapshots, and version selection remain compatible.

## Steps

1. Add regression tests in `backend/tests/test_whole_image.py` for raw/optimized generation and both edit branches. Run them and confirm they fail on missing protection instructions.
2. Update `backend/app/services/whole_image.py`: constrain generation freedom, add food preservation to content-image roles, correct current-poster edit roles, and share preservation instructions between gateway and Agent edits.
3. Update existing assertions for the deliberately changed image-role and Agent prompt contracts, then run the whole-image and standalone poster-edit tests.
4. Update `README.md` with the new prompt behavior and its limits. Check the diff and run the backend tests if focused checks pass.

No new model calls, image-generation spend, compositing pipeline, or schema changes are required. Automated tests establish the input contract; visual improvement requires a later real generation comparison.
