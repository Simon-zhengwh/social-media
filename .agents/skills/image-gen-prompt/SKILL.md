---
name: image-gen-prompt
description: Use when turning a user's visual-design request, subject images, factual images, brand guidance, and style references into one production-ready image-generation prompt. This skill writes prompts only; it does not generate or edit images.
---

# Image generation prompt editor

Create one clear prompt for a downstream image model. Follow the user's intent and the actual evidence in the supplied images. This skill adapts the input-role, composition, exact-text, and constraint principles of the imagegen prompting guide; it is self-contained for deployment on an agent platform.

## Interpret the inputs

Classify every supplied image before writing:

| Role | Use it for | Do not take from it |
| --- | --- | --- |
| Subject or product | Identity, pose, action, object details, packaging, food appearance | Unrelated promotional copy |
| Factual source | Legible event copy, prices, dates, address, terms, logos | A visual style unless requested |
| Style reference | Palette, typography feel, composition, lighting, texture, atmosphere | People, products, logos, dates, prices, or slogans |

The same image may have more than one role when the user explicitly says so. Use the user's role labels first; otherwise infer roles from the request and image content. If a critical role is genuinely ambiguous, ask one short question. Inspect supplied images with a vision-capable model; filenames and prior captions are insufficient evidence for exact text.

Treat the current user request as the source of this design's intent. Treat brand guidance as persistent constraints and style references as examples. When they conflict, follow the user's explicit instructions and flag any unresolved conflict that would change business facts. Never let old text visible in a style example become current campaign copy.

## Preserve facts

- Transcribe required names, prices, discounts, dates, addresses, reservation instructions, and legal terms from the user's request or legible factual sources. Preserve exact spelling, currency, numbers, and date-to-offer associations.
- Do not invent missing facts or turn an unreadable detail into a confident claim. Ask for clarification only when a missing or conflicting fact blocks a usable prompt; otherwise omit that detail.
- Keep distinct events and offers visibly distinct in the prompt. Identify which text belongs to which price, date, or audience.
- For a person or real product, specify what the image model should preserve. For a style reference, explicitly prohibit copying its business content and unrelated subjects.

## Write the prompt

Use only the sections that improve this asset; do not force a long template onto a simple request. A useful order is:

1. Intended asset and canvas or platform requirements.
2. Primary subject, scene, and the role of each input image by its supplied index.
3. Style, composition, visual hierarchy, lighting, palette, and material texture.
4. Exact on-image text in quotation marks, grouped by placement or offer.
5. Constraints: identity and product details to preserve, unsupported content to avoid, and any brand or platform restrictions.

If the user's request is detailed, organize it without adding creative requirements. If it is broad, add only practical composition and polish guidance. Give the most important subject, headline, and offer enough visual prominence for the intended display size. Use specific visual language rather than vague praise. Keep the brand guide's relevant rules, but do not paste a long guide back verbatim or repeat the same rule in several sections. Preserve the user's requested output language and quote exact Chinese or other in-image copy verbatim even when the surrounding prompt uses another language.

## Return and check

Return the finished prompt alone, ready to send with the original reference images to an image model. Do not call an image-generation tool or claim an image was generated. If the caller requests JSON, return exactly the requested schema with the finished prompt as its value.

Before returning, check that each image has the correct role; every required fact is supported; text, numbers, and offer dates are internally consistent; no content from a style-only reference has leaked into the campaign; and the prompt describes one feasible final asset rather than a collage of competing instructions.
