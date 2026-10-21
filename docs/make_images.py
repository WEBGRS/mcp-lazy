"""Draw docs/memory.png (README) and docs/social-preview.png (GitHub social card) from the README numbers
  python docs/make_images.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent
BG, INK, MUTE, AMBER, GREEN, LINE = '#FBFAF7', '#1D1D1F', '#55534E', '#F2A900', '#1F8A4C', '#E6E2DA'
FONTS = Path(r'C:\Windows\Fonts')
# (label, sublabel, before MB, before text, after MB, after text)
ROWS = [('Claude Code session', '5 servers, 2026-10-20', 500.0, '500 MB', 44.6, '45 MB'),
        ('Codex session', '5 wrapped servers, 2026-10-21', 821.0, '~820 MB', 45.0, '~45 MB')]


def font(size, bold=False):
    return ImageFont.truetype(str(FONTS / ('segoeuib.ttf' if bold else 'segoeui.ttf')), size)


def bars(d, x0, y0, width, scale, rows, s=1.0):
    # Two bars per row: idle memory before, then with mcp-lazy
    y = y0
    for label, sub, b_mb, b_txt, a_mb, a_txt in rows:
        d.text((x0, y), label, font=font(int(30 * s), True), fill=INK)
        d.text((x0, y + int(38 * s)), sub, font=font(int(22 * s)), fill=MUTE)
        y += int(80 * s)
        for mb, txt, color, tag in ((b_mb, b_txt, AMBER, 'servers running'), (a_mb, a_txt, GREEN, 'with mcp-lazy')):
            w = max(int(6 * s), int(width * mb / scale))
            h = int(40 * s)
            d.rounded_rectangle([x0, y, x0 + w, y + h], radius=int(8 * s), fill=color)
            d.text((x0 + w + int(14 * s), y + int(2 * s)), txt, font=font(int(30 * s), True), fill=INK)
            tw = d.textlength(txt, font=font(int(30 * s), True))
            d.text((x0 + w + int(24 * s) + tw, y + int(8 * s)), tag, font=font(int(22 * s)), fill=MUTE)
            y += h + int(12 * s)
        y += int(36 * s)
    return y


def memory_png():
    W, H = 1600, 700
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((70, 50), 'Idle MCP memory, before and after', font=font(46, True), fill=INK)
    d.text((70, 112), 'Private memory once every server has answered the handshake and is waiting for a call',
           font=font(26), fill=MUTE)
    d.line([70, 165, W - 70, 165], fill=LINE, width=2)
    y = bars(d, 70, 195, 1100, 900, ROWS)
    d.text((70, H - 60), 'Windows 11. Measurement method and raw numbers in the README.', font=font(22), fill=MUTE)
    img.save(OUT / 'memory.png', optimize=True)


def social_png():
    W, H = 1280, 640
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((70, 70), 'mcp-lazy', font=font(96, True), fill=INK)
    d.text((74, 200), 'Keep MCP servers asleep until a tool is called', font=font(36), fill=INK)
    d.text((74, 252), 'Lazy stdio proxy for Claude Code and Codex', font=font(28), fill=MUTE)
    d.line([70, 318, W - 70, 318], fill=LINE, width=2)
    bars(d, 70, 350, 760, 520, ROWS[:1], s=1.0)
    img.save(OUT / 'social-preview.png', optimize=True)


if __name__ == '__main__':
    memory_png()
    social_png()
    print('wrote', OUT / 'memory.png', OUT / 'social-preview.png')
