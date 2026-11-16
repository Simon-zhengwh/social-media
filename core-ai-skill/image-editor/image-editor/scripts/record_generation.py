"""Record an already completed image-tool call; never invokes a model or uploads files."""
import argparse
import json
from pathlib import Path

from build_editor import image_info, sha


def validate_record(record, image_path, purpose, references=()):
    if (record.get('status') != 'success' or record.get('purpose') != purpose
            or not isinstance(record.get('tool'), str) or not record['tool'].strip()
            or not isinstance(record.get('prompt'), str) or not record['prompt'].strip()
            or record.get('sha256') != sha(image_path)):
        raise ValueError('Missing or mismatched generation record for ' + purpose)
    reference_hashes = {r.get('sha256') for r in record.get('references', [])}
    if any(sha(Path(p)) not in reference_hashes for p in references):
        raise ValueError('Generation reference hashes do not match ' + purpose)


def record_generation(image, out, *, tool, purpose, prompt, references=(), review_out=None):
    image, out = Path(image).resolve(), Path(out).resolve()
    review_out = Path(review_out).resolve() if review_out else None
    references = [Path(p).resolve() for p in references]
    targets = [out] + ([review_out] if review_out else [])
    if len(set(targets)) != len(targets):
        raise ValueError('Record and review need separate output paths')
    for target in targets:
        if target.exists():
            raise FileExistsError('Use a new record/review path: ' + str(target))
    if not all(isinstance(v, str) and v.strip() for v in (tool, purpose, prompt)):
        raise ValueError('Record the actual tool, purpose and complete prompt')
    im, _ = image_info(image)
    record = {'tool': tool, 'purpose': purpose, 'status': 'success', 'prompt': prompt,
              'output': image.name, 'sha256': sha(image), 'size': list(im.size),
              'references': [{'file': p.name, 'sha256': sha(p)} for p in references]}
    if 'A' in im.getbands():
        record['alpha_extrema'] = list(im.getchannel('A').getextrema())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    if review_out:
        review_out.parent.mkdir(parents=True, exist_ok=True)
        review_out.write_text(json.dumps({'status': 'PENDING', 'image_sha256': record['sha256'],
                                          'notes': ''}, indent=2), encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--tool', required=True)
    parser.add_argument('--purpose', required=True)
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--reference', type=Path, action='append', default=[])
    parser.add_argument('--review-out', type=Path)
    args = parser.parse_args()
    print(json.dumps(record_generation(args.image, args.out, tool=args.tool, purpose=args.purpose,
                                      prompt=args.prompt_file.read_text(encoding='utf-8-sig'),
                                      references=args.reference, review_out=args.review_out),
                     ensure_ascii=False, indent=2))
