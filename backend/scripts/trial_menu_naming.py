"""Read-only SMB pilot for menu naming; never writes source files or uploads to UAT.

Run from backend with --source, --menu, --merchant-id and a LOCAL --output root.
The report and original-byte copies stay local. No database records are changed.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import Settings
from app.services.core_ai import CoreAIClient, CoreAIError
from app.services import material_filter_files as files
from app.services import material_menu_naming as naming


def select_five(entries):
    """Three serial-number frames spread over the folder, two existing name hints."""
    serial = sorted((e for e in entries if re.fullmatch(r"_?DSC\d+\.jpe?g", e.name, re.I)), key=lambda e:e.name.casefold())
    chosen = [serial[i] for i in sorted({0, len(serial)//2, len(serial)-1})] if serial else []
    by_name = {e.name: e for e in entries}
    for hint in ["牛肚面.JPG", "鸭脖.JPG"]:
        if hint in by_name and by_name[hint] not in chosen:
            chosen.append(by_name[hint])
    for entry in entries:
        if len(chosen) >= 5:
            break
        if entry not in chosen:
            chosen.append(entry)
    return chosen[:5]


def select_five_recursive(entries):
    """Sample the five largest top-level sources, rather than adjacent frames."""
    groups = {}
    for entry in entries:
        relative = entry.relative_path.replace("\\", "/")
        group = relative.split("/", 1)[0] if "/" in relative else "."
        groups.setdefault(group, []).append(entry)
    chosen = []
    for _, items in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0].casefold()))[:5]:
        ordered = sorted(items, key=lambda entry: entry.relative_path.casefold())
        chosen.append(ordered[len(ordered)//2])
    for entry in entries:
        if len(chosen) >= 5:
            break
        if entry not in chosen:
            chosen.append(entry)
    return chosen


def render_report(report, previews):
    escape = lambda value: html.escape(str(value), quote=True)
    menu = {r["id"]: r for r in report["menu"]["items"]}
    labels = {"matched":"菜单匹配", "generated":"AI 自由命名", "ambiguous":"口味／菜名待确认", "error":"请求或校验失败"}
    cards = []
    for row in report["results"]:
        status = row["naming_status"]
        thumb = '<img alt="试跑照片缩略图" src="data:image/jpeg;base64,'+previews[row["photo_id"]]+'">' if row["photo_id"] in previews else ''
        title = row.get("filename") or row.get("visual_subject") or row["original_name"]
        candidates = ''.join('<li>'+escape((menu[i].get("zh") + ' / ' if menu[i].get("zh") else '')+menu[i]["en"])+ '</li>' for i in row.get("candidate_menu_ids",[]) if i in menu)
        candidates_html = '<div class="candidates"><b>候选菜单项</b><ul>'+candidates+'</ul></div>' if status=="ambiguous" else ''
        tags = ''.join('<span class="tag">'+escape(t)+'</span>' for t in row.get("tags",[]))
        local = '<p class="hint">本机副本：'+escape(row["copy_relative_path"])+ '</p>' if row.get("copy_relative_path") else '<p class="hint">尚未生成正式菜名副本，原图保留在 SMB。</p>'
        cards.append('<article data-status="'+escape(status)+'">'+thumb+'<div class="body"><span class="badge">'+labels[status]+'</span><h2>'+escape(title)+'</h2><p class="original">'+escape(row["original_name"])+'</p><div>'+tags+'</div><p>'+escape(row.get("reason",row.get("error","")))+'</p>'+candidates_html+local+'</div></article>')
    stats = ''.join('<span>'+label+' <b>'+str(report["counts"].get(key,0))+'</b></span>' for key,label in labels.items())
    title = escape(report['menu'].get('merchant_name') or '素材') + ' · 菜单命名 5 张试跑'
    language = '英文' if report['menu'].get('name_format') == 'en' else '中英文'
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+title+'''</title><style>
*{box-sizing:border-box}body{margin:0;padding:22px;font:14px/1.65 var(--font-sans,"Segoe UI"),sans-serif;background:var(--background,#fff);color:var(--foreground,#181818)}h1{margin:0;font-size:23px}header p,.hint,.original{color:var(--muted-foreground,#666)}header p{margin:5px 0}.stats,nav{display:flex;gap:9px;flex-wrap:wrap;margin:16px 0}.stats span{border:1px solid var(--border,#ddd);padding:7px 12px;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:16px}article{border:1px solid var(--border,#ddd);border-radius:12px;overflow:hidden;background:var(--card,#fff);color:var(--card-foreground,#181818)}article img{width:100%;height:225px;object-fit:contain;display:block;background:var(--muted,#f4f4f5)}.body{padding:15px}h2{font-size:16px;overflow-wrap:anywhere;line-height:1.5;margin:9px 0 3px}.original,.hint{font-size:12px;overflow-wrap:anywhere}.original{margin:4px 0 10px}.tag{display:inline-block;margin:0 5px 5px 0;padding:1px 7px;border-radius:5px;background:var(--secondary,#eee);color:var(--secondary-foreground,#333);font-size:12px}.badge{font-size:12px;font-weight:600;border:1px solid var(--border,#ddd);padding:2px 8px;border-radius:20px}.candidates{font-size:12px;border-top:1px solid var(--border,#ddd);padding-top:10px}.candidates ul{padding-left:18px}.hint{margin-bottom:0}button{font:inherit;cursor:pointer;border:1px solid var(--border,#ddd);border-radius:7px;padding:5px 11px;background:var(--secondary,#eee);color:var(--secondary-foreground,#222)}button:hover{background:var(--accent,#ddd);color:var(--accent-foreground,#222)}button.active{background:var(--primary,#18181b);color:var(--primary-foreground,#fff)}footer{font-size:12px;margin-top:18px;color:var(--muted-foreground,#666)}[hidden]{display:none!important}
</style></head><body><header><h1>'''+title+'''</h1><p>菜单原名优先；拼盘、多菜、菜单外图片自由生成'''+language+'''名称；同菜允许重名。</p><p>SMB 只读 · 本机保存原字节副本 · AI 初步结果，供人工挑选</p></header><div class="stats">'''+stats+'''</div><nav><button class="active" data-filter="all">全部 5</button><button data-filter="matched">菜单匹配</button><button data-filter="generated">自由命名</button><button data-filter="ambiguous">待确认</button></nav><main class="grid">'''+''.join(cards)+'''</main><footer>模型：'''+escape(report["model"])+'''。仅有本次 5 张请求，不代表整库准确率；本机正式副本均核验 SHA-256 与源图快照一致。此报告独立于网页素材筛选方案，未修改数据库或同步 UAT。</footer><script>document.querySelectorAll('button[data-filter]').forEach(b=>b.onclick=()=>{document.querySelectorAll('button[data-filter]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('article').forEach(x=>x.hidden=b.dataset.filter!=='all'&&b.dataset.filter!==x.dataset.status);});</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--menu", type=Path, required=True)
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recursive", action="store_true", help="遍历全部子目录，并跨主要目录抽取5张")
    parser.add_argument("--env-file", type=Path, default=BACKEND/".env.local")
    args = parser.parse_args()
    menu = naming.validate_menu(json.loads(args.menu.read_text(encoding="utf-8")), merchant_id=args.merchant_id)
    settings = Settings(_env_file=args.env_file)
    if not settings.core_ai_api_key:
        raise RuntimeError("CORE_AI_API_KEY 未配置")
    source = files.normalize_smb_source(args.source).unc
    entries = files.scan_source(source, recursive=args.recursive, max_files=settings.material_filter_max_files_per_run)
    selected = select_five_recursive(entries) if args.recursive else select_five(entries)
    if len(selected)!=5:
        raise RuntimeError("来源图片不足5张，本次不发起请求")
    output = files.validate_local_output_root(args.output)
    if output.exists():
        raise RuntimeError("试跑输出目录已存在，请使用新的本机目录，避免重复请求")
    output = files.ensure_local_directory(output)
    report = {"merchant_id":args.merchant_id,"source":source,"source_access":"read_only",
              "model":settings.material_filter_model,"created_at":datetime.now().isoformat(),"menu":menu,
              "sample_selection":"从5个主要一级目录分别取样；非随机抽样" if args.recursive else "3张序列号前中后样本，加牛肚面、鸭脖各1张；非随机抽样",
              "recursive":args.recursive,
              "source_image_count":len(entries),"selected_files":[e.relative_path for e in selected],
              "results":[],"counts":{}}
    (output/"menu.json").write_text(json.dumps(menu,ensure_ascii=False,indent=2),encoding="utf-8")
    (output/"prompt.txt").write_text(naming.build_messages(menu,"placeholder")[0]["content"],encoding="utf-8")
    print('SELECTED:',json.dumps(report['selected_files'],ensure_ascii=False),flush=True)
    previews={}
    stop=False
    with closing(CoreAIClient(settings)) as client:
        for index, entry in enumerate(selected,1):
            started=time.monotonic()
            row={"photo_id":f"photo_{index:02d}","original_name":entry.name,"relative_path":entry.relative_path,"naming_status":"error"}
            try:
                if stop:
                    raise RuntimeError("先前图片请求被拒绝，停止后续模型调用")
                snapshot=files.read_source_snapshot(source,entry,max_bytes=settings.material_filter_max_source_bytes)
                row.update(source_sha256=snapshot.sha256,source_size=snapshot.size,source_mtime_ns=snapshot.mtime_ns)
                files.write_local_preview(output,row['photo_id'],snapshot.data)
                previews[row['photo_id']]=base64.b64encode(files.build_preview_jpeg(snapshot.data,max_side=420)).decode('ascii')
                result=naming.classify_image(client,menu=menu,image_data_url=files.prepare_model_image_data_url(snapshot.data,max_side=settings.material_filter_model_image_max_side),original_name=entry.name,model=settings.material_filter_model,timeout_seconds=90)
                row.update(asdict(result))
                row['name']=result.name
                current=files.read_source_file(source,entry.relative_path,max_bytes=settings.material_filter_max_source_bytes)
                if current.sha256!=snapshot.sha256 or current.mtime_ns!=snapshot.mtime_ns:
                    raise RuntimeError("来源图片在判断期间改变，拒绝使用旧判断生成副本")
                if result.name:
                    copy=naming.write_named_copy(output/'named',row['photo_id'],result,suffix=Path(entry.name).suffix,data=snapshot.data,sha256=snapshot.sha256)
                    row.update(filename=copy.name,copy_relative_path=str(copy.relative_to(output)),copy_sha256=files.sha256_bytes(copy.read_bytes()))
            except Exception as exc:
                row.update(naming_status='error',error=str(exc)[:1200])
                if isinstance(exc,CoreAIError) and exc.status_code==400:
                    stop=True
            row['elapsed_seconds']=round(time.monotonic()-started,2)
            report['results'].append(row)
            report['counts']=dict(Counter(r['naming_status'] for r in report['results']))
            (output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(row,ensure_ascii=False),flush=True)
    page=render_report(report,previews)
    (output/'preview.html').write_text(page,encoding='utf-8')
    scratch=os.environ.get('ORCA_CHAT_SCRATCH_DIR')
    if scratch and len(page.encode('utf-8'))<512_000:
        name='menu-trial-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.html'
        target=files.validate_local_output_root(Path(scratch))/name
        target.write_text(page,encoding='utf-8')
        print('INLINE_VISUAL:',name,flush=True)
    print('REPORT_DIRECTORY:',output,flush=True)
    print('COUNTS:',json.dumps(report['counts'],ensure_ascii=False),flush=True)
    return 1 if report['counts'].get('error') else 0


if __name__=='__main__':
    raise SystemExit(main())
