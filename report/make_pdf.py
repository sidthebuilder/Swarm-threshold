"""Build The_Defence_That_Caused_The_Leak.pdf from PAPER_FINAL.md.

Layout follows the official template: Old Standard TT serif body, Arial sans headings, a
centred title block. Figures are placed at their real aspect ratio and never separated from a
page break by hand-tracking the cursor position, the same approach as DocumentWriter requires.

    python report/make_pdf.py
"""
import html
import re
from pathlib import Path

import pymupdf as fitz
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "report" / "PAPER_FINAL.md"
FIGS = ROOT / "report" / "figures"
FONTS = ROOT / "report" / "fonts"
OUT = ROOT / "The_Defence_That_Caused_The_Leak.pdf"
PREVIEW = ROOT / "report" / "_preview"
PREVIEW.mkdir(exist_ok=True)

PAGE = fitz.paper_rect("letter")
MARGIN = (72, 58, -72, -58)
FULL = PAGE + MARGIN
TEXT_W = FULL.width

CSS = """
@font-face {font-family: Body; src: url(OldStandard-Regular.ttf);}
@font-face {font-family: Body; src: url(OldStandard-Bold.ttf); font-weight: bold;}
@font-face {font-family: Body; src: url(OldStandard-Italic.ttf); font-style: italic;}
body {margin: 0; font-family: Body; font-size: 10.3pt; line-height: 1.38; color: #111111;}
p {margin-top: 0; margin-bottom: 7pt; text-align: justify;}
h1 {font-weight: bold; font-size: 19pt; text-align: center; margin-top: 0; margin-bottom: 9pt;}
h2 {font-weight: bold; font-size: 12.3pt; margin-top: 12pt; margin-bottom: 5pt;}
h3 {font-weight: bold; font-style: italic; font-size: 10.5pt; margin-top: 8pt; margin-bottom: 3pt;}
p.center {text-align: center; margin-bottom: 3pt; margin-top: 0;}
p.abstract {font-style: italic; font-size: 9.8pt; text-align: justify; margin-top: 3pt;}
p.small {font-size: 8.8pt; line-height: 1.3; text-align: left;}
li {margin-bottom: 4pt; text-align: left;}
li.small {font-size: 8.8pt; line-height: 1.3; margin-bottom: 3pt;}
table {width: 100%; border-collapse: collapse; font-size: 8.6pt; margin-top: 4pt; margin-bottom: 8pt;}
table.small {font-size: 7.7pt;}
th {font-weight: bold; text-align: left; border-bottom: 1.2px solid #111; padding: 3px 5px;}
td {text-align: left; border-bottom: 0.6px solid #ccc; padding: 3px 5px; vertical-align: top;}
hr {border: none; border-top: 0.8px solid #999; margin: 5pt 0;}
code {font-family: Body; font-style: italic;}
a {color: #0f5fa3;}
"""


def inline(text: str) -> str:
    s = html.escape(text, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)


def parse_table(lines):
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines
            if not re.fullmatch(r"\|?[\s:|-]+\|?", ln)]
    header, *body = rows
    cls = ' class="small"' if len(header) >= 5 else ""
    out = [f"<table{cls}><thead><tr>"]
    out += [f"<th>{inline(c)}</th>" for c in header]
    out.append("</tr></thead><tbody>")
    for row in body:
        row = row + [""] * (len(header) - len(row))
        out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def image_size(name: str) -> tuple[int, int]:
    with Image.open(FIGS / name) as img:
        return img.size


def segments(md_text: str):
    """One ('html', text) segment per H1/H2 block, plus ('fig', name, height) segments, so a
    heading and its first paragraph move to a new page together."""
    segs, buf, words = [], [], 0
    seen_h2 = False
    for block in re.split(r"\n\s*\n", md_text.strip()):
        block = block.strip()
        fig = re.fullmatch(r"!\[(.*?)\]\(([^)]+)\)", block)
        if fig:
            segs.append(("html", "\n".join(buf)))
            buf = []
            name = Path(fig.group(2)).name
            w, h = image_size(name)
            segs.append(("fig", name, TEXT_W * h / w))
            continue
        if block.startswith("# "):
            segs.append(("html", "\n".join(buf)))
            buf = [f"<h1>{inline(block[2:])}</h1>"]
            continue
        if block.startswith("## "):
            segs.append(("html", "\n".join(buf)))
            buf = [f"<h2>{inline(block[3:].strip())}</h2>"]
            seen_h2 = True
            continue
        if block.startswith("### "):
            buf.append(f"<h3>{inline(block[4:].strip())}</h3>")
            continue
        if block == "---":
            buf.append("<hr/>")
            continue
        lines = [ln.strip() for ln in block.splitlines()]
        if lines and lines[0].startswith("|"):
            buf.append(parse_table(lines))
            continue
        if lines and all(re.match(r"^(-|\d+\.)\s", ln) for ln in lines):
            tag = "ol" if re.match(r"^\d+\.", lines[0]) else "ul"
            li_cls = ' class="small"' if seen_h2 else ""
            stripped = [re.sub(r"^(-|\d+\.)\s", "", ln) for ln in lines]
            items = "".join(f"<li{li_cls}>{inline(s)}</li>" for s in stripped)
            buf.append(f"<{tag}>{items}</{tag}>")
            continue
        text = " ".join(lines)
        if not seen_h2 and (text.startswith("With Apart") or text.startswith("Track ")
                             or text.startswith("Shashank Kumar")):
            buf.append(f'<p class="center">{inline(text)}</p>')
        elif not seen_h2 and text.startswith("*(") and text.endswith(")*"):
            buf.append(f'<p class="abstract small-note">{inline(text)}</p>')
        else:
            buf.append(f"<p>{inline(text)}</p>")
            if seen_h2:
                words += len(text.split())
    segs.append(("html", "\n".join(buf)))
    return [s for s in segs if s[0] == "fig" or s[1].strip()], words


