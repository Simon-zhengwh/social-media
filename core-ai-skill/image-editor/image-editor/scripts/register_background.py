"""Register a generated opaque background to unchanged original-poster regions."""
from pathlib import Path
import argparse
import json

from PIL import Image

from build_editor import image_info, sha


def register_background(poster_path,generated_path,exclude_path,output_dir):
    import cv2
    import numpy as np
    poster_path,generated_path,exclude_path=map(Path,(poster_path,generated_path,exclude_path))
    out=Path(output_dir)
    if out.exists() and any(out.iterdir()):raise FileExistsError('Use a new empty output directory')
    original,_=image_info(poster_path);generated,_=image_info(generated_path)
    if 'A' in generated.getbands() and generated.getchannel('A').getextrema()!=(255,255):raise ValueError('Generated background must be opaque')
    excluded=Image.open(exclude_path).convert('L')
    if excluded.size!=original.size:raise ValueError('Exclude mask must match original canvas')
    a=np.array(original.convert('RGB'));b=np.array(generated.convert('RGB'))
    excluded=np.array(excluded)>0
    if not excluded.any():raise ValueError('Exclude mask must independently identify the repair area')
    stable=np.where(excluded,0,255).astype(np.uint8)
    cv2.setRNGSeed(17);cv2.setNumThreads(4)
    detector=cv2.SIFT_create(nfeatures=8000)
    kb,db=detector.detectAndCompute(cv2.cvtColor(b,cv2.COLOR_RGB2GRAY),None)
    ka,da=detector.detectAndCompute(cv2.cvtColor(a,cv2.COLOR_RGB2GRAY),stable)
    if db is None or da is None:raise ValueError('No stable registration features; regenerate or provide explicit verified alignment')
    pairs=cv2.BFMatcher().knnMatch(db,da,k=2)
    matches=[row[0] for row in pairs if len(row)==2 and row[0].distance<.72*row[1].distance]
    if len(matches)<20:raise ValueError('Too few stable registration matches')
    pb=np.float32([kb[m.queryIdx].pt for m in matches]);pa=np.float32([ka[m.trainIdx].pt for m in matches])
    matrix,inliers=cv2.findHomography(pb,pa,cv2.RANSAC,3.0)
    if matrix is None or inliers is None or not np.isfinite(matrix).all():raise ValueError('Registration failed')
    selected=inliers.ravel().astype(bool)
    errors=np.linalg.norm(cv2.perspectiveTransform(pb[:,None,:],matrix)[:,0,:]-pa,axis=1)[selected]
    if len(errors)<20 or np.median(errors)>2 or np.percentile(errors,95)>3:raise ValueError('Registration residual error failed QA')
    w,h=original.size
    coverage=cv2.warpPerspective(np.full(b.shape[:2],255,np.uint8),matrix,(w,h),flags=cv2.INTER_NEAREST)
    if not np.all(coverage[excluded]==255):raise ValueError('Registered generated image does not cover the entire repair area')
    aligned=cv2.warpPerspective(b,matrix,(w,h),flags=cv2.INTER_LANCZOS4,borderMode=cv2.BORDER_CONSTANT)
    # Uncovered edges outside the actual repair area stay original, never black bars.
    aligned[coverage==0]=a[coverage==0]
    report={'status':'PASS_numeric_registration_only','poster_sha256':sha(poster_path),'generated_sha256':sha(generated_path),
            'method':'OpenCV SIFT + RANSAC on unchanged original regions','matrix':matrix.tolist(),'matches':len(matches),
            'inliers':int(selected.sum()),'median_error_px':float(np.median(errors)),'p95_error_px':float(np.percentile(errors,95)),
            'repair_coverage_complete':True,'visual_review':'not_performed','image_model_calls':0}
    out.mkdir(parents=True,exist_ok=True)
    Image.fromarray(aligned).save(out/'aligned-background.png');Image.fromarray(coverage).save(out/'coverage.png')
    (out/'registration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--poster',type=Path,required=True);parser.add_argument('--generated',type=Path,required=True)
    parser.add_argument('--exclude-mask',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(register_background(args.poster,args.generated,args.exclude_mask,args.out),ensure_ascii=False,indent=2))
