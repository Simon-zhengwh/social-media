---
name: image-gen-skill
description: Generate and edit images using the Core AI platform generate_image tool. Use for poster creation, background replacement, text edits, transparent extraction, and multi-image compositing; preserve the user's image roles, exact text, and requested constraints.
---

# Image Gen Skill

Adapted from core-ai-imagegen and the bundled imagegen prompting guidance. The original license is retained. Execution uses the Core AI platform's built-in `generate_image` tool.

## Execution

- Perform generation, editing, extraction, and compositing with `generate_image`. Do not run an HTTP image client, shell/Python image-generation script, or local compositing pipeline as a substitute.
- Default image model: `gpt-image-2.5-sunburst`; respect an explicitly requested supported model. Request one PNG per call (`n: 1`, `output_format: "png"`). Omit `quality` unless the user requests a quality preference. Never send `standard` or `hd` to this model.
- Use the tool's actual schema and model capability descriptions. The Built-in Tools web listing may omit editing parameters and contain outdated quality examples.
- This skill requires no API key, local environment file, Python runtime, or workstation path. The platform supplies the tool and credentials.
- Read [tool interface](references/tool-interface.md) before the first call or when diagnosing inputs and failures. If the tool is discoverable but inactive, activate `generate_image` with the platform's `activate_tools`; if unavailable, report the missing capability.

## Workflow

1. Identify generation versus editing. Every request to modify, extract, or combine existing images MUST follow the mandatory two-stage editing workflow below: first a meaningful transparent PNG layer, then a final image generated using that layer. New generation without editing an existing base does not require these stages. Pass existing images to the tool; prompt-only regeneration cannot preserve their contents.
2. Identify each input by order and role: edit target, photographic base, graphic overlay, content reference, or style reference. Preserve exact requested text. If images are tool-readable references rather than visible in context, use `caption_image` on the supplied image URL to understand them. Never invent a URL or convert an inaccessible local path into one.
3. Apply [prompting principles](references/prompting.md); consult [sample prompts](references/sample-prompts.md) only for relevant examples. Keep the user's request and list the elements to preserve without adding unrelated design requirements.
4. Select only the required references. For a current message whose images are all required, set `input_images: "attached"`; this preserves attachment order. For a subset or previous generated image, use an explicit JSON array string with the supported reference identifiers. State each reference's contribution in the prompt.
5. Execute the two editing stages sequentially through separate `generate_image` calls. Validate the transparent layer before starting the final-image stage; never run these stages in parallel or substitute one whole-image edit. Keep the source canvas and aspect ratio unless asked otherwise.
6. In stage 1, set `background: "transparent"` and `output_format: "png"`, and explicitly request actual alpha outside the intended layer. Verify that the PNG has both a fully transparent region and visible foreground using available artifact inspection evidence. PNG format, a tool success, a caption, or a checkerboard preview alone does not prove transparency. Preserve existing alpha. If real alpha cannot be verified or is unsupported, report the incomplete workflow and do not proceed to stage 2.
7. Check available output evidence for text, subject, framing, and edges. Use tool-readable vision or a small preview where available; do not add large image payloads to a long conversation. A successful tool response is not proof of visual fidelity.
8. On error, inspect the tool result before a targeted retry. A timeout may already have generated an image: do not blindly resubmit. Do not silently switch models, use direct HTTP, or repair the result with local compositing. Report reference trimming, unsupported masks, or other capability limits.
9. For each completed edit, return BOTH the transparent PNG and the final image using the exact Markdown image links supplied by the tools. Retain each `media_id` and identify which layer was used for the final image. Distinguish tool success, alpha verification, text accuracy, and visual quality; disclose unavailable checks. Never invent a file path, image link, or a successful result. Do not promise pixel-identical preservation from a generative edit.

## Mandatory two-stage editing

These are two stages per user-requested edit, not a recursive requirement to split each internal tool call again. Small text changes, resizing, reflow, and photo replacements all require both stages, even when the user does not explicitly request transparency.

