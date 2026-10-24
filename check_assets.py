"""Offline deployment guard: loaded HTML/CSS assets must exist locally."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote
import re
ROOT=Path(__file__).resolve().parent
class Assets(HTMLParser):
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('img','script','iframe','source','video','audio') and a.get('src'):check(a['src'],ROOT)
        if tag=='link' and set(a.get('rel','').split()) & {'stylesheet','icon','preload','preconnect','dns-prefetch'} and a.get('href'):check(a['href'],ROOT)
        if a.get('srcset'):
            for part in a['srcset'].split(','):check(part.strip().split()[0],ROOT)
def check(url,base):
    if url.startswith(('data:','#','blob:')) or unquote(url).startswith('#'):return
    parsed=urlsplit(url)
    if parsed.netloc or parsed.scheme:raise ValueError('External asset: '+url)
    file=(ROOT / unquote(parsed.path).lstrip('/')) if parsed.path.startswith('/') else base/unquote(parsed.path)
    if not file.is_file():raise ValueError('Missing asset: '+str(file))
def validate():
    text=(ROOT/'index.html').read_text();Assets().feed(text)
    for file in [ROOT/'index.html', *ROOT.glob('assets/**/*.css'), *ROOT.glob('site-assets/**/*.css')]:
        for url in re.findall(r'url\(\s*[\"\']?([^\)\"\']+)',file.read_text()):check(url.strip(),file.parent)
    print('Local assets verified')
if __name__=='__main__':validate()
