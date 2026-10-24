# Local presentation assets

Browser-loaded fonts and thumbnail images are committed with the site. Fonts
retain their upstream OFL licence files and source manifests alongside the WOFF2
files. Image snapshots retain source URLs in `assets/images/sources.json` where
applicable. A source URL records provenance; it does not grant additional rights.
Ordinary outbound links remain normal links.

Run `python3 check_assets.py` before deployment. Both manual and scheduled Pages
workflows include the asset directory and run this offline guard. Never add a
remote fallback, font preconnect, CDN runtime or externally hosted thumbnail.

`local_assets.py` downloads missing GitHub avatars during generation and reuses
committed PNGs. If downloading fails, it writes a local SVG placeholder. Stage
`assets/images` with regenerated `index.html` and `stats_history.json`.

Tailwind CSS is precompiled locally. After changing utility classes or the theme,
run `npx tailwindcss@3.4.17 -c tailwind.config.cjs -i tailwind.input.css -o assets/tailwind.css --minify`
and commit the CSS. Do not restore the Play CDN script.
