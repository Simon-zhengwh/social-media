"""Poster-series workbench browser check driven only by the Orca CLI browser.

The fixture page mounts the real studio with an in-page mocked API. The HTML editor used by
"保存到本页" is the *real* checked-in image-editor template (read-only reference), rendered
from a generated copy with fixture layer data, so the bridge has to cope with the template's
IIFE-private export and its normal <a download> path.

Usage:
    python tests/browser/posterSeries.py [http://127.0.0.1:5188]

Never touches port 5173/5174/8000/8011 and never calls a model.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parents[1]
REPO = FRONTEND.parent
TEMPLATE = REPO / 'core-ai-skill' / 'image-editor' / 'image-editor' / 'assets' / 'editor.html'
GENERATED = HERE / 'generated'
CANVAS = (1080, 1920)
OFFSET = (60, 40)


def png_data_uri(width: int, height: int, pixel) -> str:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixel(x, y))
    payload = zlib.compress(bytes(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))

    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
           + chunk(b'IDAT', payload) + chunk(b'IEND', b''))
    return 'data:image/png;base64,' + base64.b64encode(png).decode()


def build_editor(offset: tuple[int, int], path: Path) -> None:
    """Write the real template with fixture layers at the requested photo offset."""
    template = TEMPLATE.read_text(encoding='utf-8')
    assert '__EDITOR_DATA__' in template, 'real editor template changed'
    fixed = png_data_uri(CANVAS[0], CANVAS[1], lambda x, y: (0, 0, 0, 0))
    gate = png_data_uri(CANVAS[0], CANVAS[1], lambda x, y: (255, 255, 255, 255))
    photo = png_data_uri(240, 240, lambda x, y: (255, 0, 255, 255))
    data = {
        'width': CANVAS[0], 'height': CANVAS[1], 'title': '海报原照编辑器',
        'mode': 'photos', 'fixed': fixed, 'background': None,
        'layers': [{
            'id': 'photo1', 'kind': 'photo', 'label': '莓果杯',
            'source': photo, 'window': gate, 'baseScale': 1,
            'initial': {'x': CANVAS[0] // 2 + offset[0], 'y': CANVAS[1] // 2 + offset[1],
                        'scale': 1, 'rotation': 0, 'visible': True},
            'brightness': 0, 'contrast': 0,
        }],
    }
    path.write_text(template.replace('__EDITOR_DATA__', json.dumps(data, ensure_ascii=False)),
                    encoding='utf-8')


class Orca:
    """Thin wrapper over the Orca CLI browser commands for one tab."""

    def __init__(self, page: str | None = None) -> None:
        self.page = page

    def run(self, *args: str, timeout: int = 120) -> str:
        command = ['orca', *args]
        if self.page and args and args[0] not in {'tab', 'mouse'}:
            command.extend(['--page', self.page])
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                timeout=timeout, shell=False)
        if result.returncode != 0:
            raise RuntimeError(f'{command} failed: {result.stderr or result.stdout}')
        return result.stdout.strip()

    def mouse(self, *args: str, attempts: int = 12) -> str:
        """Mouse input is refused while the user is typing, so retry briefly."""
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self.run('mouse', *args)
            except RuntimeError as error:
                last = error
                if 'input method' not in str(error):
                    raise
                time.sleep(1.5)
        raise RuntimeError(f'mouse {args} still blocked: {last}')

    def focus(self) -> None:
        """Mouse commands act on the active tab only."""
        self.run('tab', 'switch', '--page', self.page)

    def screenshot(self) -> str:
        """Capture evidence; a screenshot failure must not fail the functional check."""
        try:
            self.focus()
            return self.run('screenshot')
        except RuntimeError as error:
            return f'(screenshot failed: {error})'

    def eval_json(self, expression: str):
        raw = self.run('eval', '--expression', expression)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # The CLI prints string results verbatim; JSON objects arrive as one line.
            if raw.startswith('{') or raw.startswith('['):
                match = re.search(r'(\{.*\}|\[.*\])$', raw, re.S)
                if match:
                    return json.loads(match.group(1))
            return raw

    def wait_for(self, expression: str, timeout: float = 30.0, interval: float = 0.4):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = self.eval_json(expression)
            if value:
                return value
            time.sleep(interval)
        raise RuntimeError(f'timed out waiting for {expression}')


CENTROID = """(() => {
  const t = window.editorTest;
  if (!t) return null;
  try {
    const canvas = t.makeCanvas();
    const ctx = canvas.getContext('2d');
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let count = 0, sumX = 0, sumY = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i] > 200 && data[i + 1] < 60 && data[i + 2] > 200) {
        const pixel = i / 4; count += 1;
        sumX += pixel % canvas.width; sumY += Math.floor(pixel / canvas.width);
      }
    }
    return JSON.stringify({ width: canvas.width, height: canvas.height, magenta: count,
      centerX: count ? Math.round(sumX / count * 10) / 10 : null,
      centerY: count ? Math.round(sumY / count * 10) / 10 : null,
      layer: { x: t.layers[0].x, y: t.layers[0].y } });
  } catch (error) { return JSON.stringify({ error: String(error && error.message || error) }); }
})()"""


def check_costs(studio: Orca) -> None:
    studio.eval_json("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '费用').click()")
    studio.wait_for("document.querySelector('.poster-cost-summary')?.textContent.includes('$0.3432')")
    assert studio.eval_json("document.querySelector('.poster-cost-summary').textContent.includes('总积分12')")
    assert studio.eval_json("!document.querySelector('.poster-cost-log')")
    studio.eval_json("[...document.querySelectorAll('button')].find(b => b.textContent.includes('查看调用明细')).click()")
    studio.wait_for("document.querySelector('.poster-cost-log')?.textContent.includes('Image Editor')")
    assert studio.eval_json("!document.querySelector('.poster-cost-log details').open")
    studio.eval_json("document.querySelector('.poster-cost-log summary').click()")
    studio.wait_for("document.querySelector('.poster-cost-log details[open]')?.textContent.includes('DeepSeek Flash')")
    scroll = studio.eval_json("""JSON.stringify((() => {
      const log = document.querySelector('.poster-cost-log');
      const summary = document.querySelector('.poster-cost-summary');
      const lastCall = log.querySelector('.poster-cost-children > li:last-child');
      const initialSummaryTop = summary.getBoundingClientRect().top;
      log.scrollTop = log.scrollHeight;
      const bottom = log.getBoundingClientRect().bottom;
      const last = lastCall.getBoundingClientRect();
      return {
        overflow: getComputedStyle(log).overflowY,
        scrolled: log.scrollTop > 0,
        lastInView: last.top >= log.getBoundingClientRect().top && last.bottom <= bottom,
        summaryStayed: summary.getBoundingClientRect().top === initialSummaryTop,
      };
    })())""")
    assert scroll == {"overflow": "auto", "scrolled": True, "lastInView": True, "summaryStayed": True}, scroll
    assert studio.eval_json("document.querySelector('.poster-cost-summary').textContent.includes('$0.3432')")
    for page in (2, 1):
        studio.eval_json(f"""document.querySelector('button[aria-label="查看 图片 {page}"]').click()""")
        studio.wait_for(f"new URL(location.href).searchParams.get('series_page') === 'p{page}'")
        assert studio.eval_json("document.querySelector('.poster-cost-summary').textContent.includes('$0.3432')")
    studio.eval_json("document.querySelector('button[aria-label=收起辅助工具]').click()")


def select_history(studio: Orca, group: str, label: str) -> None:
    studio.eval_json(f'''JSON.stringify((() => {{
      document.querySelector('.poster-history-trigger').click();
      const batch = [...document.querySelectorAll('.poster-history-batch')]
        .find(el => el.querySelector('h3').textContent.includes({json.dumps(group, ensure_ascii=False)}));
      const image = [...batch.querySelectorAll('.poster-history-image')]
        .find(el => el.lastElementChild.textContent === {json.dumps(label, ensure_ascii=False)});
      image.click();
      return !document.querySelector('.poster-history-dialog').open;
    }})())''')


def check(name: str, ok: bool, detail=None) -> None:
    if not ok:
        raise AssertionError(f'{name}: {json.dumps(detail, ensure_ascii=False)[:800]}')
    print(f'  ok  {name}' + (f'  {json.dumps(detail, ensure_ascii=False)}' if detail is not None else ''))


def wait_http(url: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise RuntimeError(f'{url} did not become reachable')


def start_vite(port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        ['npm', 'run', 'dev', '--', '--port', str(port), '--strictPort', '--host', '127.0.0.1'],
        cwd=str(FRONTEND), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        shell=sys.platform.startswith('win'),
    )
    wait_http(f'http://127.0.0.1:{port}/')
    return process


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('vite_url', nargs='?', default='http://127.0.0.1:5188')
    args = parser.parse_args()
    vite_url = args.vite_url.rstrip('/')
    port = int(vite_url.rsplit(':', 1)[1])
    assert port not in (5173, 5174, 8000, 8011), 'refusing the shared QA ports'

    GENERATED.mkdir(parents=True, exist_ok=True)
    build_editor((0, 0), GENERATED / 'editor-unmoved.html')
    build_editor(OFFSET, GENERATED / 'editor-moved.html')

    server = None
    try:
        urllib.request.urlopen(vite_url + '/', timeout=2)
    except (urllib.error.URLError, OSError):
        server = start_vite(port)

    tabs: list[str] = []

    def open_tab(url: str) -> Orca:
        payload = json.loads(subprocess.run(
            ['orca', 'tab', 'create', '--url', url, '--json'],
            capture_output=True, text=True, encoding='utf-8', check=True).stdout)
        page_id = (payload.get('result') or {}).get('browserPageId') or payload.get('pageId') or payload.get('id')
        assert page_id, payload
        tabs.append(page_id)
        browser = Orca(page_id)
        browser.wait_for('document.readyState === "complete"')
        return browser

    try:
        # 1. The real editor template: numeric crop movement in the actual checked-in file.
        print('real template: numeric photo movement')
        editor = open_tab(f'{vite_url}/tests/browser/generated/editor-unmoved.html?selftest')
        editor.wait_for('Boolean(window.editorTest && window.editorTest.state.ready)')
        before = editor.eval_json(CENTROID)
        check('real template decodes at the group canvas', before.get('width') == CANVAS[0]
              and before.get('height') == CANVAS[1], before)
        check('fixture photo is drawn', (before.get('magenta') or 0) > 50000, before)
        check('photo starts at the canvas centre',
              abs((before.get('centerX') or 0) - CANVAS[0] / 2) <= 1
              and abs((before.get('centerY') or 0) - CANVAS[1] / 2) <= 1, before)
        box = editor.eval_json("JSON.stringify((() => { const r = document.getElementById('stage').getBoundingClientRect();"
                              " return { left: r.left, top: r.top, width: r.width, height: r.height }; })())")
        start_x = box['left'] + box['width'] / 2
        start_y = box['top'] + box['height'] / 2
        step_x = OFFSET[0] * box['width'] / CANVAS[0]
        step_y = OFFSET[1] * box['height'] / CANVAS[1]
        editor.focus()
        editor.mouse('move', '--x', f'{start_x:.1f}', '--y', f'{start_y:.1f}')
        editor.mouse('down')
        editor.mouse('move', '--x', f'{start_x + step_x:.1f}', '--y', f'{start_y + step_y:.1f}')
        editor.mouse('move', '--x', f'{start_x + step_x:.1f}', '--y', f'{start_y + step_y:.1f}')
        editor.mouse('up')
        after = editor.eval_json(CENTROID)
        check('dragging one fixture photo moves its pixels by the requested offset',
              abs(after['centerX'] - before['centerX'] - OFFSET[0]) <= 2
              and abs(after['centerY'] - before['centerY'] - OFFSET[1]) <= 2,
              {'before': before, 'after': after})
        print('  screenshot:', editor.screenshot())

        # 2. The workbench: series mode, groups, pages, per-page actions, HTML save.
        print('workbench: series groups and pages')
        studio = open_tab(f'{vite_url}/tests/browser/posterSeries.html?merchant_id=m1&whole_workspace=ws1&series_group=g1&series_page=p1')
        studio.wait_for("document.querySelectorAll('.poster-history-batch').length === 3")
        check_costs(studio)
        state = studio.eval_json("""JSON.stringify({
          modeBadge: document.querySelector('.poster-mode-badge')?.textContent || '',
          modePressed: document.querySelector('.poster-mode-switch button[aria-pressed="true"]')?.textContent || '',
          modeDisabled: [...document.querySelectorAll('.poster-mode-switch button')].every(b => b.disabled),
          autoInteriorControls: document.querySelectorAll('.poster-auto-interiors, .poster-mode-row input[type="checkbox"]').length,
          bodyHasAutoInteriorCopy: document.body.innerText.includes('自动生成内页'),
          interiorCalls: (window.__seriesProbe?.interiors || []).length,
          historyTitles: [...document.querySelectorAll('.poster-history-batch h3')].map(el => el.textContent),
          pages: [...document.querySelectorAll('.poster-rail-item .candidate-thumbnail')].map(el => el.getAttribute('aria-label')),
          manualInteriors: [...document.querySelectorAll('button')].some(el => el.textContent.trim() === '生成内页'),
          zips: document.querySelectorAll('a[href*="/series-groups/"][href$="/download"]').length,
          retries: [...document.querySelectorAll('.poster-rail-retry')].map(el => el.textContent),
          countUnit: document.querySelector('.model-count-unit')?.textContent || '',
          countLabel: document.querySelector('.model-count-options')?.getAttribute('aria-label') || '',
          smallCopies: document.querySelectorAll('.model-toolbar small, .model-toolbar .poster-series-note').length,
          bodyHasPageHint: document.body.innerText.includes('页数按设计需求'),
          generateLabel: document.querySelector('.model-generate-button')?.getAttribute('aria-label') || '',
          generateText: document.querySelector('.model-generate-button')?.textContent || '',
          hintLines: document.querySelectorAll('.model-toolbar .model-generation-hint, .model-toolbar .model-generation-error').length,
          canvas: document.querySelector('.preview-frame img')?.getAttribute('src') || '',
          comparison: Boolean(document.querySelector('.comparison-board')),
          deleteButtons: document.querySelectorAll('.candidate-delete:not([hidden])').length,
        })""")
        check('the mode switch is locked to 系列图 after the first accepted submission',
              state['modePressed'] == '系列图' and state['modeDisabled'] is True, state)
        check('the toolbar title shows the task mode', state['modeBadge'] == '系列图', state)
        # The operator removed the automatic-interior checkbox: new rounds always build their pages.
        check('no auto-interior checkbox renders with the series controls',
              state['autoInteriorControls'] == 0 and state['bodyHasAutoInteriorCopy'] is False, state)
        check('model counts are group counts', state['countUnit'] == '组' and '组数' in state['countLabel'], state)
        # The user removed the small page-count sentence: no sub-copy under the controls.
        check('no small page-count copy renders under the series controls',
              state['bodyHasPageHint'] is False and state['smallCopies'] == 0,
              {'hint': state['bodyHasPageHint'], 'smallCopies': state['smallCopies']})
        # The submit verb is a plain 生成 in series mode, and nothing is printed under the count row.
        check('the series submit says 生成 and the controls stay',
              state['generateLabel'] == '生成' and state['generateText'] == '生成'
              and state['countUnit'] == '组' and state['countLabel'] == 'GPT Image 2.5 生成组数', state)
        check('no status line renders under the series count controls',
              state['hintLines'] == 0, state['hintLines'])
        check('history lists every group with its round and model',
              len(state['historyTitles']) == 3 and '第 1 轮' in state['historyTitles'][0]
              and any('Seedream 5.5' in title and title.endswith('组 2') for title in state['historyTitles']),
              state['historyTitles'])
        check('only the current group appears in the rail, in page order',
              state['pages'] == ['查看 图片 1', '查看 图片 2', '查看 图片 3 · 失败'], state['pages'])
        check('only failed pages show a retry control', state['retries'] == ['重试'], state['retries'])
        check('the workbench has no separate interior generation button', not state['manualInteriors'], state)
        check('opening history does not trigger old generation jobs', state['interiorCalls'] == 0, state['interiorCalls'])
        check('the workbench offers no group ZIP download', state['zips'] == 0, state['zips'])
        check('the selected cover preview is bound to its own page task',
              state['canvas'] == '/fixture/poster1.svg', state['canvas'])
        check('the finished image has no duplicate base/finished comparison', not state['comparison'], state)
        check('a series page never offers candidate deletion', state['deleteButtons'] == 0, state)

        print('workbench: page switching and versions')
        # One interaction through the accessibility tree, to prove the a11y path works.
        snapshot = studio.run('snapshot')
        ref = re.search(r'button "查看 图片 2"[^\n]*\[ref=(@?e\d+)\]', snapshot)
        check('the group/page buttons appear in the accessibility snapshot', ref is not None,
              snapshot[-1200:] if ref is None else None)
        studio.run('click', '--element', ref.group(1))
        switched = studio.wait_for("""(() => {
          const img = document.querySelector('.preview-frame img');
          if (!img || img.getAttribute('src') !== '/fixture/poster2.svg' || !document.querySelector('.poster-rail-version')) return null;
          return JSON.stringify({
            canvas: img.getAttribute('src'),
            url: location.search,
            versions: document.querySelector('.poster-rail-version')?.textContent,
            prompt: document.querySelector('.prompt-field textarea, textarea')?.value || '',
          });
        })()""", timeout=20)
        check('selecting a page rebinds the preview and the deep link',
              switched['canvas'] == '/fixture/poster2.svg' and 'series_page=p2' in switched['url'], switched)
        check('the page task exposes its versions for editing',
              switched['versions'] == 'v2⌄', switched)
        studio.eval_json("JSON.stringify(document.querySelector('.poster-rail-version').click() || true)")
        studio.wait_for("Boolean(document.querySelector('[aria-label=\"选择版本 v1\"]'))")
        studio.eval_json("JSON.stringify(document.querySelector('[aria-label=\"选择版本 v1\"]').click() || true)")
        picked = studio.wait_for('''(() => {
          const image = document.querySelector('.preview-frame img')?.getAttribute('src');
          if (image !== '/fixture/task-detail1-v1.svg') return null;
          return JSON.stringify({ download: document.querySelector('.chat-download')?.getAttribute('href'),
            page: new URLSearchParams(location.search).get('series_page') });
        })()''')
        check('the selected version changes the current page download',
              picked == {'download': '/fixture/task-detail1-v1.svg', 'page': 'p2'}, picked)
        studio.eval_json("JSON.stringify(document.querySelector('.poster-rail-version').click() || true)")
        studio.wait_for("Boolean(document.querySelector('[aria-label=\"选择版本 v2\"]'))")
        studio.eval_json("JSON.stringify(document.querySelector('[aria-label=\"选择版本 v2\"]').click() || true)")
        studio.wait_for("document.querySelector('.preview-frame img')?.getAttribute('src') === '/fixture/poster2.svg'")

        studio.run('reload')
        studio.wait_for("document.querySelectorAll('.poster-history-batch').length === 3", timeout=40)
        reloaded = studio.wait_for('''(() => {
          const img = document.querySelector('.preview-frame img');
          const active = document.querySelector('.poster-rail-item .candidate-thumbnail[aria-pressed="true"]');
          if (!img || img.getAttribute('src') !== '/fixture/poster2.svg' || !active) return null;
          return JSON.stringify({ selected: active.getAttribute('aria-label'), url: location.search });
        })()''', timeout=30)
        check('a refresh restores the workspace, group and page',
              reloaded['selected'] == '查看 图片 2' and 'series_page=p2' in reloaded['url'], reloaded)

        check('refresh restores all five submitted photos even when the current image used only one',
              studio.eval_json("JSON.stringify(document.querySelectorAll('button[aria-label^=\"移除图片\"]').length)") == 5)

        print('workbench: stale edit response guard')
        studio.wait_for("Boolean(document.querySelector('.chat-panel [role=\"textbox\"]'))", timeout=20)
        edit_snapshot = studio.run('snapshot')
        ref_input = re.search(r'textbox "修改要求"[^\n]*\[ref=(@?e\d+)\]', edit_snapshot)
        check('the chat edit box is reachable through the accessibility tree', ref_input is not None,
              edit_snapshot[-800:] if ref_input is None else None)
        studio.run('fill', '--element', ref_input.group(1), '--value', '把标题放大')
        send_snapshot = studio.run('snapshot')
        ref_send = re.search(r'button "发送修改"[^\n]*\[ref=(@?e\d+)\]', send_snapshot)
        check('the send action is reachable through the accessibility tree', ref_send is not None,
              send_snapshot[-800:] if ref_send is None else None)
        studio.run('click', '--element', ref_send.group(1))
        # Switch pages while the edit request is still in flight.
        select_history(studio, '组 2', '图片 1')
        edit_race = studio.wait_for('''(() => {
          const probe = window.__seriesProbe;
          const active = document.querySelector('.poster-rail-item .candidate-thumbnail[aria-pressed="true"]');
          const img = document.querySelector('.preview-frame img');
          if (!probe || !probe.edits.length || !active || !img) return null;
          return JSON.stringify({ edits: probe.edits.length, active: active.getAttribute('aria-label'),
            canvas: img.getAttribute('src'), url: location.search });
        })()''', timeout=60)
        check('a chat edit that lands after a page switch never replaces the opened page',
              edit_race['active'] == '查看 图片 1' and edit_race['canvas'] == '/fixture/poster3.svg'
              and 'series_page=p4' in edit_race['url'], edit_race)

        print('workbench: per-page retry')
        select_history(studio, '组 1', '图片 3')
        studio.eval_json("JSON.stringify(document.querySelector('.poster-rail-retry').click() || true)")
        retried = studio.wait_for("""(() => {
          const probe = window.__seriesProbe;
          if (!probe || !probe.retries.length) return null;
          return JSON.stringify({ retries: probe.retries,
            status: document.querySelector('.poster-rail-item .candidate-thumbnail[aria-pressed="true"]').getAttribute('aria-label') });
        })()""", timeout=20)
        check('retry targets exactly the failed page',
              retried['retries'] == ['g1:p3'] and '生成中' in retried['status'], retried)

        print('workbench: legacy group has no manual generation button')
        select_history(studio, '组 2', '图片 1')
        studio.wait_for("document.querySelector('.preview-frame img')?.getAttribute('src') === '/fixture/poster3.svg'")
        check('an older group also hides the separate interior action',
              studio.eval_json('''JSON.stringify(!(window.__seriesProbe?.interiors || []).length &&
                ![...document.querySelectorAll('button')].some(el => el.textContent.trim() === '生成内页'))'''))

        print('workbench: a series replacement never asks the Agent for the Logo')
        select_history(studio, '组 3', '图片 2')
        studio.wait_for("document.querySelector('.preview-frame img')?.getAttribute('src') === '/fixture/poster5.svg'", timeout=30)
        studio.eval_json("JSON.stringify((() => { const tab = [...document.querySelectorAll('.model-toolbar-tools button')].find(el => el.textContent.includes('原照替换')); if (tab) tab.click(); return Boolean(tab); })())")
        studio.wait_for("Boolean(document.querySelector('.original-replacement-panel'))", timeout=20)
        form = studio.wait_for("""(() => {
          const panel = document.querySelector('.original-replacement-panel');
          if (!panel) return null;
          const rows = panel.querySelectorAll('.original-replacement-row').length;
          const button = [...panel.querySelectorAll('button')]
            .find(el => el.textContent.trim() === '仅替换元素');
          if (!button || !rows) return null;
          return JSON.stringify({
            disabled: button.disabled,
            help: panel.querySelector('.replacement-help')?.textContent ?? null,
            buttons: [...panel.querySelectorAll('button')].map(el => el.textContent.trim()),
          });
        })()""", timeout=30)
        check('the series replacement form is ready to submit', form['disabled'] is False, form)
        studio.eval_json("""JSON.stringify((() => {
          const button = [...document.querySelectorAll('.original-replacement-panel button')]
            .find(el => el.textContent.trim() === '仅替换元素');
          button.click();
          return true;
        })())""")
        created = studio.wait_for("""(() => {
          const probe = window.__seriesProbe;
          if (!probe || !probe.replacementCreates.length) return null;
          return JSON.stringify(probe.replacementCreates[0]);
        })()""", timeout=60)
        check('a series replacement request never includes the official Logo',
              [item['target'] for item in created['replacements']] == ['千层']
              and all(item['asset_id'] != 'logo1' for item in created['replacements']), created)
        # Close the utility tab again so the page list and the later tab click behave.
        studio.eval_json("""JSON.stringify((() => {
          const tab = [...document.querySelectorAll('.model-toolbar-tools button')]
            .find(el => el.getAttribute('aria-expanded') === 'true');
          if (tab) tab.click();
          return Boolean(tab);
        })())""")
        studio.wait_for("document.querySelectorAll('.poster-rail-item').length > 0", timeout=30)

        print('workbench: replacement defaults and the real HTML save')
        select_history(studio, '组 1', '图片 2')
        studio.wait_for("document.querySelector('.preview-frame img')?.getAttribute('src') === '/fixture/poster2.svg'", timeout=20)
        studio.eval_json("JSON.stringify((() => { const tab = [...document.querySelectorAll('.model-toolbar-tools button')].find(el => el.textContent.includes('原照替换')); if (tab) tab.click(); return Boolean(tab); })())")
        studio.wait_for("Boolean(document.querySelector('.original-replacement-panel'))", timeout=20)
        record_view = studio.wait_for("""(() => {
          const panel = document.querySelector('.original-replacement-panel');
          if (!panel) return null;
          const buttons = [...panel.querySelectorAll('button')].map(el => el.textContent.trim());
          if (!buttons.includes('保存到本页')) return null;
          return JSON.stringify({
            buttons,
            openHtml: Boolean(panel.querySelector('a[href]')),
            targets: [...panel.querySelectorAll('.original-replacement-targets span')].map(el => el.textContent),
          });
        })()""", timeout=20)
        check('the saved replacement record offers the isolated editor save',
              record_view['buttons'].count('保存到本页') == 1, record_view)

        # A new replacement starts from the current version's content refs, not the pool.
        studio.eval_json('''JSON.stringify((() => {
          const select = document.querySelector('.original-replacement-history select');
          if (!select) return { switched: false };
          select.value = '';
          select.dispatchEvent(new Event('change', { bubbles: true }));
          return { switched: true };
        })())''')
        studio.wait_for("document.querySelectorAll('.original-replacement-row').length > 0", timeout=20)
        defaults = studio.eval_json('''JSON.stringify({
          targets: [...document.querySelectorAll('.original-replacement-row .replacement-target')].map(el => el.value),
          poolOptions: [...document.querySelectorAll('.original-replacement-pool option')].map(el => el.textContent),
        })''')
        check('replacement defaults come from the current version content refs',
              defaults['targets'] == ['莓果杯'], defaults)
        check('the pool offers other photos from the same task',
              any('photo' in str(option) for option in defaults['poolOptions']), defaults['poolOptions'])

        studio.wait_for('''(() => {
          const select = document.querySelector('.original-replacement-history select');
          if (!select) return null;
          select.value = 'rec1';
          select.dispatchEvent(new Event('change', { bubbles: true }));
          return 'record';
        })()''', timeout=20)
        studio.wait_for("""(() => {
          const button = [...document.querySelectorAll('.original-replacement-panel button')]
            .find(el => el.textContent.trim() === '保存到本页');
          if (!button) return null;
          button.click();
          return 'opened';
        })()""", timeout=20)
        studio.wait_for("Boolean(document.querySelector('.series-editor-frame'))", timeout=20)
        studio.eval_json("JSON.stringify((() => { const button = [...document.querySelectorAll('.series-editor-actions button')].find(el => el.textContent.includes('确认保存到本页')); if (button) { button.click(); return Boolean(button); } return false; })())")
        # Switch pages while the save request is still in flight, then let it land.
        select_history(studio, '组 2', '图片 1')
        saved = studio.wait_for("""(() => {
          const probe = window.__seriesProbe;
          if (!probe || !probe.saves.length) return null;
          return JSON.stringify(probe.saves[0]);
        })()""", timeout=90)
        check('the save request binds the page version, the artifact and the editor session',
              saved['source_version_id'] == 'ver-task-detail1-2' and saved['artifact_id'] == 'artifact-editor'
              and saved['session_id'], {k: v for k, v in saved.items() if k != 'png_base64'})
        check('the exported PNG is the real template canvas',
              saved['measured']['width'] == CANVAS[0] and saved['measured']['height'] == CANVAS[1],
              saved['measured'])
        # Stale-response guard: a page switched while the save is in flight keeps the new page.
        # Bind the newly opened page first, then let the late save response land.
        studio.wait_for("document.querySelector('.preview-frame img')?.getAttribute('src') === '/fixture/poster3.svg'", timeout=30)
        switched_during_save = studio.wait_for('''(() => {
          const probe = window.__seriesProbe;
          const active = document.querySelector('.poster-rail-item .candidate-thumbnail[aria-pressed="true"]');
          const img = document.querySelector('.preview-frame img');
          if (!probe || !probe.saves.length || !active || !img) return null;
          return JSON.stringify({
            active: active.getAttribute('aria-label'),
            canvas: img.getAttribute('src'),
            url: location.search,
          });
        })()''', timeout=60)
        check('a save that lands after a page switch never replaces the opened page',
              switched_during_save['active'] == '查看 图片 1'
              and switched_during_save['canvas'] == '/fixture/poster3.svg'
              and 'series_page=p4' in switched_during_save['url'], switched_during_save)
        check('the saved canvas carries the moved photo pixels exactly',
              abs(saved['measured']['centerX'] - (CANVAS[0] / 2 + OFFSET[0])) <= 2
              and abs(saved['measured']['centerY'] - (CANVAS[1] / 2 + OFFSET[1])) <= 2,
              {'measured': saved['measured'], 'expected': (CANVAS[0] / 2 + OFFSET[0], CANVAS[1] / 2 + OFFSET[1])})
        print('  screenshot:', studio.screenshot())
        print('poster series browser check passed')

        # 3. A fresh draft submits one round for every model the operator gave a group count.
        print('workbench: one submit covers every selected model')
        fresh_page = open_tab(f'{vite_url}/tests/browser/posterSeries.html?merchant_id=m1&scenario=fresh')
        fresh_page.wait_for("document.querySelectorAll('.poster-mode-switch button').length === 2")
        choice = fresh_page.eval_json('''JSON.stringify({
          single: document.querySelectorAll('.poster-mode-switch button')[0]?.getAttribute('aria-pressed'),
          series: document.querySelectorAll('.poster-mode-switch button')[1]?.getAttribute('aria-pressed'),
          seriesDisabled: document.querySelectorAll('.poster-mode-switch button')[1]?.disabled,
          unit: document.querySelector('.model-count-unit')?.textContent,
          groups: document.querySelectorAll('.poster-history-batch').length,
        })''')
        check('a new draft can still choose the series mode',
              choice['single'] == 'true' and choice['series'] == 'false'
              and choice['seriesDisabled'] is False and choice['groups'] == 0, choice)
        fresh_page.eval_json('''JSON.stringify((() => {
          const series = document.querySelectorAll('.poster-mode-switch button')[1];
          series.click();
          return series.getAttribute('aria-pressed');
        })())''')
        fresh_page.wait_for("document.querySelector('.model-count-unit')?.textContent === '组'", timeout=20)
        # Zero groups: the action stays disabled and prints no status line under the count row.
        empty = fresh_page.eval_json('''JSON.stringify({
          label: document.querySelector('.model-generate-button')?.getAttribute('aria-label'),
          text: document.querySelector('.model-generate-button')?.textContent,
          disabled: document.querySelector('.model-generate-button')?.disabled,
          hintLines: document.querySelectorAll('.model-toolbar .model-generation-hint').length,
          hintText: document.querySelector('.model-toolbar .model-generation-hint')?.textContent || '',
          autoInteriorControls: document.querySelectorAll('.poster-auto-interiors, .poster-mode-row input[type="checkbox"]').length,
        })''')
        check('an empty series round shows a disabled 生成 with no helper line',
              empty['label'] == '生成' and empty['text'] == '生成' and empty['disabled'] is True
              and empty['hintLines'] == 0 and '请选择' not in empty['hintText'], empty)
        check('the series mode row carries only the mode switch', empty['autoInteriorControls'] == 0, empty)
        configured = fresh_page.eval_json('''JSON.stringify((() => {
          const pill = [...document.querySelectorAll('.model-count-options button')]
            .find(el => el.getAttribute('aria-label') === 'GPT Image 2.5 2 组');
          if (!pill) return { clicked: false };
          pill.click();
          const seedream = [...document.querySelectorAll('.model-view-button')]
            .find(el => (el.getAttribute('aria-label') || '').includes('Seedream 5.5'));
          if (!seedream) return { clicked: false, seedream: false };
          seedream.click();
          return { clicked: true, seedream: true };
        })())''')
        check('each model keeps its own group count pills', configured['clicked'] and configured['seedream'], configured)
        fresh_page.eval_json('''JSON.stringify((() => {
          const pill = [...document.querySelectorAll('.model-count-options button')]
            .find(el => el.getAttribute('aria-label') === 'Seedream 5.5 1 组');
          if (!pill) return { clicked: false };
          pill.click();
          return { clicked: true };
        })())''')
        ready = fresh_page.eval_json('''JSON.stringify({
          label: document.querySelector('.model-generate-button')?.getAttribute('aria-label'),
          text: document.querySelector('.model-generate-button')?.textContent,
          disabled: document.querySelector('.model-generate-button')?.disabled,
          counts: [...document.querySelectorAll('.model-count-options button')]
            .filter(el => el.getAttribute('aria-checked') === 'true').map(el => el.getAttribute('aria-label')),
          bodyHasPageHint: document.body.innerText.includes('页数按设计需求'),
        })''')
        check('the series action states the verb only once a group count exists',
              ready['label'] == '生成' and ready['text'] == '生成'
              and ready['disabled'] is False, ready)
        check('a freshly switched series draft shows no page-count small copy',
              ready['bodyHasPageHint'] is False, ready['bodyHasPageHint'])
        fresh_page.eval_json("JSON.stringify((() => { document.querySelector('.model-generate-button').click(); return true; })())")
        submitted = fresh_page.wait_for('''(() => {
          const probe = window.__seriesProbe;
          if (!probe || !probe.runs.length) return null;
          return JSON.stringify(probe.runs[0]);
        })()''', timeout=40)
        check('one click submits every positive model count in a single round',
              submitted['image_groups'] == [
                {'image_model': 'gpt-image-2.5-sunburst', 'group_count': 2},
                {'image_model': 'seedream-5.5-image-to-image', 'group_count': 1},
              ], submitted['image_groups'])
        check('the round keeps the page count in the requirement and submits the entire group',
              'pages_per_group' not in submitted and 'auto_generate_interiors' not in submitted
              and submitted['reference_asset_ids'] == ['photo1'], {k: v for k, v in submitted.items() if k != 'prompt'})
        rendered = fresh_page.wait_for('''(() => {
          const groups = document.querySelectorAll('.poster-history-batch');
          if (groups.length !== 3) return null;
          return JSON.stringify({ groups: groups.length,
            rail: [...document.querySelectorAll('.poster-rail-scroll .candidate-thumbnail')].map(el => el.getAttribute('aria-label')),
            titles: [...document.querySelectorAll('.poster-history-batch h3')].map(el => el.textContent) });
        })()''', timeout=40)
        check('the submit response renders three independent groups',
              rendered['groups'] == 3 and rendered['titles'][1].endswith('组 2'), rendered['titles'])
        check('an in-flight group displays no predetermined image slots', rendered['rail'] == [], rendered['rail'])
        check('the pending group shows a loading preview',
              fresh_page.eval_json("JSON.stringify(Boolean(document.querySelector('.preview-loading')))"))
        fresh_page.eval_json("JSON.stringify(window.__seriesProbe.completeRound() || true)")
        fresh_page.wait_for("document.querySelectorAll('.poster-rail-label').length === 3")
        output = fresh_page.eval_json('''JSON.stringify({
          rail: [...document.querySelectorAll('.poster-rail-label')].map(el => el.textContent),
          groups: [...document.querySelectorAll('.poster-history-batch')].map(el => el.querySelectorAll('.poster-history-image').length),
        })''')
        check('the rail and history show the image counts returned by the Agent',
              output['rail'] == ['图片 1', '图片 2', '图片 3'] and output['groups'] == [3, 1, 1], output)
        print('  screenshot:', fresh_page.screenshot())

        print('workbench: stale mode follows the saved workspace')
        stale = open_tab(f'{vite_url}/tests/browser/posterSeries.html?merchant_id=m1&scenario=fresh&locked_elsewhere=1')
        stale.wait_for("document.querySelectorAll('.poster-mode-switch button').length === 2")
        stale.eval_json("JSON.stringify(document.querySelector('.poster-mode-switch button:last-child').click() || true)")
        stale.wait_for("document.querySelector('.model-count-unit')?.textContent === '组'")
        stale.eval_json("JSON.stringify(document.querySelector('.model-count-options button:nth-child(2)').click() || true)")
        stale.wait_for("document.querySelector('.model-generate-button')?.disabled === false")
        stale.eval_json("JSON.stringify(document.querySelector('.model-generate-button').click() || true)")
        conflict = stale.wait_for('''(() => {
          if (!window.__seriesProbe.runs.length || document.querySelector('.model-generate-button')?.disabled) return null;
          const buttons = [...document.querySelectorAll('.poster-mode-switch button')];
          return JSON.stringify({
            locked: buttons.every(button => button.disabled),
            mode: buttons.find(button => button.getAttribute('aria-pressed') === 'true')?.textContent,
            conflictAlert: document.body.innerText.includes('当前任务已锁定为另一种海报模式'),
          });
        })()''')
        check('a stale mode conflict selects the saved mode and disables the switch without a toast',
              conflict == {'locked': True, 'mode': '单张', 'conflictAlert': False}, conflict)

        print('workbench: single poster rounds use the same rail and dialog')
        single = open_tab(f'{vite_url}/tests/browser/posterSeries.html?merchant_id=m1&scenario=single&whole=single-old&whole_workspace=single-ws')
        single.wait_for("document.querySelector('.preview-frame img')?.getAttribute('src') === '/fixture/single-old.svg'")
        single.eval_json("JSON.stringify(document.querySelector('.model-count-options button:last-child').click() || true)")
        single.wait_for("document.querySelector('.model-generate-button')?.disabled === false")
        single.eval_json("JSON.stringify(document.querySelector('.model-generate-button').click() || true)")
        new_batch = single.wait_for('''(() => {
          if (document.querySelector('.preview-frame img')?.getAttribute('src') !== '/fixture/single-new-1.svg') return null;
          return JSON.stringify({ rail: [...document.querySelectorAll('.poster-rail-label')].map(el => el.textContent),
            rounds: [...document.querySelectorAll('.poster-history-batch h3')].map(el => el.textContent) });
        })()''')
        check('generating three single posters shows just the three new images in order',
              new_batch['rail'] == ['图片 1', '图片 2', '图片 3'], new_batch)
        check('single history lists older rounds and their own models',
              len(new_batch['rounds']) == 3 and '第 3 轮 GPT Image 2.5' in new_batch['rounds'][0]
              and 'Seedream 5.5' in new_batch['rounds'][-1], new_batch['rounds'])
        select_history(single, 'Seedream 5.5', '图片 1')
        single.wait_for("document.querySelector('.preview-frame img')?.getAttribute('src') === '/fixture/single-seed.svg'")
        check('selecting an older model round loads only its own thumbnails and download',
              single.eval_json('''JSON.stringify(!document.querySelector('.poster-history-dialog').open &&
                document.querySelectorAll('.poster-rail-item').length === 1 &&
                document.querySelector('.chat-download')?.getAttribute('href') === '/fixture/single-seed.svg')'''))

        # Feature off: the single-poster controls do not need the series routes.
        print('feature off: single poster parity')
        off = open_tab(f'{vite_url}/tests/browser/posterSeries.html?merchant_id=m1&series=off')
        off.wait_for("document.querySelectorAll('.model-toolbar').length === 1")
        parity = off.eval_json('''JSON.stringify({
          modeSwitch: document.querySelectorAll('.poster-mode-switch').length,
          seriesPanel: document.querySelectorAll('.poster-image-nav').length,
          unit: document.querySelector('.model-count-unit')?.textContent || '',
          countLabel: document.querySelector('.model-count-options')?.getAttribute('aria-label') || '',
          smallCopies: document.querySelectorAll('.model-toolbar small, .model-toolbar .poster-series-note').length,
          generate: document.querySelector('.model-generate-button')?.textContent || '',
          hint: document.querySelector('.model-generation-hint')?.textContent || '',
        })''')
        check('without the series config there is no mode switch or series panel',
              parity['modeSwitch'] == 0 and parity['seriesPanel'] == 0 and parity['smallCopies'] == 0, parity)
        check('the count control stays a poster count in single mode',
              parity['unit'] == '张' and parity['countLabel'] == 'GPT Image 2.5 生成张数', parity)
        check('the single-poster action is unchanged', parity['generate'] == '生成', parity)
        # Single mode still explains what is missing, so the series change did not mute it.
        check('single mode still explains its unmet conditions', parity['hint'] != '', parity)
        print('  screenshot:', off.screenshot())
        return 0
        return 0
    finally:
        for page_id in tabs:
            subprocess.run(['orca', 'tab', 'close', '--page', page_id], capture_output=True, text=True)
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == '__main__':
    sys.exit(main())
