"""Save an actual Core AI generate_image result; optionally download its native artifact.

This script never calls a generation API, SDK or agent. The parent agent calls the
platform's native tool and supplies its verbatim result and actual input files.
"""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from urllib.request import urlopen

from PIL import Image

from record_generation import record_generation

REQUIRED_QUALITY = 'xhigh'
REQUIRED_MODEL = 'gpt-image-2.5-sunburst'


def parse_tool_result(tool_result):
    links = re.findall(r'^!\[[^\]\r\n]*\]\((.+)\)\s*$', tool_result, re.MULTILINE)
    if ('data:image/' in tool_result or not tool_result.lstrip().startswith('Image generated.')
            or len(links) != 1):
        raise ValueError('Need the verbatim successful generate_image result with one native image link, no base64')
    parsed = urlparse(links[0])
    if parsed.scheme not in ('http', 'https', 'sandbox') or parsed.username or parsed.password:
        raise ValueError('Unsupported native artifact link; use the actual platform result')
    media = re.search(r'^media_id:\s*(\S+)\s*$', tool_result, re.MULTILINE)
    return links[0], media.group(1) if media else None


def download_native(tool_result, destination):
    link, _ = parse_tool_result(tool_result)
    parsed = urlparse(link)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Download needs an HTTP(S) native artifact; use --image for an existing sandbox file')
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('Do not overwrite an existing native output')
    with urlopen(link, timeout=60) as response:
        data = response.read(128 * 1024 * 1024 + 1)
    if len(data) > 128 * 1024 * 1024:
        raise ValueError('Native artifact exceeds 128 MiB')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(data)


def record_core_ai_generation(image, out, *, tool_result, purpose, prompt, model,
                              background, quality=REQUIRED_QUALITY, references=(), review_out=None):
    link, media_id = parse_tool_result(tool_result)
    if quality != REQUIRED_QUALITY:
        raise ValueError('Generation quality must be '+REQUIRED_QUALITY+'; retain the actual lower-quality result as rejected evidence, never relabel it')
    if model != REQUIRED_MODEL:
        raise ValueError('Generation model must be '+REQUIRED_MODEL+'; retain a different-model result as rejected evidence, never relabel it')
    if background not in ('transparent', 'opaque', 'auto'):
        raise ValueError('Record the actual background argument or auto when omitted')
    if purpose in ('photo-template', 'background-foreground', 'text-sticker', 'element') and background != 'transparent':
        raise ValueError('This purpose requires a native transparent tool request')
    if purpose in ('dish-background', 'opaque-background', 'text-final') and background == 'transparent':
        raise ValueError('This purpose needs an opaque native output')
    image = Path(image)
    with Image.open(image) as native:
        if native.format != 'PNG':
            raise ValueError('Use the native PNG; never transcode to conceal a format failure')
        alpha = native.convert('RGBA').getchannel('A')
        lo, hi = alpha.getextrema()
        if background == 'transparent' and ('A' not in native.getbands() or lo != 0 or hi == 0):
            raise ValueError('Native transparency failed; retain evidence and stop this branch')
        if (background == 'opaque' or purpose in ('dish-background', 'opaque-background', 'text-final')) and (lo, hi) != (255, 255):
            raise ValueError('Native opaque background/final image has alpha holes')
    record = record_generation(image, out, tool='generate_image', purpose=purpose,
                               prompt=prompt, references=references, review_out=review_out)
    record.update(transport='core_ai_native_tool', model=model, quality=quality,
                  background=background, tool_result=tool_result, result_link=link,
                  tool_call_attempts=1, visual_review='pending')
    if media_id:
        record['media_id'] = media_id
    Path(out).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--tool-result', type=Path, required=True)
    parser.add_argument('--purpose', required=True)
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--model', choices=[REQUIRED_MODEL], required=True)
    parser.add_argument('--quality', choices=[REQUIRED_QUALITY], default=REQUIRED_QUALITY)
    parser.add_argument('--background', choices=['transparent', 'opaque', 'auto'], required=True)
    parser.add_argument('--reference', type=Path, action='append', default=[])
    parser.add_argument('--review-out', type=Path)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    try:
        if args.out.exists() or (args.review_out and args.review_out.exists()):
            raise FileExistsError('Use new receipt/review paths')
        result = args.tool_result.read_text(encoding='utf-8-sig')
        if args.download:
            protected = [args.out, args.tool_result, args.prompt_file, *args.reference]
            if args.review_out:
                protected.append(args.review_out)
            if args.image.resolve() in {p.resolve() for p in protected}:
                raise ValueError('Download destination must not be a record, review or input reference')
            download_native(result, args.image)
        record = record_core_ai_generation(args.image, args.out, tool_result=result,
            purpose=args.purpose, prompt=args.prompt_file.read_text(encoding='utf-8-sig'),
            model=args.model, quality=args.quality, background=args.background,
            references=args.reference, review_out=args.review_out)
        print(json.dumps({k: v for k, v in record.items() if k not in ('tool_result', 'result_link')},
                         ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
