"""Package a native transparent poster template and complete sources as offline HTML."""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil

from PIL import Image, ImageChops, ImageDraw, ImageOps

# Match the existing opaque-separator cutoff; alpha 252/254 in retained graphics
# must not make the whole canvas a photo window, but translucent hole edges need backing.
SOURCE_ALPHA_LIMIT = 240

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_info(path):
    with Image.open(path) as im:
        if im.format not in ('PNG','JPEG','WEBP','BMP','GIF'):
            raise ValueError('Use a static raster source, not SVG or HTML')
        return ImageOps.exif_transpose(im).copy(), Image.MIME[im.format]


def data_url(path):
    _,mime=image_info(path)
    return 'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode('ascii')


def numbers(values,length,name):
    if not isinstance(values,list) or len(values)!=length or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in values):
        raise ValueError('Invalid '+name)
    return values


def box(values,width,height,name):
    l,t,r,b=numbers(values,4,name)
    if not 0<=l<r<=width or not 0<=t<b<=height:raise ValueError('Invalid '+name)
    return [l,t,r,b]


def region_mask(spec,size,base,name):
    if not isinstance(spec,dict) or ('mask' in spec)==('polygon' in spec):
        raise ValueError(name+': specify one mask or polygon')
    width,height=size
    if 'mask' in spec:
        raw,_=image_info((base/spec['mask']).resolve())
        if raw.size!=size:raise ValueError(name+': mask must match canvas_size')
        mask=raw.convert('L').point(lambda v:255 if v else 0)
    else:
        polygon=spec['polygon']
        if not isinstance(polygon,list) or len(polygon)<3:raise ValueError(name+': invalid polygon')
        for point in polygon:
            x,y=numbers(point,2,'polygon')
            if not 0<=x<=width or not 0<=y<=height:raise ValueError(name+': invalid polygon coordinates')
        mask=Image.new('L',size)
        ImageDraw.Draw(mask).polygon([tuple(p) for p in polygon],fill=255)
    if not mask.getbbox():raise ValueError(name+': empty region')
    return mask


