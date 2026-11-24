---
name: poster-series
description: "Create coordinated poster groups from the user's request in one Agent run. Use when operation is poster-series-generate or a saved series uses a legacy operation. Scope: series generation and ordered PNG delivery. Not for editing a standalone poster or producing HTML."
metadata:
  version: 0.3.0
  author: simon
  tags: [poster, series, image-generation]
---

# Poster Series

## Workflow

For `poster-series-generate`, follow `prompt` as written. Use the supplied canvas and the model assigned to each entry in `groups`. Decide each group's image count, content, order and choice of references from the user's needs. Give the images a coherent style. A cover or any other role is optional and comes from the user.

For `poster-series-generate`, plan all pages before calling the tool. Lock the shared palette, lettering, texture and Logo position, and choose each page's copy and source photos. Use the supplied canvas size or the closest size supported by the requested model that preserves the intended aspect ratio. Export every page at the exact supplied canvas size.

Build one native `generate_image` call per page with the requested model and actual supplied reference URLs. Review the full batch once before sending. Each reference name uses ASCII letters, digits, underscores or hyphens and appears verbatim as `@name` in the prompt with a sentence explaining its purpose. Check the planned content photos against the URLs in each call. Use content roles such as `subject`, `scene` or `prop` for food photos and `style` for style references.

Send the independent first-pass calls together in one assistant tool-call message, using separate calls for separate pages. Use the shared supplied style references so those pages can start independently. If parallel width is limited, use the available batches. Collect all first-pass results before reviewing any page.

## Decision logic

If `logo.options` is present, pick one option per group before generating any page. Use that option's exact `logo_box` in the final manifest. Its `safe_area` is the complete clearance around the visible mark, in the supplied canvas coordinates. Give the tool that rectangle on every page. Scale its coordinates proportionally when the tool works at a different resolution. If options are absent on an older request, choose a compact shared corner box that meets the supplied minimum size and honors `logo.placement`.

Inspect the official Logo reference for its mark color and shape. During `generate_image`, keep the safe area free of text, decorations and busy detail. Use the surrounding design's existing surface, such as a quiet part of a photo or paper texture. In each first-pass prompt, place nearby headings, photos and decoration explicitly to the side of, above or below the rectangle. Use the rectangle's edge coordinates to anchor those elements. Keep the clearance local. Use the actual Logo file for the overlay. Keep it out of the `generate_image` references so the model leaves the area blank.

## Logo composition

When `logo.delivery` is `agent_composited`, download the supplied official artwork from the entry in `references` with `role: "logo"` to the sandbox. Save each no-Logo page as a PNG at the supplied canvas size. Run the bundled script using the skill's resolved directory returned by the platform. Pass the chosen box and `logo.artwork.min_visible_px` from the request:

```bash
python "$SKILL_DIR/scripts/composite_logo.py" \
  --base work/page-1-base.png \
  --logo work/official-logo.png \
  --out work/page-1-final.png \
  --canvas-width "$WIDTH" --canvas-height "$HEIGHT" \
  --box "$LOGO_BOX_JSON" --min-visible-px "$MIN_VISIBLE_PX"
```

The script keeps the original artwork and can add a compact neutral backing inside the box. Its contrast score helps locate a problem. Open the actual composited PNGs for visual review before registering them. For older round requests without `logo.delivery`, keep the reserved area empty and submit the no-Logo PNG as `image_url`. The legacy application applies the overlay.

Plan the visible copy for each poster. The main headline may contain the brand name once. Outside the headline, use product names or details in headers, labels, taglines and footers. Avoid extra brand names, aliases, wordmarks and watermarks. The official Logo can contain its own lettering. Style references guide the look. Exclude example lettering and business descriptors unless the user explicitly wants that copy.

For saved `poster-series-plan` and `poster-series-page` requests, read [Legacy page requests](references/legacy-pages.md). All other operations are out of scope.

## Output specification

Submit each final PNG through the artifact tool. Copy its exact HTTPS image link into `image_url` from the native tool or artifact result. Use a filename only if it uniquely identifies the registered artifact. Return one fenced JSON object with the exact group IDs. Keep the intended display order in each `images` array and list each image’s actual source photos in `content_refs`. If the native tool requests Markdown links, put them after the JSON.

```json
{"groups":[{"id":"<requested group id>","model":"<requested model>","logo_box":null,"images":[{"image_url":"<exact returned HTTPS image link>","content_refs":[]}]}]}
```

With a Logo, provide the chosen `logo_box` with its exact `x`, `y`, `width` and `height`. When `logo.delivery` is `agent_composited`, register two PNGs per image under unique names. Put the visually reviewed composite in `image_url` and the matching no-Logo PNG in `base_image_url`. Only the composite is shown as the result. Use both exact registered URLs. Each content reference contains `asset_id` and an optional `target`.

## Error handling

On failure return `{"error":"<actual reason>"}`. If the tool rejects the model, report the rejection without substituting another model.

## Quality assurance

For `poster-series-generate`, prepare a single review sheet with a full-page thumbnail of every PNG to be submitted as `image_url` and, when a Logo is enabled, a close-up of each `logo_box` at delivery size. Open that sheet with the session's vision capability. Inspect the full artwork for copy, coherent styling, photo fidelity and layout. Check corners and footers for repeated brand names, alternate spellings and copied reference text.

When `logo.delivery` is `agent_composited`, inspect each composited Logo for readable lettering and contrast in both light and dark parts of the mark, clearance and absence of duplicates. For older Logo requests, inspect the reserved area for clearance and a suitable background. A score or successful script exit cannot replace looking at the pixels. Report if the session cannot visually review images.

Keep each page that passes. For any page that fails, record the concrete defect and fix that page's local layout or background with the same Logo box. When the skill owns composition, reapply the original Logo to the revised no-Logo PNG and inspect the new composite. Batch independent fixes in one tool-call message, and review just the changed pages against the same criteria. Keep `content_refs` tied to the original photos represented in the final page.

Parse the final manifest with a JSON decoder. Check that every image URL matches a registered PNG and every PNG matches the canvas. Deliver only when every page passes.
