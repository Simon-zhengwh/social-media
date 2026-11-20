# Poster Series Page

This skill handles exactly one page. The application owns the group, page order, page
count, shared visual plan, optional locked cover and official Logo overlay. It calls this
skill independently for each page. Keep the requested page ID and return a single page.

## Input

The user message is one JSON object:

```json
{
  "operation": "poster-series-page",
  "page": {"page_id": "wspage_...", "order": 0, "role": "cover", "page_count": 3},
  "group": {"group_id": "wsgrp_...", "model": "gpt-image-2.5-sunburst", "canvas": {"width": 1080, "height": 1350}, "logo": {"enabled": true, "box": {"x": 600, "y": 1200, "width": 400, "height": 100}}},
  "user_prompt": "用户原始需求",
  "page_prompt": "本页需要执行的提示",
  "requested_model": "gpt-image-2.5-sunburst",
  "content_pool": [
    {"asset_id": "asset-1", "name": "纸杯蛋糕", "url": "https://...", "role": "content"},
    {"asset_id": "asset-2", "name": "甜品柜", "url": "https://...", "role": "content"},
    {"asset_id": "asset-9", "name": "三连参考图", "url": "https://...", "role": "style_reference"}
  ],
  "planned_content_refs": [{"asset_id": "asset-1", "target": "纸杯蛋糕"}],
  "group_plan": {"visual_direction": "warm cream, rounded bold brown type, diagonal photo cards", "pages": []},
  "cover": {"asset_id": "asset-cover", "version_id": "wver_...", "url": "https://..."},
  "output_contract": {}
}
```

- `content_pool` is the complete material pool for this task. Pick what this page actually
  needs. It is allowed to use only one image on a cover, to leave images unused, and to
  reuse one image across pages. Follow any image the user names in `user_prompt`.
- Use `group_plan.visual_direction` on every page, including the cover. Match the common
  palette, typography, photography treatment and decorations. Use `page_plan` for the page's
  role, topic and material choices. The cover introduces the theme and each interior adds its own content.
- If an interior has a `cover.url`, send it to `generate_image` as the cover/style reference
  and echo its `asset_id`. If `cover` is `null`, the cover is being generated alongside this
  page. Use the shared visual plan and the supplied style references. Return a null `cover_asset_id`.
- Style references in the pool are style-only. Never present style-only content as food
  or as a protected dish photo.
- When `group.logo.enabled` is true, use the exact location in `group.logo.box` and the
  background treatment in `group.logo.treatment`. In the production prompt describe the
  box as a small location relative to the entire image. Draw the planned background as part
  of a nearby title block, color accent, or existing layout element. Keep it clean, with no
  type, seal, icon, or mark in that spot. The application adds the official mark. Keep the
  photos, text, and background arranged across the entire poster. A Logo does not require
  its own footer, a divider, a full-width band, or a large empty gap. Include a separate
  footer only if called for by the user's design requirements.

## Generation

Use the current session's native image tool `generate_image` (or the actually exposed
equivalent). Send one call for this page:

- `model`: exactly `requested_model`. Never substitute another model. If the platform
  rejects that model or a parameter, stop and report the rejection instead of producing a
  different model's image.
- `prompt`: a complete production prompt derived from `page_prompt` and `user_prompt`.
  Name each input image by its role and position. Preserve real food photos as protected
  photographic units: no redrawing, adding or removing dishes.
- `input_images`: JSON string containing selected content images, style images, and the
  cover for interior pages when supplied. Use the actual `url` values from the pool.
- `quality`: `xhigh`; `n`: 1; `output_format`: `png`.
- `size`: a size whose aspect ratio matches `group.canvas`. The application validates and
  fits the PNG to the exact canvas, so do not change the aspect ratio.

Do not use HTTP image APIs, SDK keys, another Agent, or a different browser driver. Only
the native `generate_image` tool may create the page image.

Do not call caption/vision tools for this task: the application performs its own pixel and
canvas checks. Keep tool turns to generate, resize/save, submit.

## Output

Write the final page to `page.png` in the **same working directory the shell tool reports
from `pwd`** (usually `/workspace`), never to `/tmp`. Resize the native PNG to the exact
`group.canvas` size with Pillow, save it as `page.png` there, and verify the file exists
and decodes before submitting. Submit exactly that one final PNG with the platform
artifact tool as `page.png`, passing the absolute path you just verified (for example
`/workspace/page.png`). Do not submit the raw, unresized model file. If the submit tool
rejects the path once, list the directory, fix the path and resubmit the same file;
never generate the image again just to fix a submission path.

Then finish with one JSON object, and nothing else outside that object:

