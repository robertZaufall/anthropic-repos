"""Build-time avatar cache. Visitors only load committed, same-origin assets."""
from pathlib import Path
from urllib.request import Request, urlopen
from html import escape
import re

ROOT = Path(__file__).resolve().parent

def local_avatar(owner):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}', owner):
        raise ValueError('Invalid GitHub owner')
    relative = 'assets/images/github-' + owner.lower()
    png = ROOT / (relative + '.png')
    svg = ROOT / (relative + '.svg')
    if png.exists():
        return relative + '.png'
    png.parent.mkdir(parents=True, exist_ok=True)
    try:
        request = Request('https://github.com/' + owner + '.png?size=32', headers={'User-Agent': 'RepositoryAtlas-build'})
        with urlopen(request, timeout=10) as response:
            image = response.read(1024 * 1024 + 1)
        if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 1024 * 1024:
            raise ValueError('Invalid avatar')
        temporary = png.with_suffix('.tmp')
        temporary.write_bytes(image)
        temporary.replace(png)
        return relative + '.png'
    except Exception:
        # A download outage never restores third-party browser requests.
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#334155"/><text x="16" y="22" text-anchor="middle" font-family="sans-serif" font-size="20" fill="white">' + escape(owner[0].upper()) + '</text></svg>')
        return relative + '.svg'
