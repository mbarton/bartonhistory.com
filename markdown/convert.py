"""Convert the Wikispaces HTML export to Markdown, logging anything that doesn't map cleanly.

Usage, from the repository root:

    pip install beautifulsoup4 markdownify
    python3 markdown/convert.py .

Writes one .md per top-level .html page into markdown/ (overwriting existing ones) and
the list of things needing review into markdown/CONVERSION-REVIEW.md.
"""
import re
import sys
import html
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, quote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter

SRC = Path(sys.argv[1])
OUT = SRC / "markdown"

# category -> list of (file, detail)
issues = defaultdict(list)
existing_pages = {p.stem for p in SRC.glob("*.html")}
existing_files = {p.name for p in (SRC / "files").iterdir()} if (SRC / "files").exists() else set()


def build_anchor_maps():
    maps = {}
    for f in SRC.glob("*.html"):
        soup = BeautifulSoup(f.read_text(encoding="utf-8"), "html.parser")
        m = {}
        for a in soup.find_all("a", attrs={"name": True}):
            h = a.find_parent(re.compile(r"^h[1-6]$"))
            if h and not a.get("href"):
                m[a["name"]] = gh_slug(h.get_text())
        maps[f.stem] = m
    return maps


def log(cat, fname, detail):
    if cat.startswith("Broken") and (fname, detail) in issues[cat]:
        return
    issues[cat].append((fname, detail))


def snippet(tag, n=80):
    t = " ".join(tag.get_text(" ").replace("\x00", "").split())
    return (t[:n] + "…") if len(t) > n else t


def gh_slug(text):
    s = text.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def md_page_href(name):
    return quote(name + ".md")


def rewrite_internal(href, fname):
    """Map an internal wiki link to a relative .md link. Returns new href or None if external."""
    if not href or re.match(r"^[a-z]+:", href, re.I) or href.startswith("//"):
        return None
    if href.startswith("#"):
        return href
    if href.startswith("?tag="):
        return href
    path, _, frag = href.partition("#")
    path = unquote(path)
    if path.startswith("mainSpace/"):
        path = path[len("mainSpace/"):]
    if path.startswith("/"):
        path = path[1:]
    if path.startswith("files/"):
        target = path[len("files/"):]
        if target not in existing_files:
            log("Broken internal links / missing files", fname, f"`{href}`")
        return "../files/" + quote(target) + (("#" + frag) if frag else "")
    if path.endswith(".html"):
        path = path[:-5]
    if path.startswith("*"):
        path = "_" + path[1:]
    if path and path not in existing_pages:
        log("Broken internal links / missing files", fname, f"`{href}` (no page named “{path}”)")
    new = md_page_href(path) if path else ""
    if frag:
        frag = ANCHORS.get(path, {}).get(unquote(frag), frag)
        new += "#" + frag
    return new


STYLE_IGNORE = re.compile(r"^(cursor|line-height|margin-bottom|margin|padding|text-align: left|font-family|background-[a-z-]+|padding-right)\b")


def style_props(style):
    props = []
    for part in style.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            props.append((k.strip().lower(), v.strip()))
    return props