1. **Extract and prepare the transparent PNG.** Supply the original image and any needed replacement references to `generate_image`. Isolate the relevant content and perform the requested change on that isolated layer in this first call. Set `background: "transparent"`. The layer must contain the actual changed content or the foreground retained during a background replacement; an empty or unrelated transparent placeholder is invalid. Preserve backing shapes when necessary. Validate real alpha, exact text/content, visible bounds, geometry, and edges before proceeding. Alpha alone cannot detect painted checkerboards or missing foreground; use available visual inspection on a small preview, preferably against contrasting backgrounds. If validation fails or cannot be completed, retain the returned layer and explain what remains unverified; do not silently bypass this stage.
2. **Recompose and generate the final image.** Make a separate `generate_image` call with the ORIGINAL base, the verified stage-1 PNG, and needed replacement photographs as explicitly labeled inputs. Use the returned layer's real `media_id` or another supported reference in `input_images`; `"attached"` alone does not include the new layer. Specify replacement region, position, scale, crop, angle, and layering. Remove the old content completely and repair exposed areas after moving or shrinking it. Preserve the original canvas and unrelated content as closely as possible. Keep both outputs. This stage also uses `generate_image`, never local compositing.

| Requested edit | Stage 1: transparent PNG | Stage 2: final image |
| --- | --- | --- |
| Change A to B, including text or graphics | Isolate the affected graphic and render B in its requested style; retain a backing shape if needed to cover A cleanly. | Replace A with B, leaving no old letters or graphics underneath. |
| Enlarge/reduce A, move A, or reflow A's sentence | Isolate A and prepare its requested arrangement or line breaks on transparency; preserve exact wording. | Place the layer at the intended size/location, remove the former A, and repair exposed areas. |
| Replace image 1's background with image 2 | Extract retained foreground from image 1, including text, logos, brush strokes, and decorations; exclude the old photograph. | Use image 1 as the layout reference, image 2 as the new photographic base, and the PNG as retained foreground. |
| Replace the lower-right picture with image 3 | Prepare image 3 as a photo patch matching the target slot's crop, shape, and perspective, with transparency outside the patch. Keep its own photographic background unless a subject cutout was requested. | Replace only the specified slot; preserve surrounding frames, text, and other images. |

Equal canvas dimensions do not guarantee equal placement or scale: verify visible layer bounds and tell stage 2 how to fit a centered or enlarged cutout into its intended region. A rectangular photo patch may have transparent padding; do not erase its photographic content merely to create alpha. Multiple final variants may reuse one verified layer, with its link retained for each variant.

If native transparency fails, retain the actual output and report the incomplete edit. Do not conceal failure with chroma-keying, local background removal, model switching, HTTP generation, or a direct opaque edit. On timeout, inspect the existing result before any targeted retry.

## Two-image background replacement example

User request: 将图一的背景替换为图二

- Image 1 is the poster to edit: preserve its text, typography, brush strokes, decorative graphics, and overall arrangement.
- Image 2 is the replacement photographic background: use its photographed scene rather than reconstructing the old photograph from Image 1.
- Stage 1: use `input_images: "attached"` with `background: "transparent"`; extract Image 1's retained text and decorations into a real-alpha PNG, excluding its photographic base. Image 2 is the later replacement scene, not foreground to extract. Preserve the poster layout and aspect ratio.
- Stage 2, only after validation: pass explicit references to Image 1, the generated transparent layer, and Image 2. Generate the final poster with Image 2 as the entire photographic base and the retained layer placed according to Image 1. Do not send only the original attachments or a text description of the new layer.
- Deliver the transparent PNG and final poster separately. See the two-call examples in [tool interface](references/tool-interface.md).

## Optional prompt scaffolding

```text
Use case: <generation / background replacement / text edit / compositing>
Primary request: <user request>
Input images: <number, order, and role of each image>
Composition/framing: <preserve or requested change>
Text (verbatim): "<exact text when relevant>"
Constraints: <elements to preserve and only the requested changes>
Avoid: <unwanted changes>
```

This is optional guidance, not a required output schema.