def build_editor(manifest,output_dir,base_dir=None):
    if isinstance(manifest,(str,Path)):
        path=Path(manifest).resolve();base_dir=path.parent
        manifest=json.loads(path.read_text(encoding='utf-8-sig'))
    m=deepcopy(manifest);base=Path(base_dir or '.').resolve();out=Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):raise FileExistsError('Use a new empty output directory')
    resolve=lambda value:(base/value).resolve()
    mode=m.get('mode','photos')
    if mode not in ('photos','background','elements'):raise ValueError('mode must be photos, background or elements')
    slots=m.get('slots',[]);elements=m.get('elements',[])
    if not slots and mode!='elements':raise ValueError('At least one source-photo slot is required')
    if len(slots)>255:raise ValueError('Split editors with more than 255 photo windows')
    if mode!='elements' and elements:raise ValueError('Elements require explicit elements mode')
    if mode=='background' and len(slots)!=1:raise ValueError('Background mode requires one full-canvas slot')
    if mode=='elements' and (not isinstance(m.get('element_request'),str) or not m['element_request'].strip()):
        raise ValueError('Record the explicit user element_request')
    if mode=='elements' and not elements:raise ValueError('Elements mode needs selected element groups')
    if mode=='elements' and not m.get('background'):raise ValueError('Elements mode requires a complete opaque background')
    all_ids=[s.get('id','') for s in slots+elements]
    if len(set(all_ids))!=len(all_ids) or any(not re.fullmatch('[a-z][a-z0-9-]*',v) for v in all_ids):
        raise ValueError('Layer ids must be unique lowercase ASCII names')
    poster_path=resolve(m['poster']);poster,_=image_info(poster_path)
    width,height=numbers(m.get('canvas_size',list(poster.size)),2,'canvas_size')
    if any(not isinstance(v,int) or v<=0 for v in (width,height)):raise ValueError('Invalid canvas_size')
    if (width,height)!=poster.size:
        raise ValueError('Edits must keep the original poster canvas')
    native_path=resolve(m['template']);native,_=image_info(native_path)
    if 'A' not in native.getbands():raise ValueError('Template must have real alpha')
    if mode!='elements' and (native.getchannel('A').getextrema()[0]!=0 or not native.getchannel('A').getbbox()):
        raise ValueError('Template needs transparent photo pixels and visible fixed graphics')
    aspect_error=abs(native.width*height/(native.height*width)-1)
    if aspect_error>0.002:
        raise ValueError(f'Template aspect ratio differs from canvas by {aspect_error:.2%}; regenerate or use an explicitly registered template, never add contain margins')
    # Only permit tiny rounding differences, not a differently framed poster.
    mapped=native.convert('RGBA').resize((width,height),Image.Resampling.LANCZOS)
    offset=(0,0)
    template=mapped.copy()
    source_area=mapped.getchannel('A').point(lambda v:255 if v<SOURCE_ALPHA_LIMIT else 0)
    protected=Image.new('L',(width,height));protected_masks=[]
    for index,spec in enumerate(m.get('protected_regions',[])):
        mask=region_mask(spec,(width,height),base,f'protected_regions[{index}]')
        protected=ImageChops.lighter(protected,mask);protected_masks.append(mask)
    if m.get('background'):
        background,_=image_info(resolve(m['background']))
        if 'A' in background.getbands() and background.getchannel('A').getextrema()!=(255,255):
            raise ValueError('Background must be fully opaque')
    generation=m.get('generation',{})
    for key in ('new_calls','upstream_calls'):
        value=generation.get(key,0)
        if isinstance(value,bool) or not isinstance(value,int) or value<0:raise ValueError('Invalid generation call count')
    reports=[]
    for file in generation.get('reports',[]):
        receipt=json.loads(resolve(file).read_text(encoding='utf-8-sig'))
        reports.append((resolve(file),receipt))
    if mode=='background':
        if not any(r.get('status')=='success' and r.get('sha256')==sha(native_path)
                   and r.get('tool') and r.get('prompt') and r.get('purpose')=='background-foreground'
                   for _,r in reports):
            raise ValueError('Background requires a background-foreground generation record bound to the native PNG')
    labels=Image.new('L',(width,height));union=Image.new('L',(width,height))
    prepared=[]
    for index,slot in enumerate(slots,1):
        model_window=mode=='photos' and len(slots)==1 and 'mask' not in slot and 'polygon' not in slot
        mask=Image.new('L',(width,height),255) if model_window else region_mask(slot,(width,height),base,slot['id']+' window')
        if mode=='background' and mask.getextrema()!=(255,255):
            raise ValueError('Background mode requires a full-canvas window')
        polygon=slot.get('polygon')
        if 'photo_core' not in slot:raise ValueError(slot['id']+': independent photo_core is required')
        core=region_mask(slot['photo_core'],(width,height),base,slot['id']+' photo_core')
        if ImageChops.multiply(core,ImageChops.invert(mask)).getbbox():
            raise ValueError(slot['id']+': photo_core must be inside its window')
        if ImageChops.multiply(core,protected).getbbox():
            raise ValueError(slot['id']+': photo_core overlaps a protected graphic')
        if ImageChops.multiply(core,mapped.getchannel('A')).getextrema()[1]!=0:
            raise ValueError(slot['id']+': photo_core alpha is not exactly zero; template failed QA')
        overlap=ImageChops.multiply(mask,union)
        unprotected=source_area
        if ImageChops.multiply(overlap,unprotected).getbbox():raise ValueError('Photo windows overlap outside a fixed opaque separator')
        labels.paste(index,(0,0),mask);union=ImageChops.lighter(union,mask)
        source_path=resolve(slot['source']);source,_=image_info(source_path);sw,sh=source.size
        crop=box(slot.get('source_crop',[0,0,sw,sh]),sw,sh,'source_crop')
        default_bounds=source_area.getbbox() if model_window else mask.getbbox()
        bounds=box(slot.get('fit_box',list(default_bounds)),width,height,'fit_box')
        scale=max((bounds[2]-bounds[0])/(crop[2]-crop[0]),(bounds[3]-bounds[1])/(crop[3]-crop[1]))
        initial={'x':(bounds[0]+bounds[2])/2+((sw-crop[0]-crop[2])/2)*scale,
                 'y':(bounds[1]+bounds[3])/2+((sh-crop[1]-crop[3])/2)*scale,
                 'scale':scale,'baseScale':scale,'rotation':0,'visible':True}
        prepared.append({'slot':slot,'source':source_path,'index':index,'polygon':polygon,'bounds':bounds,'source_size':[sw,sh],'initial':initial,'photo_core':core})
    protection_report={'original_pixels_locked':False,'outside_window_changed_pixels':None,'protected_regions':len(protected_masks)}
    coverage_report=None
    if mode=='photos':
        # Source gates never edit the model layer or restore erased poster pixels.
        # A protected brush can have genuine partial alpha; only erased pixels are missing.
        erased=mapped.getchannel('A').point(lambda v:255 if v==0 else 0)
        if ImageChops.multiply(protected,erased).getbbox():
            raise ValueError('A protected graphic is missing from the model template; regenerate instead of restoring poster pixels')
        newly_cleared=ImageChops.multiply(source_area,poster.convert('RGBA').getchannel('A').point(lambda v:255 if v else 0))
        uncovered=ImageChops.multiply(newly_cleared,ImageChops.invert(union))
        uncovered_count=uncovered.histogram()[255]
        if uncovered_count:
            raise ValueError(f'Photo windows omit {uncovered_count} cleared template pixels; correct source routing or regenerate, never restore the original photo')
        coverage_report={'photo_area_pixels':newly_cleared.histogram()[255],'template_alpha_below':SOURCE_ALPHA_LIMIT,'uncovered_pixels':0}
        protection_report.update(fixed_layer_source='model_template',model_template_changed_pixels=0)
    elif protected_masks:
        if poster.size!=(width,height):raise ValueError('Protected original regions need original poster canvas')
        template=Image.composite(poster.convert('RGBA'),template,protected)
    art=[]
    for element in elements:
        path=resolve(element['source']);im,_=image_info(path)
        if ('A' in im.getbands() and not im.getchannel('A').getbbox()) or (element.get('role') != 'logo' and ('A' not in im.getbands() or im.getchannel('A').getextrema()[0]!=0)):
            raise ValueError('Each art element needs native visible RGBA content and transparency')
        b=box(element['box'],width,height,'element box')
        scale=min((b[2]-b[0])/im.width,(b[3]-b[1])/im.height)
        initial={'x':(b[0]+b[2])/2,'y':(b[1]+b[3])/2,'scale':scale,'baseScale':scale,'rotation':0,'visible':True}
        art.append((element,path,b,initial))
    # Finish validation before writing so a failed manifest cannot overwrite inputs.
    out.mkdir(parents=True,exist_ok=True);(out/'inputs').mkdir()
    def copy_input(path,name):
        target=out/'inputs'/(name+path.suffix.lower())
        shutil.copy2(path,target)
        return target.relative_to(out).as_posix()
    replay=deepcopy(m);replay['mode']=mode;replay['canvas_size']=[width,height];replay['slots']=deepcopy(slots)
    replay['poster']=copy_input(poster_path,'poster')
    replay['template']=copy_input(native_path,'native-template')
    template.save(out/'poster-template.png')
    mapped.save(out/'generated-mapped-template.png')
    for index,mask in enumerate(protected_masks):
        name=f'protected-{index}-mask.png';mask.save(out/name)
        replay['protected_regions'][index]={'id':m['protected_regions'][index].get('id',f'protected-{index}'),'mask':name}
    background_url=None
    if m.get('background'):
        replay['background']=copy_input(resolve(m['background']),'background')
        background_url=data_url(out/replay['background'])
    layers=[];source_records=[]
    for item,replay_slot in zip(prepared,replay['slots']):
        slot=item['slot'];source_name=copy_input(item['source'],slot['id'])
        # One integer ownership raster gives each shared frame edge exactly one owner.
        gate_mask=labels.point(lambda v:255 if v==item['index'] else 0).convert('L')
        gate=Image.new('RGBA',(width,height),'white');gate.putalpha(gate_mask)
        gate_name=slot['id']+'-window.png';gate.save(out/gate_name)
        mask_name=slot['id']+'-mask.png';gate_mask.save(out/mask_name)
        if item['polygon'] is None:replay_slot['mask']=mask_name
        replay_slot['source']=source_name;replay_slot['original_name']=slot.get('original_name',item['source'].name)
        replay_slot['fit_box']=item['bounds']
        core_name=slot['id']+'-photo-core.png';item['photo_core'].save(out/core_name)
        replay_slot['photo_core']={'mask':core_name}
        layers.append({'id':slot['id'],'label':str(slot.get('label',slot['id'])),'kind':'photo',
                       'source':data_url(out/source_name),'window':data_url(out/gate_name),'polygon':item['polygon'],
                       'source_size':item['source_size'],'bounds':item['bounds'],'fileName':replay_slot['original_name'],'initial':item['initial']})
        source_records.append({'id':slot['id'],'sha256':sha(item['source']),'source_pixels_cropped':0,'source_size':item['source_size']})
    for (element,path,b,initial),replay_art in zip(art,replay.get('elements',[])):
        name=copy_input(path,element['id']);replay_art['source']=name
        replay_art['original_name']=element.get('original_name',path.name)
        layers.append({'id':element['id'],'kind':'art','label':str(element.get('label',element['id'])),'source':data_url(out/name),
                       'text':str(element.get('text','')),'bounds':b,'fileName':replay_art['original_name'],'initial':initial,
                       **({'role':'logo'} if element.get('role') == 'logo' else {})})
    report_files=[]
    for index,(path,receipt) in enumerate(reports,1):
        name=f'generation-{index}.json';(out/name).write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');report_files.append(name)
    replay['generation']={'new_calls':0,'upstream_calls':generation.get('new_calls',0)+generation.get('upstream_calls',0),'reports':report_files}
    config={'width':width,'height':height,'title':str(m.get('title','海报原照编辑器')),'mode':mode,'background':background_url,
            'fixed':data_url(out/'poster-template.png'),'layers':layers}
    payload=json.dumps(config,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e')
    shell=(Path(__file__).resolve().parents[1]/'assets/editor.html').read_text(encoding='utf-8')
    (out/'editor.html').write_text(shell.replace('__EDITOR_DATA__',payload),encoding='utf-8')
    (out/'manifest.json').write_text(json.dumps(replay,ensure_ascii=False,indent=2),encoding='utf-8')
    report={'mode':mode,'canvas_size':[width,height],'photo_count':len(slots),'element_count':len(elements),'complete_background':bool(background_url),
            'sources':source_records,'template_mapping':{'native_size':list(native.size),'mapped_size':list(mapped.size),'offset':list(offset),'method':'full-canvas-aspect-checked','aspect_error':aspect_error},
            'protection':protection_report,'window_coverage':coverage_report,'photo_core_checks':[{'id':item['slot']['id'],'max_alpha':0,'pixels':item['photo_core'].histogram()[255]} for item in prepared],
            'html_build_network_calls':0,'html_build_model_calls':0,'declared_generation':generation,'visual_review':'not_performed','browser_review':'not_performed',
            'html_sha256':sha(out/'editor.html')}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build_editor(args.manifest,args.out),ensure_ascii=False,indent=2))