```json
{
  "page_id": "wspage_...",
  "role": "cover",
  "model": "gpt-image-2.5-sunburst",
  "prompt": "the exact prompt sent to generate_image",
  "content_refs": [{"asset_id": "asset-1", "target": "纸杯蛋糕"}],
  "style_reference_ids": ["asset-9"],
  "cover_asset_id": null
}
```

Rules for the output:

- `page_id`, `role` and `model` must match the request exactly.
- `content_refs` must contain only `asset_id` values from `content_pool` with role
  `content`, and must match the content images actually sent to `generate_image`.
- `style_reference_ids` must contain only pool asset ids used purely as style references.
- `cover_asset_id` must equal `cover.asset_id` for detail pages with a supplied cover. Use `null` otherwise.
- On a tool rejection or any failure, do not submit an image. Return
  `{"page_id": "...", "role": "...", "error": "actionable reason", "model": "..."}`.


## Group planning

`operation: "poster-series-plan"` is called once per group. Read the full `content_pool`,
the page list, the user prompt and the Logo settings. Decide the group's theme and split
it into the requested pages without repeating a topic. Return exactly:

```json
{
  "group_id": "wsgrp_...",
  "visual_direction": "A concrete shared recipe for palette, typography, photo framing and decoration",
  "pages": [
    {
      "page_id": "wspage_...",
      "role": "cover",
      "title": "封面标题",
      "intent": "这一页要讲什么",
      "content_refs": [{"asset_id": "asset-1", "target": "纸杯蛋糕"}],
      "style_reference_ids": ["asset-9"]
    }
  ],
  "logo_box": {"x": 900, "y": 60, "width": 94, "height": 94},
  "logo_treatment": "Small cocoa-brown paper tab integrated in the corner of the title block, with no wording"
}
```

- Every requested `page_id` must appear exactly once with its role.
- Choose one concrete `visual_direction` that the cover and all interiors can apply independently.
- Only pool asset ids may appear. The cover may use one image; leave unused images unused.
- When the Logo is enabled, read `group.logo.artwork` for its color and aspect ratio. Choose
  a compact `logo_box` inside the cover's existing composition that also works on all interiors.
  Match the box to the mark's proportions, and honor the position and/or size specified in
  `user_box`. Aim for the scale of a small brand accent. Give `logo_treatment` as one shared
  design direction with a contrasting color from the palette and a clear relationship to a
  local layout element. Avoid giving the Logo its own footer, page band or extra whitespace.
- Do not generate an image for the plan operation.

## Workflow

1. Parse the one JSON user message. It is either `poster-series-plan` or
   `poster-series-page`; stop for any other operation.
   - `poster-series-plan`: produce the group outline once (see Group planning), submit no
     image, and return the plan JSON.
   - `poster-series-page`: continue with the steps below.
2. Read `page.role`, `requested_model`, `group.canvas`, `content_pool`, `group_plan`, `page_plan` and `cover`.
3. Choose this page's content and style images. Include the supplied `cover.url` for an interior when present.
4. Write the production prompt and call native `generate_image` exactly once with the
   requested model, `quality: "xhigh"`, `n: 1`, `output_format: "png"` and the selected
   images.
5. Verify the returned PNG decodes and its aspect ratio matches `group.canvas`.
6. Submit exactly one final PNG as the `page.png` artifact.
7. Return the JSON object from the Output section with the actual model, prompt and refs.

## Decision logic

- Cover: choose a main visual; one content image is valid. Set the series theme and title.
- Detail: give this page its own content and copy. Follow the shared visual direction and
  use the locked cover as a style reference when supplied.
- If the user names a specific image, use it. If the page needs fewer images than the
  pool, leave the rest unused; never invent a second dish to fill space.
- If the requested model is rejected, do not switch models. Return the error object.

## Error handling

- Tool rejection, timeout, unreadable image or wrong aspect ratio: submit
  no image and return `{"page_id": "...", "role": "...", "model": "...", "error": "reason"}`.
- Never use a fallback model, direct HTTP API, SDK key or another agent.
- Do not retry more than once; report the actual rejection so the application can show it.

## Quality assurance

- Confirm one image artifact exists and decodes as PNG at `group.canvas` size.
- Confirm `model` equals `requested_model` and `prompt` is the exact prompt sent.
- Confirm every `content_refs` asset id exists in `content_pool` with role `content`.
- Confirm `cover_asset_id` equals the supplied cover ID for an interior with a cover, or is null otherwise.
- Do not run extra vision/caption passes; the application performs its own pixel, Logo
  and canvas-size checks on the submitted file.
