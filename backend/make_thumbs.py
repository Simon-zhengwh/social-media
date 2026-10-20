from pathlib import Path
from PIL import Image
root=Path(r'D:\ai_project\social-media\.agents\skills\seejoy-brand-poster\assets')
out=Path(r'D:\ai_project\social-media\storage\reference-thumbs')
out.mkdir(parents=True, exist_ok=True)
for p in sorted(root.glob('reference-*.jpg')):
    with Image.open(p) as im:
        im=im.convert('RGB')
        im.thumbnail((512,512), Image.Resampling.LANCZOS)
        dest=out/(p.stem+'.jpg')
        im.save(dest, quality=82, optimize=True)
        print(dest, dest.stat().st_size, im.size)
