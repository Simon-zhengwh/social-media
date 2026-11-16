"""Offline CPU photo segmentation, existing-alpha reuse and corrected-mask import."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from PIL import Image, ImageChops, ImageDraw, ImageOps


def resolve(base, value):
    path=Path(value).expanduser()
    return path if path.is_absolute() else Path(base)/path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_mask(path, size):
    with Image.open(path) as image:mask=image.convert('L')
    if mask.size!=size:raise ValueError('Mask must match the EXIF-oriented original image size')
    return mask


def integer(value, low, high, name):
    if type(value) is not int or not low<=value<=high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def segment_photo(spec, output_dir, base_dir=None):
    started=time.perf_counter()
    if isinstance(spec,(str,Path)):
        path=Path(spec);base_dir=path.parent
        spec=json.loads(path.read_text(encoding='utf-8-sig'))
    base_dir=Path(base_dir or '.');output_dir=Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError('Output directory is not empty; choose a new version directory')
    allowed={'image','box','foreground','background','seed_mask','corrected_mask',
             'source_sha256','iterations','max_size','point_radius','fill_holes','min_component_area'}
    if set(spec)-allowed:raise ValueError(f'Unknown segmentation fields: {sorted(set(spec)-allowed)}')
    iterations=integer(spec.get('iterations',5),1,10,'iterations')
    max_size=integer(spec.get('max_size',1024),64,4096,'max_size')
    radius=integer(spec.get('point_radius',8),1,100,'point_radius')
    fill_holes=spec.get('fill_holes',False)
    min_component_area=integer(spec.get('min_component_area',16),0,1000000,'min_component_area')
    removed_small_components=0
    if type(fill_holes) is not bool:raise ValueError('fill_holes must be a boolean')
    source_path=resolve(base_dir,spec['image']);source_hash=digest(source_path)
    if spec.get('source_sha256',source_hash)!=source_hash:
        raise ValueError('source_sha256 mismatch; do not reuse a mask on a different photo')
    with Image.open(source_path) as image:source=ImageOps.exif_transpose(image).convert('RGBA')
    original_alpha=source.getchannel('A');size=source.size
    box=spec.get('box')
    if box is not None and (len(box)!=4 or any(type(v) is not int for v in box) or not (
            0<=box[0]<box[2]<=size[0] and 0<=box[1]<box[3]<=size[1])):
        raise ValueError('box must be an integer [left,top,right,bottom] inside the oriented source')
    seeds=Image.new('L',size,128);draw=ImageDraw.Draw(seeds)
    for field,label in [('foreground',255),('background',0)]:
        for point in spec.get(field,[]):
            if len(point)!=2 or any(type(v) is not int for v in point) or not (
                    0<=point[0]<size[0] and 0<=point[1]<size[1]):
                raise ValueError(f'{field} points must be integer coordinates on the oriented source')
            x,y=point
            if field=='foreground' and box and not (box[0]<=x<box[2] and box[1]<=y<box[3]):
                raise ValueError('Foreground point is outside box; enlarge the box instead of clipping the subject')
            draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=label)
    if 'seed_mask' in spec:
        brush=read_mask(resolve(base_dir,spec['seed_mask']),size)
        histogram=brush.histogram()
        if sum(histogram[v] for v in (0,128,255))!=size[0]*size[1]:
            raise ValueError('seed_mask must use only 0=background, 128=unknown, 255=foreground')
        seeds=Image.composite(brush,seeds,brush.point(lambda v:0 if v==128 else 255))
    work_size=list(size)
    if 'corrected_mask' in spec:
        mask=read_mask(resolve(base_dir,spec['corrected_mask']),size)
        method='corrected_mask'
    elif original_alpha.getextrema()[0]<255 and box is None:
        mask=original_alpha.copy();method='existing_alpha'
    else:
        if box is None:raise ValueError('GrabCut requires box enclosing the complete dish; or supply corrected_mask')
        try:
            import cv2
            import numpy as np
        except ImportError as error:
            raise ValueError('CPU segmentation dependencies missing; preinstall scripts/requirements-mask.txt in the runtime') from error
        scale=min(1,max_size/max(size))
        work_size=[max(1,round(v*scale)) for v in size]
        image=source.convert('RGB').resize(tuple(work_size),Image.Resampling.LANCZOS)
        labels=np.zeros((work_size[1],work_size[0]),np.uint8)
        x0,y0,x1,y1=[round(v*scale) for v in box]
        if x1<=x0 or y1<=y0:raise ValueError('box vanished at working resolution; enlarge it or increase max_size')
        labels[y0:y1,x0:x1]=cv2.GC_PR_FGD
        hard=np.array(seeds.resize(tuple(work_size),Image.Resampling.NEAREST))
        inside=np.zeros(labels.shape,bool);inside[y0:y1,x0:x1]=True
        labels[(hard==255)&inside]=cv2.GC_FGD
        labels[hard==0]=cv2.GC_BGD
        if not np.any(labels==cv2.GC_BGD) or not np.any((labels==cv2.GC_FGD)|(labels==cv2.GC_PR_FGD)):
            raise ValueError('GrabCut needs both background and foreground samples')
        cv2.setNumThreads(4);cv2.setRNGSeed(17)
        try:
            cv2.grabCut(cv2.cvtColor(np.array(image),cv2.COLOR_RGB2BGR),labels,None,
                        np.zeros((1,65),np.float64),np.zeros((1,65),np.float64),iterations,cv2.GC_INIT_WITH_MASK)
        except cv2.error as error:
            raise ValueError('GrabCut failed; check box and foreground/background seeds') from error
        raw=np.where((labels==cv2.GC_FGD)|(labels==cv2.GC_PR_FGD),255,0).astype(np.uint8)
        if fill_holes:
            flood=np.pad(raw,1,constant_values=0);cv2.floodFill(flood,None,(0,0),255)
            holes=cv2.bitwise_not(flood)[1:-1,1:-1]
            holes[hard==0]=0
            raw=cv2.bitwise_or(raw,holes)
        # Remove detached tiny noise, but keep components explicitly marked as food.
        if min_component_area:
            n,component,stats,_=cv2.connectedComponentsWithStats((raw>0).astype(np.uint8))
            for label in range(1,n):
                selected=component==label
                if stats[label,cv2.CC_STAT_AREA]<min_component_area and not np.any((hard==255)&selected):
                    raw[selected]=0;removed_small_components+=1
        mask=Image.fromarray(raw).resize(size,Image.Resampling.NEAREST)
        # Honor explicit full-resolution brush edits even when a thin stroke was downsampled.
        keep=seeds.point(lambda v:255 if v==255 else 0)
        allowed_fg=Image.new('L',size);ImageDraw.Draw(allowed_fg).rectangle(
            (box[0],box[1],box[2]-1,box[3]-1),fill=255)
        mask=ImageChops.lighter(mask,ImageChops.multiply(keep,allowed_fg))
        mask=ImageChops.multiply(mask,seeds.point(lambda v:0 if v==0 else 255))
        method='grabcut_cpu'
    if method!='existing_alpha':mask=ImageChops.multiply(mask,original_alpha)
    bounds=mask.getbbox()
    if not bounds:raise ValueError('Empty subject mask; correct the selection instead of delivering an empty cutout')
    check_box=box if method=='grabcut_cpu' else [0,0,*size]
    contacts=[name for name,hit in zip(('left','top','right','bottom'),
              (bounds[0]<=check_box[0],bounds[1]<=check_box[1],bounds[2]>=check_box[2],bounds[3]>=check_box[3])) if hit]
    cutout=source.copy();cutout.putalpha(mask)
    report={'method':method,'source':str(source_path.resolve()),'source_sha256':source_hash,
            'source_size':list(size),'work_size':work_size,'box':box,'mask_bbox':list(bounds),
            'contacts_box_sides':contacts,'status':'needs_boundary_review' if contacts else 'candidate',
            'seconds':round(time.perf_counter()-started,3),'rgb_preserved':True,
            'visual_review':'pending','image_generation_calls':0,'network_calls':0,
            'removed_small_components':removed_small_components,'min_component_area_work_pixels':min_component_area,
            'limitations':'Candidate mask only. Box/frame contact needs review; original missing content cannot be restored.'}
    saved=dict(spec,image=str(source_path.resolve()),source_sha256=source_hash)
    for field in ('seed_mask','corrected_mask'):
        if field in saved:saved[field]=str(resolve(base_dir,saved[field]).resolve())
    output_dir.mkdir(parents=True,exist_ok=True)
    mask.save(output_dir/'mask.png');cutout.save(output_dir/'cutout.png')
    original=source.convert('RGB')
    overlay=Image.composite(Image.blend(original,Image.new('RGB',size,(20,150,240)),.42),original,mask)
    checker=Image.new('RGB',size,(180,180,180));checker.paste(cutout,mask=mask)
    preview=Image.new('RGB',(960,280),(245,245,245))
    for index,image in enumerate((original,overlay,checker)):
        image=image.copy();image.thumbnail((310,260),Image.Resampling.LANCZOS)
        preview.paste(image,(index*320+(320-image.width)//2,(280-image.height)//2))
    preview.save(output_dir/'preview.jpg',quality=88)
    for name,value in [('report.json',report),('segmentation.json',saved)]:
        (output_dir/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(segment_photo(args.spec,args.out),ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError) as error:
        print(f'{type(error).__name__}: {error}',file=sys.stderr);raise SystemExit(1)
