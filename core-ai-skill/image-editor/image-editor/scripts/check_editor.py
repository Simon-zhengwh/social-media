"""Check the generated offline editor in a fresh local browser (optional Playwright)."""
import argparse
from io import BytesIO
import json
from pathlib import Path
import base64

from PIL import Image, ImageOps


def check_dishes(page,config,check,results,errors,requests):
    check('dish layers never use old-photo gates',page.evaluate("editorTest.layers.every(l=>l.kind==='dish'&&l.gate===null)"))
    check('all dishes default to cutouts with complete original alternatives',page.evaluate("editorTest.layers.every(l=>l.backgroundMode==='cutout'&&l.image===l.cutoutImage&&l.originalImage)"))
    for layer in config['layers']:
        page.select_option('#layer-select',layer['id'])
        before=page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.scale])')
        page.evaluate('editorTest.move(20,-12);editorTest.scaleAt(editorTest.layers[editorTest.state.selected].baseScale*1.2)')
        after=page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.scale])');index=page.evaluate('editorTest.state.selected')
        check(layer['id']+' independent free transform',after[index][0]==before[index][0]+20 and all(before[i]==after[i] for i in range(len(before)) if i!=index))
        page.locator('#rotation').evaluate("el=>{el.value=12;el.dispatchEvent(new Event('input',{bubbles:true}));}")
        check(layer['id']+' rotation',page.evaluate('editorTest.layers[editorTest.state.selected].rotation')==12)
        transforms=page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.scale,l.rotation,l.visible])')
        modes=page.evaluate('editorTest.layers.map(l=>l.backgroundMode)')
        page.select_option('#dish-background','original')
        check(layer['id']+' background switch keeps every transform',page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.scale,l.rotation,l.visible])')==transforms)
        after_modes=page.evaluate('editorTest.layers.map(l=>l.backgroundMode)')
        check(layer['id']+' original background choice is independent',after_modes[index]=='original' and all(after_modes[i]==modes[i] for i in range(len(modes)) if i!=index))
        check(layer['id']+' draws the full original',page.evaluate('()=>{const l=editorTest.layers[editorTest.state.selected];return l.image===l.originalImage;}'))
        hit_result=page.evaluate('''()=>{
            const l=editorTest.layers[editorTest.state.selected],w=l.image.naturalWidth,h=l.image.naturalHeight;
            let pixel=-1;for(let i=3;i<l.alpha.length;i+=4)if(l.alpha[i]<=20&&l.originalAlpha[i]>20){pixel=(i-3)/4;break;}
            if(pixel<0)return null;
            const visible=editorTest.layers.map(v=>v.visible);editorTest.layers.forEach(v=>v.visible=v===l);
            const dx=(pixel%w+.5-w/2)*l.scale,dy=(Math.floor(pixel/w)+.5-h/2)*l.scale,t=l.rotation*Math.PI/180;
            const x=l.x+dx*Math.cos(t)-dy*Math.sin(t),y=l.y+dx*Math.sin(t)+dy*Math.cos(t);
            const original=editorTest.hit(x,y);l.backgroundMode='cutout';l.image=l.cutoutImage;
            const cutout=editorTest.hit(x,y);l.backgroundMode='original';l.image=l.originalImage;
            editorTest.layers.forEach((v,i)=>v.visible=visible[i]);
            return original===editorTest.state.selected&&cutout!==editorTest.state.selected;
        }''')
        if hit_result is not None:check(layer['id']+' click area follows the selected background mode',hit_result)
        with page.expect_download() as item:page.click('#download-element')
        original=ImageOps.exif_transpose(Image.open(BytesIO(base64.b64decode(layer['originalSource'].split(',',1)[1])))).convert('RGBA')
        with Image.open(item.value.path()) as exported:
            check(layer['id']+' original-background PNG keeps full size and alpha',exported.size==original.size and exported.convert('RGBA').getchannel('A').tobytes()==original.getchannel('A').tobytes())
        expected=page.evaluate("editorTest.makeCanvas().toDataURL().split(',')[1]")
        with page.expect_download() as item:page.click('#download')
        with Image.open(item.value.path()) as exported:
            check(layer['id']+' composite export matches current background choice',exported.convert('RGBA').tobytes()==Image.open(BytesIO(base64.b64decode(expected))).convert('RGBA').tobytes())
        page.select_option('#dish-background','cutout')
        check(layer['id']+' returns to cutout without changing placement',page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.scale,l.rotation,l.visible])')==transforms and page.evaluate('()=>{const l=editorTest.layers[editorTest.state.selected];return l.image===l.cutoutImage;}'))
        page.click('#reset')
    page.select_option('#dish-background','original')
    count=len(config['layers']);page.click('#duplicate')
    check('duplicate adds an independent dish layer',page.evaluate('editorTest.layers.length')==count+1)
    check('duplicate inherits the current background mode',page.evaluate('editorTest.layers[editorTest.state.selected].backgroundMode')=='original')
    page.select_option('#dish-background','cutout')
    check('duplicate background mode changes independently',page.evaluate('editorTest.layers[editorTest.state.selected-1].backgroundMode')=='original')
    sample=base64.b64decode(config['layers'][0]['source'].split(',',1)[1])
    page.locator('#add-dish').set_input_files({'name':'dish.png','mimeType':'image/png','buffer':sample})
    page.wait_for_function('(n)=>editorTest.layers.length===n',arg=count+2)
    check('additional transparent dish imports',page.evaluate('editorTest.layers.length')==count+2)
    check('imported cutout has no invented original-background option',page.locator('#dish-background option[value="original"]').is_disabled() and page.evaluate('editorTest.layers[editorTest.state.selected].backgroundMode')=='cutout')
    marker=BytesIO();Image.new('RGB',(32,32),'orange').save(marker,format='PNG')
    page.locator('#add-dish').set_input_files({'name':'opaque.png','mimeType':'image/png','buffer':marker.getvalue()})
    page.wait_for_selector('#status.error')
    check('opaque upload is rejected as a dish cutout',page.evaluate('editorTest.layers.length')==count+2)
    def canvas():
        return page.evaluate("editorTest.makeCanvas().toDataURL().split(',')[1]")
    page.check('#hide-art');hidden=canvas()
    image=Image.open(BytesIO(base64.b64decode(hidden))).convert('RGBA')
    check('all dishes hidden leaves fully opaque background',image.getchannel('A').getextrema()==(255,255))
    page.uncheck('#hide-art');page.evaluate('editorTest.layers.forEach(l=>{l.x+=100000;l.y+=100000;});editorTest.move(0,0)')
    check('dishes move away without leaving holes or old dishes',canvas()==hidden)
    page.reload();page.wait_for_function('()=>editorTest.state.ready')
    with page.expect_download() as item:page.click('#download-element')
    with Image.open(item.value.path()) as sticker:check('dish-only export stays transparent','A' in sticker.getbands() and sticker.getchannel('A').getextrema()[0]==0)
    with page.expect_download() as item:page.click('#download')
    with Image.open(item.value.path()) as exported:
        check('composite PNG exact size and fully opaque',list(exported.size)==[config['width'],config['height']] and exported.getchannel('A').getextrema()==(255,255))
    page.set_viewport_size({'width':390,'height':844})
    check('mobile layout no overflow',page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth+1'))
    check('no script errors or external requests',not errors and not requests)
    return {'status':'PASS','scope':'functional_checks_only_not_visual_acceptance','checks':results,'count':len(results),'visual_review':'not_performed'}


def check_editor(html,browser_path=None):
    from playwright.sync_api import sync_playwright
    results=[]
    def check(name,ok):
        results.append({'name':name,'pass':bool(ok)})
        if not ok:raise AssertionError(name)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=str(browser_path) if browser_path else None)
        try:
            page=browser.new_page(viewport={'width':1280,'height':1000},accept_downloads=True)
            errors=[];requests=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:requests.append(r.url) if r.url.startswith(('http:','https:')) else None)
            page.goto(html.resolve().as_uri()+'?selftest=1')
            page.wait_for_function('()=>window.editorTest?.state.ready')
            config=page.evaluate("JSON.parse(document.getElementById('editor-data').textContent)")
            summary=[(l['id'],l['kind']) for l in config['layers']]
            check('all declared layers load',page.evaluate('editorTest.layers.length')==len(summary))
            check('canvas resolution',page.evaluate('[stage.width,stage.height]')==[config['width'],config['height']])
            if config['mode']=='dishes':return check_dishes(page,config,check,results,errors,requests)
            check('default photo workflow has no art controls',config['mode']!='photos' or (not page.locator('#mode-art').is_visible() and all(k=='photo' for _,k in summary)))
            for layer_id,kind in summary:
                page.select_option('#layer-select',layer_id)
                before=page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.scale])')
                page.evaluate('editorTest.move(9,7);editorTest.scaleAt(editorTest.layers[editorTest.state.selected].baseScale*1.2)')
                after=page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.scale])');index=page.evaluate('editorTest.state.selected')
                check(layer_id+' independent transforms',after[index][0]==before[index][0]+9 and all(before[i]==after[i] for i in range(len(summary)) if i!=index))
                if kind=='art':
                    page.locator('#rotation').evaluate("el=>{el.value=10;el.dispatchEvent(new Event('input',{bubbles:true}));}")
                    check(layer_id+' rotation',page.evaluate('editorTest.layers[editorTest.state.selected].rotation')==10)
                page.click('#reset')
            photo_id=next((i for i,k in summary if k=='photo'),None)
            if photo_id is not None:
                page.select_option('#layer-select',photo_id)
                dimensions=page.evaluate('()=>{const l=editorTest.layers[editorTest.state.selected];return [l.image.naturalWidth,l.image.naturalHeight];}')
                page.locator('#upload').set_input_files({'name':'broken.png','mimeType':'image/png','buffer':b'not an image'})
                page.wait_for_selector('#status.error')
                check('invalid upload preserves valid source',page.evaluate('()=>{const l=editorTest.layers[editorTest.state.selected];return [l.image.naturalWidth,l.image.naturalHeight];}')==dimensions)
                marker=BytesIO();Image.new('RGB',(64,48),'#d29020').save(marker,format='PNG')
                page.locator('#upload').set_input_files({'name':'replacement.png','mimeType':'image/png','buffer':marker.getvalue()})
                page.wait_for_function("()=>document.getElementById('file-name').textContent==='replacement.png'")
                check('local replacement loads',page.evaluate('editorTest.layers[editorTest.state.selected].image.naturalWidth')==64)
            else:
                check('element-only editor has no photo mode or upload',not page.locator('#mode-photo').is_visible() and not page.locator('#photo-upload').is_visible())
            page.reload();page.wait_for_function('()=>editorTest.state.ready')
            if config['mode']=='photos' and not config.get('background'):
                page.select_option('#view','fixed')
                expected=Image.open(BytesIO(base64.b64decode(config['fixed'].split(',',1)[1]))).convert('RGBA')
                with page.expect_download() as item:page.click('#download')
                with Image.open(item.value.path()) as image:
                    check('fixed-layer PNG preserves the supplied template alpha',image.convert('RGBA').getchannel('A').tobytes()==expected.getchannel('A').tobytes())
                page.select_option('#view','all')
            if config['mode']=='elements':
                page.check('#hide-art')
                def opaque():
                    return page.evaluate("()=>{const c=editorTest.makeCanvas(),a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<a.length;i+=4)if(a[i]!==255)return false;return true;}")
                check('all art hidden leaves a fully opaque canvas',opaque())
                with page.expect_download() as item:page.click('#download')
                file=item.value.path()
                with Image.open(file) as image:
                    check('actual PNG with hidden art is opaque','A' not in image.getbands() or image.getchannel('A').getextrema()==(255,255))
                page.evaluate("editorTest.layers.filter(l=>l.kind==='photo').forEach(l=>{l.x+=100000;l.y+=100000;});editorTest.move(0,0)")
                check('all photos moved away leaves opaque background',opaque())
                page.select_option('#view','fixed');check('fixed-only view is opaque',opaque())
                art_id=next(i for i,k in summary if k=='art');page.select_option('#layer-select',art_id)
                with page.expect_download() as item:page.click('#download-element')
                with Image.open(item.value.path()) as image:check('individual sticker remains transparent','A' in image.getbands() and image.getchannel('A').getextrema()[0]==0)
            with page.expect_download() as item:page.click('#download')
            with Image.open(item.value.path()) as image:check('actual download matches canvas size',list(image.size)==[config['width'],config['height']])
            page.set_viewport_size({'width':390,'height':844})
            check('mobile layout no overflow',page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth+1'))
            check('no script errors',not errors);check('no external network requests',not requests)
        finally:browser.close()
    return {'status':'PASS','scope':'functional_checks_only_not_template_acceptance','checks':results,'count':len(results),'browser':'isolated local headless Chromium','visual_review':'not_performed'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--browser',type=Path)
    args=parser.parse_args()
    try:report=check_editor(args.html,args.browser)
    except Exception as error:
        report={'status':'FAIL','error':str(error)}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['status']=='PASS' else 1)