def preprocess(soup, fname):
    root = soup.find(id="content_view") or soup.body

    # Scripts (Twitter widgets etc.)
    for s in root.find_all("script"):
        src = s.get("src") or ""
        body = s.get_text()
        kind = "Twitter widget script" if "twitter" in body + src else "script"
        log("Scripts (removed)", fname, kind)
        s.decompose()
    for a in root.find_all("a", class_=["twitter-timeline", "twitter-follow-button"]):
        log("Twitter widgets (kept as plain link)", fname, f"{' '.join(a.get('class'))} → {a.get('href')}")

    # iframes
    for f in root.find_all("iframe"):
        src = html.unescape(f.get("src", ""))
        if src.startswith("//"):
            src = "https:" + src
        host = urlparse(src).netloc
        if "archive.org" in host:
            kind, cat = "Internet Archive book viewer", "Embeds: Internet Archive book viewers"
        elif "google" in host and "/maps" in src:
            kind, cat = "Google map", "Embeds: maps (Google / Bing / MapChannels)"
        elif "bing.com" in host:
            kind, cat = "Bing map", "Embeds: maps (Google / Bing / MapChannels)"
        elif "mapchannels" in host:
            kind, cat = "MapChannels map", "Embeds: maps (Google / Bing / MapChannels)"
        elif "books.google" in host:
            kind, cat = "Google Books viewer", "Embeds: Google Books viewers"
        elif "youtube" in host:
            kind, cat = "YouTube video", "Embeds: YouTube videos"
        else:
            kind, cat = f"embed from {host}", "Embeds: other"
        log(cat, fname, f"<{src}>")
        p = soup.new_tag("p")
        a = soup.new_tag("a", href=src)
        a.string = f"[Embedded {kind}]"
        p.append(a)
        f.replace_with(p)

    # object/embed (old Flash YouTube etc.)
    for o in root.find_all(["object", "embed"]):
        if o.name == "embed" and o.find_parent("object"):
            continue
        url = None
        for prm in o.find_all("param"):
            if prm.get("name") == "movie":
                url = html.unescape(prm.get("value"))
        if not url:
            e = o if o.name == "embed" else o.find("embed")
            url = html.unescape(e.get("src")) if e else None
        log("Embeds: Flash <object>/<embed>", fname, f"<{url}>")
        p = soup.new_tag("p")
        if url:
            a = soup.new_tag("a", href=url)
            a.string = "[Embedded Flash object]"
            p.append(a)
        o.replace_with(p)

    # Dipity etc. divs with embed classes
    for d in root.find_all(class_="dipity_embed"):
        log("Embeds: other", fname, f"Dipity timeline embed: {snippet(d)}")

    # Tag cloud widget
    for tc in root.find_all(class_="includeTagCloud"):
        tags = [li.get_text(strip=True) for li in tc.find_all("li")]
        log("Wikispaces widgets: tag cloud (removed)", fname, f"{len(tags)} tags: {', '.join(tags[:15])}{'…' if len(tags) > 15 else ''}")
        parent = tc.parent
        tc.decompose()
        if parent and parent.name in ("h1", "h2", "h3") and not parent.get_text(strip=True):
            parent.decompose()

    # Include-page-list widget (dynamic list of pages, kept as a static snapshot)
    for pl in root.find_all(class_="includePageList"):
        log("Wikispaces widgets: auto-generated page list (kept as static list)", fname, f"{len(pl.find_all('li'))} entries")

    # Footnote-style references (Wikipedia-like cite refs)
    for sup in root.find_all("sup", class_="reference"):
        log("Footnote references", fname, f"cite marker {sup.get_text(strip=True)} (reference list is empty in the export)")
    for ol in root.find_all("ol", class_="references"):
        if not ol.find("li"):
            ol.decompose()
    for hr in root.find_all("hr", class_="references"):
        hr.decompose()

    # Heading anchors -> GitHub slugs; others kept as raw HTML anchors
    anchor_map = {}
    for a in root.find_all("a", attrs={"name": True}):
        if a.get("href"):
            continue
        h = a.find_parent(re.compile(r"^h[1-6]$"))
        if h:
            anchor_map[a["name"]] = gh_slug(h.get_text())
            a.decompose()
        else:
            a.replace_with(NavigableString(f'\x00ANCHOR{a["name"]}\x00'))
            log("Non-heading anchors (kept as raw HTML <a name>)", fname, f"`{a['name']}`")

    # Links
    for a in root.find_all("a", href=True):
        href = a["href"]
        if "wiki_link_new" in (a.get("class") or []):
            log("Links to pages that never existed (wiki_link_new)", fname, f"“{a.get_text()}”")
        new = rewrite_internal(href, fname)
        if new is not None:
            if new.startswith("?tag="):
                log("Wikispaces tag links (removed, text kept)", fname, f"`{href}`")
                a.unwrap()
                continue
            if new.startswith("#"):
                frag = unquote(new[1:])
                if frag in anchor_map:
                    new = "#" + anchor_map[frag]
                elif frag in ANCHORS.get(fname[:-5], {}):
                    new = "#" + ANCHORS[fname[:-5]][frag]
            a["href"] = new

    # Images
    for img in root.find_all("img"):
        src = img.get("src", "")
        new = rewrite_internal(src, fname)
        if new is not None:
            img["src"] = new
        alt = img.get("alt", "")
        if alt.startswith("external image"):
            log("External images (cached copies in files/)", fname, f"{alt} → `{src}`")
        st = img.get("style", "") + " " + " ".join(f"{k}={img.get(k)}" for k in ("width", "height", "align") if img.get(k))
        if st.strip():
            log("Image sizing / float / alignment (dropped)", fname, f"{alt}: {st.strip()}")

    # Styled spans/divs/paragraphs
    for el in root.find_all(style=True):
        if el.name in ("img",):
            continue
        props = style_props(el["style"])
        kept_strike = False
        for k, v in props:
            if k == "color":
                log("Coloured text (colour dropped)", fname, f"{v}: “{snippet(el, 60)}”")
            elif k == "text-decoration" and "line-through" in v:
                kept_strike = True
            elif k in ("margin-left", "padding-left") and v not in ("0", "0px"):
                log("Indentation (dropped)", fname, f"{k} {v}: “{snippet(el, 60)}”")
            elif k == "vertical-align" and v == "super":
                el.name = "sup"
            elif k == "font-size" and el.name == "span" and v not in ("10pt",):
                log("Font size changes (dropped)", fname, f"{v}: “{snippet(el, 60)}”")
            elif k == "float":
                log("Floats / layout (dropped)", fname, f"float {v} on <{el.name}>: “{snippet(el, 60)}”")
            elif k == "text-align" and v not in ("left",):
                log("Text alignment (dropped)", fname, f"{v}: “{snippet(el, 60)}”")
            elif k == "background-color" and v not in ("initial", "transparent"):
                log("Background colour / highlight (dropped)", fname, f"{v}: “{snippet(el, 60)}”")
        if kept_strike:
            el.name = "del"
        del el["style"]

    for u in root.find_all("u"):
        log("Underline (kept as inline <u> HTML)", fname, f"“{snippet(u, 60)}”")
    for s in root.find_all("sup"):
        if "reference" not in (s.get("class") or []):
            log("Superscript (kept as inline <sup> HTML)", fname, f"“{snippet(s, 40)}”")
    for s in root.find_all("small"):
        log("Small text (size dropped)", fname, f"“{snippet(s, 60)}”")

    for t in root.find_all("table"):
        complex_ = bool(t.find(attrs={"colspan": True}) or t.find(attrs={"rowspan": True}) or t.find("table")
                        or any(td.find(["ul", "ol", "br", "img", "h1", "h2"]) for td in t.find_all("td")))
        rows = len(t.find_all("tr"))
        log("Tables (converted to Markdown tables; check layout)", fname,
            f"{rows} rows{' — contains merged cells / line breaks / lists / images, may not render well' if complex_ else ''}")

    return root