def layout(segs):
    archive = fitz.Archive(str(FONTS))
    archive.add(str(FIGS))
    import io
    stream = io.BytesIO()
    writer = fitz.DocumentWriter(stream)
    device = writer.begin_page(PAGE)
    y = FULL.y0

    def new_page(cur_device):
        writer.end_page()
        dev = writer.begin_page(PAGE)
        return dev, FULL.y0

    for seg in segs:
        if seg[0] == "fig":
            name, h = seg[1], seg[2]
            if y + h > FULL.y1 and y > FULL.y0 + 1:
                device, y = new_page(device)
            img_html = f'<img src="{name}" width="{TEXT_W:.1f}" height="{h:.1f}"/>'
            story = fitz.Story(html=f"<html><body>{img_html}</body></html>",
                               user_css="body{margin:0;}", archive=archive)
            story.place(fitz.Rect(FULL.x0, y, FULL.x1, y + h + 1))
            story.draw(device)
            y += h + 10
            continue
        if seg[1].startswith("<h2>") and y > FULL.y1 - 90:
            device, y = new_page(device)
        story = fitz.Story(html=f"<html><body>{seg[1]}</body></html>", user_css=CSS, archive=archive)
        more = 1
        while more:
            if y > FULL.y1 - 24:
                device, y = new_page(device)
            more, filled = story.place(fitz.Rect(FULL.x0, y, FULL.x1, FULL.y1))
            story.draw(device)
            if more:
                device, y = new_page(device)
            else:
                y = fitz.Rect(filled).y1
    writer.end_page()
    writer.close()
    return fitz.open("pdf", stream.getvalue())


def build():
    md_text = MD.read_text(encoding="utf-8")
    abstract_match = re.search(r"## Abstract\s*\n+\*\(.*?\)\*\s*\n+(.*?)\n\n##", md_text, re.S)
    abstract_words = len(abstract_match.group(1).split()) if abstract_match else 0
    segs, body_words = segments(md_text)
    doc = layout(segs)
    georgia = str(FONTS / "OldStandard-Regular.ttf")
    for i, page in enumerate(doc):
        if i == 0:
            continue
        label = str(i + 1)
        x = PAGE.width / 2 - fitz.get_text_length(label, fontsize=8) / 2
        page.insert_text((x, PAGE.height - 32), label, fontsize=8, fontname="os",
                         fontfile=georgia, color=(0.35, 0.35, 0.35))
    doc.set_metadata({
        "title": "A Defence That Caused the Leak",
        "author": "Shashank Kumar",
        "subject": "Swarm Alignment Hackathon, Track 4, with a Track 2 result",
        "creator": "Shashank Kumar, with Kiro (Claude) as a coding assistant",
    })
    doc.save(str(OUT), garbage=3, deflate=True)
    pages = len(doc)
    doc.close()
    return abstract_words, body_words, pages


def contact_sheet():
    src = fitz.open(str(OUT))
    n = len(src)
    cols = min(4, n)
    rows = (n + cols - 1) // cols
    W, H = PAGE.width, PAGE.height
    sheet = fitz.open()
    pg = sheet.new_page(width=cols * W, height=rows * H)
    for i in range(n):
        r = fitz.Rect((i % cols) * W, (i // cols) * H, (i % cols + 1) * W, (i // cols + 1) * H)
        pg.show_pdf_page(r, src, i)
        pg.draw_rect(r, color=(0.6, 0.6, 0.6), width=1.5)
    pg.get_pixmap(dpi=42).save(str(PREVIEW / "contact.png"))
    for i in range(n):
        src[i].get_pixmap(dpi=105).save(str(PREVIEW / f"page{i + 1}.png"))
    src.close()
    return n


if __name__ == "__main__":
    abstract_words, body_words, pages = build()
    n = contact_sheet()
    print(f"abstract words: {abstract_words} (must be 150-250)")
    print(f"body words (excludes title block, tables, limitations/dual-use/appendix bullets, "
          f"references): {body_words}")
    print(f"pages: {pages}")
    print("wrote:", OUT)
