"""작업자 가이드(code/platform/worker-guide/README.md)를 UI 안에서 보여 줍니다.

문서는 GitHub에서도 그대로 읽히는 Markdown 한 벌만 관리하고, 여기서는 그 문서가 쓰는 문법
(제목·문단·목록·표·그림·인용·굵게·코드)만 HTML로 바꿉니다.
"""
import html
import re
from pathlib import Path

# UI 코드와 같은 폴더에 둡니다: 이미지에 함께 들어가고, 문서만 고쳐도 업데이트 범위가 'UI만'으로 끝납니다.
_DIR = Path(__file__).resolve().parent / 'worker-guide'
IMAGE_NAME = re.compile(r'^[A-Za-z0-9_-]+\.png$')

CSS = """
body{margin:0;background:#f5f6fb;color:#1f2033;font:16px/1.7 'Noto Sans KR','Malgun Gothic',sans-serif}
main{max-width:920px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:30px;margin:0 0 12px}h2{font-size:22px;margin:44px 0 12px;padding-top:20px;border-top:1px solid #dfe1ee}
h3{font-size:17px;margin:28px 0 8px}
img{max-width:100%;height:auto;border:1px solid #dfe1ee;border-radius:10px;margin:8px 0;background:#fff}
table{border-collapse:collapse;width:100%;margin:12px 0;background:#fff;font-size:15px}
th,td{border:1px solid #dfe1ee;padding:8px 10px;text-align:left;vertical-align:top}th{background:#eeeefa}
code{background:#e9e9f6;border-radius:4px;padding:1px 5px;font-size:.92em}
blockquote{margin:14px 0;padding:10px 14px;border-left:4px solid #6753d9;background:#efedfb;border-radius:0 8px 8px 0}
blockquote p{margin:0}hr{border:0;border-top:1px solid #dfe1ee;margin:36px 0}
li{margin:3px 0}a{color:#5a45cf}
h2{scroll-margin-top:12px}
/* 목차: 넓은 화면에서는 왼쪽에 고정, 좁은 화면에서는 문서 위에 */
.toc{background:#fff;border:1px solid #dfe1ee;border-radius:10px;padding:12px 14px;margin:0 0 20px;font-size:14px}
.toc b{display:block;margin-bottom:6px;color:#6a6d85;font-size:13px}.toc a{display:block;padding:3px 6px;border-radius:6px;text-decoration:none;color:#3d3f58}
.toc a:hover{background:#efedfb;color:#5a45cf}
@media(min-width:1280px){.toc{position:fixed;top:24px;left:max(16px,calc(50vw - 460px - 250px));width:220px;max-height:calc(100vh - 48px);overflow:auto;margin:0}main{margin-left:max(260px,calc(50vw - 460px))}}
@media print{.toc{display:none}}
"""


def guide_dir():
    return _DIR if (_DIR / 'README.md').is_file() else None


def _inline(text):
    out = html.escape(text, quote=False)
    out = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)\)', lambda m: f'<img src="{html.escape(m[2])}" alt="{html.escape(m[1])}">', out)
    out = re.sub(r'`([^`]+)`', r'<code>\1</code>', out)
    out = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', out)
    return out


def _cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def render(markdown):
    lines = markdown.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
        elif s == '---':
            out.append('<hr>')
            i += 1
        elif s.startswith('#'):
            level = len(s) - len(s.lstrip('#'))
            out.append(f'<h{level}>{_inline(s[level:].strip())}</h{level}>')
            i += 1
        elif re.fullmatch(r'<img [^<>]*>', s):
            # 문서에 직접 쓴 크기 지정 그림 태그만 그대로 통과시킵니다.
            out.append(f'<p>{s}</p>')
            i += 1
        elif s.startswith('|') and i + 1 < len(lines) and re.fullmatch(r'\|[\s:|-]+\|', lines[i + 1].strip()):
            head = _cells(s)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append(_cells(lines[i]))
                i += 1
            out.append('<table><thead><tr>' + ''.join(f'<th>{_inline(c)}</th>' for c in head) + '</tr></thead><tbody>'
                       + ''.join('<tr>' + ''.join(f'<td>{_inline(c)}</td>' for c in r) + '</tr>' for r in rows) + '</tbody></table>')
        elif re.match(r'(- |\d+\. )', s):
            ordered = not s.startswith('- ')
            pattern = r'\d+\. ' if ordered else r'- '
            items = []
            while i < len(lines) and re.match(pattern, lines[i].strip()):
                items.append(re.sub('^' + pattern, '', lines[i].strip()))
                i += 1
            tag = 'ol' if ordered else 'ul'
            out.append(f'<{tag}>' + ''.join(f'<li>{_inline(x)}</li>' for x in items) + f'</{tag}>')
        elif s.startswith('>'):
            quote = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append('<blockquote><p>' + _inline(' '.join(quote)) + '</p></blockquote>')
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r'(#|\||- |\d+\. |>|---$|<img )', lines[i].strip()):
                para.append(lines[i].strip())
                i += 1
            out.append('<p>' + '<br>'.join(_inline(x) for x in para) + '</p>')
    return '\n'.join(out)


def page():
    d = guide_dir()
    if not d:
        return None
    body = render((d / 'README.md').read_text(encoding='utf-8'))
    # 그림 주소(img/…)를 UI 경로로 바꿉니다.
    body = body.replace('src="img/', 'src="/guide/img/')
    # 목차: 큰 제목(##)마다 이동 링크를 만듭니다.
    titles = []

    def _anchor(m):
        titles.append(m[1])
        return f'<h2 id="s{len(titles)}">{m[1]}</h2>'
    body = re.sub(r'<h2>(.*?)</h2>', _anchor, body)
    toc = '<nav class="toc" aria-label="목차"><b>목차</b>' + ''.join(f'<a href="#s{n}">{t}</a>' for n, t in enumerate(titles, 1)) + '</nav>'
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>작업자 가이드</title><style>{CSS}</style></head><body>{toc}<main>{body}</main></body></html>')


def image(name):
    d = guide_dir()
    if not d or not IMAGE_NAME.match(name):
        return None
    p = d / 'img' / name
    return p if p.is_file() else None
