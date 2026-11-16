"""Small paginated review sheets for dish layout and restored-background checks."""
from PIL import Image, ImageDraw


def save_review_sheets(panels, output_dir, stem):
    names = []
    for start in range(0, len(panels), 9):
        page = panels[start:start + 9]
        sheet = Image.new('RGB', (960, ((len(page) + 2) // 3) * 300), '#eeeeee')
        draw = ImageDraw.Draw(sheet)
        for index, (label, image) in enumerate(page):
            x, y = index % 3 * 320, index // 3 * 300
            draw.text((x + 6, y + 4), label[:49], fill='black')
            draw.text((x + 6, y + 19), f'{image.width} x {image.height}', fill='#555555')
            thumb = image.convert('RGB')
            thumb.thumbnail((308, 258), Image.Resampling.LANCZOS)
            left, top = x + (320 - thumb.width) // 2, y + 37 + (258 - thumb.height) // 2
            sheet.paste(thumb, (left, top))
            if label.startswith(('Original poster', 'Source ')):
                for step in range(1, 4):
                    gx, gy = left + thumb.width * step // 4, top + thumb.height * step // 4
                    draw.line((gx, top, gx, top + thumb.height), fill='#55ccee')
                    draw.line((left, gy, left + thumb.width, gy), fill='#55ccee')
                    draw.text((gx + 2, top + 2), str(image.width * step // 4), fill='#ffcc44')
                    draw.text((left + 2, gy + 2), str(image.height * step // 4), fill='#ffcc44')
        name = f'{stem}-{start // 9 + 1:02d}.jpg'
        sheet.save(output_dir / name, quality=88)
        names.append(name)
    return names


def layout_overlay(poster, repair, protection):
    image = poster.convert('RGB')
    for mask, color in ((repair, '#ff5444'), (protection, '#00ccdd')):
        tint = Image.blend(image, Image.new('RGB', image.size, color), 0.5)
        image = Image.composite(tint, image, mask)
    return image


def background_panels(scene):
    poster, clean, repair = scene['poster'], scene['clean'], scene['repair']
    panels = [('Original poster', poster), ('Generated background', scene['background']),
              ('Final empty scene', clean),
              ('Red=repair Cyan=protected', layout_overlay(poster, repair, scene['protection']))]
    left, top, right, bottom = repair.getbbox()
    band = max(12, min(poster.size) // 20)
    crops = [('top', (left, top - band, right, top + band)),
             ('bottom', (left, bottom - band, right, bottom + band)),
             ('left', (left - band, top, left + band, bottom)),
             ('right', (right - band, top, right + band, bottom))]
    for side, bounds in crops:
        x1, y1, x2, y2 = bounds
        bounds = (max(0, x1), max(0, y1), min(poster.width, x2), min(poster.height, y2))
        pair = Image.new('RGB', (308, 250), '#dddddd')
        for index, image in enumerate((poster, clean)):
            crop = image.crop(bounds).convert('RGB')
            crop.thumbnail((150, 246), Image.Resampling.LANCZOS)
            pair.paste(crop, (index * 156 + (150 - crop.width) // 2, (250 - crop.height) // 2))
        panels.append((f'{side} old/new {bounds}', pair))
    return panels
