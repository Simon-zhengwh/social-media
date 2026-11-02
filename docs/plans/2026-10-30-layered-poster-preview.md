# Layered poster preview implementation plan

**Goal:** Preview image 1's existing foreground over the exact original image 2, following the user's approved HTML/SVG layering approach.

**Architecture:** A local, self-contained HTML file embeds a two-image SVG. Both images share the original photo's 1536×2048 grid. The photo is embedded byte-for-byte; the previously validated transparent PNG is a separate foreground. No product source changes or new image-model requests are required.

**Tech stack:** Static HTML/SVG, browser Canvas PNG export, existing Python/Pillow for source verification only.

1. Create a uniquely named output directory containing a reproducible builder, HTML preview and standalone SVG.
2. Add an original/overlay toggle and downloads for full-size PNG and SVG.
3. Verify embedded source hashes and dimensions, test browser rendering and controls, and compare exported pixels outside the foreground against the original photo.
4. Open the local preview and deliver the files, identifying the reused AI-extracted foreground's limits.
