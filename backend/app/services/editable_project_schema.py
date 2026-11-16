"""Validation for the portable, network-free image-editor-v2 project format."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import math
import re

from fontTools.ttLib import TTFont
from PIL import Image, ImageOps


IMAGE_URL = re.compile(r'^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)$')
FONT_URL = re.compile(r'^data:font/(ttf|otf|woff|woff2);base64,([A-Za-z0-9+/=\r\n]+)$')
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unpack_url(value, pattern, label):
    match = pattern.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError(label + ': expected embedded data URL')
    try:
        return base64.b64decode(match[2], validate=False)
    except Exception as error:
        raise ValueError(label + ': invalid base64') from error


def raster(data):
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in ('PNG', 'JPEG', 'WEBP') or getattr(source, 'n_frames', 1) != 1:
                raise ValueError('Use static PNG, JPEG or WebP assets')
            source.load()
            return ImageOps.exif_transpose(source).copy(), Image.MIME[source.format]
    except (OSError, SyntaxError) as error:
        raise ValueError('Invalid raster asset') from error


def font_glyphs(data):
    try:
        with TTFont(BytesIO(data)) as font:
            return sorted((font.getBestCmap() or {}).keys())
    except Exception as error:
        raise ValueError('Invalid font file') from error


def normalize_font(data):
    """Store compressed webfonts as sfnt so browsers can inspect their real cmap."""
    if data[:4] not in (b'wOFF', b'wOF2'):
        return data
    try:
        with TTFont(BytesIO(data)) as font:
            font.flavor = None
            output = BytesIO(); font.save(output)
            return output.getvalue()
    except Exception as error:
        raise ValueError('Cannot normalize compressed font to sfnt') from error


def number(value, label, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid ' + label)
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError('Invalid ' + label)
    return value


def validate_project(project):
    if not isinstance(project, dict) or project.get('schema') != 'image-editor-v2':
        raise ValueError('Expected image-editor-v2 schema')
    for key in ('width', 'height'):
        if not isinstance(project.get(key), int) or isinstance(project[key], bool):
            raise ValueError('Invalid canvas ' + key)
        number(project[key], key, 1, 16384)
    if project['width'] * project['height'] > 100_000_000:
        raise ValueError('Canvas exceeds 100 megapixels')
    if not isinstance(project.get('title'), str):
        raise ValueError('Project title must be text')
    color = project.get('backgroundColor', '#ffffff')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('backgroundColor must be an opaque hex color')
    assets = project.get('assets')
    if not isinstance(assets, dict):
        raise ValueError('assets must be an object')
    decoded = {}
    for id, source in assets.items():
        if not isinstance(id, str) or not ID.fullmatch(id):
            raise ValueError('Invalid asset id')
        raw = unpack_url(source, IMAGE_URL, id)
        image, mime = raster(raw)
        if not source.startswith('data:' + mime + ';'):
            raise ValueError('Asset MIME does not match original bytes')
        decoded[id] = (raw, image)
    fonts = project.get('fonts')
    if not isinstance(fonts, list):
        raise ValueError('fonts must be an array')
    font_maps = {}
    for font in fonts:
        id = font.get('id')
        if not isinstance(id, str) or not ID.fullmatch(id) or id in font_maps:
            raise ValueError('Invalid or duplicate font id')
        if not isinstance(font.get('family'), str) or not font['family'].strip():
            raise ValueError('Font family is required')
        raw = unpack_url(font.get('source'), FONT_URL, 'font')
        if font.get('sha256') != digest(raw):
            raise ValueError('Font SHA256 mismatch')
        glyphs = font_glyphs(raw)
        if font.get('glyphs') != glyphs:
            raise ValueError('Font glyph metadata mismatch')
        font_maps[id] = set(glyphs)
    provenance = project.get('provenance')
    if not isinstance(provenance, dict):
        raise ValueError('provenance must be an assetId mapping')
    roles = set()
    for id, item in provenance.items():
        if id not in decoded or item.get('sha256') != digest(decoded[id][0]):
            raise ValueError('Provenance SHA256 mismatch')
        if item.get('role') not in ('photo', 'logo', 'background', 'graphic'):
            raise ValueError('Invalid provenance role')
        roles.add((id, item['role']))
    layers = project.get('layers')
    if not isinstance(layers, list) or not layers or len(layers) > 500:
        raise ValueError('Expected 1 to 500 layers')
    ids = set()
    for layer in layers:
        id = layer.get('id')
        if not isinstance(id, str) or not ID.fullmatch(id) or id in ids:
            raise ValueError('Layer ids must be unique')
        ids.add(id)
        if not isinstance(layer.get('name'), str) or not isinstance(layer.get('visible'), bool):
            raise ValueError('Layer needs name and boolean visible')
        type = layer.get('type')
        if type not in ('text', 'photo', 'image', 'logo', 'background'):
            raise ValueError('Invalid layer type')
        for key in ('x', 'y', 'rotation'):
            number(layer.get(key), key)
        for key in ('width', 'height', 'scale'):
            number(layer.get(key), key, 0.001, 100000)
        number(layer.get('opacity'), 'opacity', 0, 1)
        if type == 'text':
            if any(key in layer for key in ('assetId', 'source', 'replacementId', 'mode')):
                raise ValueError('Native text must not contain bitmap text or fallback modes')
            text = layer.get('text', {})
            if not isinstance(text.get('value'), str) or len(text['value']) > 10000:
                raise ValueError('Invalid native text value')
            glyphs = font_maps.get(text.get('font'))
            if glyphs is None:
                raise ValueError('Native text must use an embedded font')
            missing = sorted({ord(char) for char in text['value'] if char not in '\n\r\t' and ord(char) not in glyphs})
            if missing:
                raise ValueError('Missing font glyph: ' + ', '.join('U+%04X' % code for code in missing))
            for key in ('fontSize', 'width', 'lineHeight'):
                number(text.get(key), 'text.' + key, 0.001, 100000)
            number(text.get('strokeWidth', 0), 'text.strokeWidth', 0, 1000)
            for key in ('color', 'strokeColor'):
                if key in text and not re.fullmatch(r'#[0-9a-fA-F]{6}', text[key]):
                    raise ValueError('Invalid text ' + key)
        else:
            if any(key in layer for key in ('text', 'replacementId', 'mode')) or layer.get('containsText'):
                raise ValueError('Raster graphics must not substitute editable text')
            asset = layer.get('assetId')
            if asset not in decoded:
                raise ValueError('Missing layer assetId')
            role = 'graphic' if type == 'image' else type
            if (asset, role) not in roles:
                raise ValueError('Missing source provenance role')
            record = provenance[asset]
            if record.get('source') not in ('original', 'local', 'generated'):
                raise ValueError('Invalid provenance source')
            if 'generated' in layer and not isinstance(layer['generated'], bool):
                raise ValueError('generated must be boolean')
            generated = layer.get('generated', False) or record['source'] == 'generated'
            if generated:
                if type in ('photo', 'logo'):
                    raise ValueError('Original photo and official logo cannot be generated')
                if record['source'] != 'generated':
                    raise ValueError('generated flag and provenance source must agree')
                review = layer.get('review', record.get('review'))
                if not isinstance(review, dict) or review.get('status') != 'PASS' or review.get('image_sha256') != digest(decoded[asset][0]) or not isinstance(review.get('notes'), str) or not review['notes'].strip():
                    raise ValueError('Generated asset needs a current PASS review bound to image_sha256')
            if type == 'background':
                image = decoded[asset][1].convert('RGBA')
                if image.getchannel('A').getextrema() != (255, 255):
                    raise ValueError('Background must be fully opaque')
            if type == 'photo':
                number(layer.get('photoZoom', 1), 'photoZoom', 0.001, 100)
                for key in ('photoX', 'photoY'):
                    number(layer.get(key, 0), key)
                if 'clipPolygon' in layer:
                    points = layer['clipPolygon']
                    if not isinstance(points, list) or len(points) < 3:
                        raise ValueError('Invalid clipPolygon')
                    for point in points:
                        if not isinstance(point, list) or len(point) != 2:
                            raise ValueError('Invalid clipPolygon')
                        number(point[0], 'clipPolygon x', 0, layer['width'])
                        number(point[1], 'clipPolygon y', 0, layer['height'])
                    area = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points, points[1:]+points[:1]))
                    if abs(area) < 0.001:
                        raise ValueError('Empty clipPolygon')
    return project