class Conv(MarkdownConverter):
    def convert_u(self, el, text, parent_tags):
        return f"<u>{text}</u>" if text.strip() else text

    def convert_sup(self, el, text, parent_tags):
        if "reference" in (el.get("class") or []):
            return text
        return f"<sup>{text}</sup>" if text.strip() else text

    def convert_del(self, el, text, parent_tags):
        return f"~~{text}~~" if text.strip() else text

    def convert_td(self, el, text, parent_tags):
        text = re.sub(r"\s*\n\s*", " <br> ", text.strip())
        return " " + text.replace("|", "\\|") + " |"


def to_markdown(root):
    md = Conv(heading_style="ATX", bullets="-", escape_underscores=False, escape_asterisks=True,
              newline_style="BACKSLASH", table_infer_header=True).convert_soup(root)
    # stray "<" in the text (mostly OCR noise) would otherwise be read as HTML
    md = re.sub(r"<(?!/?(?:u|sup|br)>|https?://|mailto:)", r"\\<", md)
    md = md.replace("\x00ANCHOR", '<a name="').replace("\x00", '"></a>')
    return md


def tidy(md):
    lines = [l.rstrip() for l in md.splitlines()]
    # Wikispaces used <br/> for everything; a line that is only a hard break, or a hard break
    # before a blank line, is really a paragraph break
    lines = ["" if l.strip() == "\\" else l for l in lines]
    for i in range(len(lines) - 1):
        if lines[i].endswith("\\") and not lines[i + 1].strip():
            lines[i] = lines[i][:-1].rstrip()
    if lines and lines[-1].endswith("\\"):
        lines[-1] = lines[-1][:-1].rstrip()
    out = "\n".join(lines)
    # Wikispaces used <br/><br/> as paragraph breaks
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n"


