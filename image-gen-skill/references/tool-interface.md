# Core AI generate_image interface

Verified against the UAT built-in group schema on 2026-10-27:
`GET /api/tools/builtin/builtin:builtin-media-generation/tools`.
This is a maintainer reference; the runtime skill calls tools, not this HTTP route.

## Tool inputs

| Parameter | Usage |
| --- | --- |
| `prompt` | Required description including the requested change and invariants. |
| `model` | Default `gpt-image-2.5-sunburst`; editing requires image-to-image support. |
| `n` | Default 1. |
| `output_format` | Use `png`. |
| `size` | Optional supported dimensions; preserve the requested/source aspect ratio. |
| `quality` | Omit without a quality preference. gpt-image-2 uses low/medium/high/auto; gpt-image-2.5 additionally accepts xhigh/max at higher cost. Do not choose the latter without an explicit request. Never use standard/hd. |
| `input_images` | STRING: `attached`, or a JSON array serialized as a string. Omit for pure text-to-image. |
| `mask` | Optional STRING containing one reference object, not an array. Transparent mask pixels are repainted. Mask and every image must share pixel dimensions. |
| `background` | Stage 1 of EVERY edit requires `transparent`, with PNG. Verify real alpha separately before stage 2. For final images and new generation, follow the requested output background. |
| `provider_extra` | Provider-specific JSON string only for documented model parameters. Not a replacement for `input_images`. |
| `previous_interaction_id` | Gemini interaction continuation when explicitly returned and applicable. |

Do not set `output_compression` for PNG: the runtime schema describes it as JPEG compression 0–100. Keep `model_scope` omitted/once; a one-off task should not change the session default.

## Stage 1: current attachments to transparent layer

For a background replacement where Image 1 is the poster and Image 2 the replacement scene, first extract the retained foreground. All current attachments are available in their original order:

```json
{
  "prompt": "Stage 1 only: extract Image 1's exact text, backing brush strokes, and decorative graphics onto a real transparent PNG. Exclude its entire photographic background. Preserve text, colors, relative positions, scale, and canvas aspect ratio. Image 2 is the replacement background for the next stage; do not include its pixels in this layer. Keep visible foreground with actual zero-alpha pixels outside it; no painted checkerboard or opaque backdrop.",
  "model": "gpt-image-2.5-sunburst",
  "n": 1,
  "output_format": "png",
  "background": "transparent",
  "input_images": "attached"
}
```

`attached` means every image of the current message. Do not use it when extra irrelevant attachments are present. A local Windows path on the caller's computer is not a server-side attachment.

Inspect the returned PNG for real alpha AND correct foreground, wording, bounds, and edges. A successful response or PNG suffix does not prove transparency. Use available artifact inspection tools; do not assume `generate_image` returns alpha statistics or invent an inspection tool. If validation is unavailable or fails, report the incomplete workflow and stop before stage 2. Never perform local image removal/compositing to manufacture a passing result.

## Stage 2: explicit references including the generated layer

Supported array items include `media_id`, `sandbox_path`, external `url`, or `b64Json`. Prefer returned `media_id` for generated images and the platform's staged path for selected attachments. Never write image base64 by hand into model/tool arguments when platform references are available.

```json
{
  "prompt": "Stage 2: @poster is the ORIGINAL layout reference; @layer is the verified transparent foreground generated in stage 1; @scene supplies the entire new photographic background. Replace the old photographic base completely with @scene, fitting it to the original canvas. Place @layer at the positions and scale shown in @poster. Preserve its exact text, brush strokes, and decorations without duplicate lettering, old background remnants, or a checkerboard. Keep the original canvas and aspect ratio.",
  "model": "gpt-image-2.5-sunburst",
  "n": 1,
  "output_format": "png",
  "input_images": "[{\"sandbox_path\":\"<actual original poster attachment path>\",\"name\":\"poster\",\"role\":\"subject\"},{\"media_id\":\"<actual stage-1 returned media_id>\",\"name\":\"layer\",\"role\":\"subject\"},{\"sandbox_path\":\"<actual replacement scene attachment path>\",\"name\":\"scene\",\"role\":\"scene\"}]"
}
```

Replace placeholders only with real references supplied by the platform. Named references need a role sentence in the prompt. `input_images: "[\"last\"]"` can refer to the latest generated image in the same conversation; it is not a cross-session identifier.

Use an explicit reference for each original attachment and the stage-1 layer. `"attached"` alone includes only the current message attachments, not the generated layer. If the platform has not exposed a usable original reference, resolve it through available attachment tools or report the missing reference; never guess paths or proceed without the layer. Do not use a latest-image shortcut if it could select the wrong output.

For text replacement, resizing, or sentence reflow, stage 1 instead prepares the changed graphic on transparency, and stage 2 replaces its old region on the original base. For a photo-slot replacement, retain the replacement photograph inside its patch and make only the area outside the patch transparent. All editing examples follow the same two-stage sequence; the first call is not itself recursively split.

## Output and errors

Return BOTH tools' exact Markdown image links unchanged, labeled transparent PNG and final image. Keep returned `media_id` values and the layer-to-final relationship for subsequent edits; disclose warnings such as trimmed references. If a required reference was trimmed, do not claim the two-stage edit succeeded. Empty output or a tool failure is not success. Distinguish actual alpha verification and visual/text checks from tool execution success.

For timeouts, inspect the existing run/tool result first; do not automatically resubmit a potentially billable call. For malformed images or unsupported parameters, diagnose the stated cause before a targeted correction. Never fall back to HTTP generation, another model, or local pixel compositing without the user's direction.