def main():
    global ANCHORS
    ANCHORS = build_anchor_maps()
    OUT.mkdir(exist_ok=True)
    files = sorted(SRC.glob("*.html"))
    for f in files:
        raw = f.read_text(encoding="utf-8")
        soup = BeautifulSoup(raw, "html.parser")
        title = soup.title.get_text().strip() if soup.title else f.stem
        title = re.sub(r"^BartonHistory - ", "", title)
        root = preprocess(soup, f.name)
        body = tidy(to_markdown(root))
        front = f'---\ntitle: "{title.replace(chr(34), chr(92) + chr(34))}"\n---\n\n'
        (OUT / (f.stem + ".md")).write_text(front + body, encoding="utf-8")

    # Review list
    lines = ["# Conversion review list", "",
             f"Generated when converting {len(files)} HTML pages to Markdown. Each section lists things in the "
             "original HTML that don't map cleanly to Markdown, with what the converter did and where they occur.", "",
             "## Summary", "", "| Item | Occurrences | Pages | What the converter did |", "|---|---|---|---|"]
    actions = {}
    for cat in issues:
        m = re.search(r"\(([^)]*)\)\s*$", cat)
        if cat.startswith("Embeds"):
            actions[cat] = "replaced with a link to the embedded URL"
        elif cat.startswith("Broken"):
            actions[cat] = "link kept as-is (target page/file isn't in the export)"
        elif cat.startswith("Footnote"):
            actions[cat] = "marker kept as plain text, e.g. [2]"
        else:
            actions[cat] = m.group(1) if m else ""
    def esc(t):
        return re.sub(r"(<[^>]+>)", r"`\1`", t)
    order = sorted(issues, key=lambda c: (not c.startswith("Embeds"), c))
    for cat in order:
        pages = {f for f, _ in issues[cat]}
        lines.append(f"| [{esc(cat)}](#{gh_slug(esc(cat))}) | {len(issues[cat])} | {len(pages)} | {esc(actions[cat])} |")
    grouped = ("Coloured text", "Indentation", "Font size", "Background")
    for cat in order:
        lines += ["", f"## {esc(cat)}", ""]
        if cat.startswith(grouped):
            # page -> value -> examples
            g = defaultdict(lambda: defaultdict(list))
            for f, d in issues[cat]:
                k, _, ex = d.partition(": ")
                g[f][k].append(ex)
            for f in sorted(g):
                lines.append(f"- [{f[:-5]}]({md_page_href(f[:-5])})")
                for k, exs in sorted(g[f].items(), key=lambda kv: -len(kv[1])):
                    shown = [e for e in exs if e.strip("“”")][:2]
                    lines.append(f"  - **{k}** ×{len(exs)}" + (f" — e.g. {'; '.join(shown)}" if shown else ""))
            continue
        byfile = defaultdict(list)
        for f, d in issues[cat]:
            byfile[f].append(d)
        for f in sorted(byfile):
            ds = byfile[f]
            link = f"[{f[:-5]}]({md_page_href(f[:-5])})"
            if len(ds) == 1:
                lines.append(f"- {link}: {ds[0]}")
            else:
                lines.append(f"- {link} ({len(ds)})")
                for d in ds[:25]:
                    lines.append(f"  - {d}")
                if len(ds) > 25:
                    lines.append(f"  - …and {len(ds) - 25} more")
    (OUT / "CONVERSION-REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"converted {len(files)} files")
    for cat in order:
        print(f"{len(issues[cat]):6}  {cat}")


main()
