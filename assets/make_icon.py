"""Génère assets/icon.png, assets/icon.ico (Windows) et assets/icon.icns (macOS) (cube filaire sur fond olive, dans les couleurs de l'app).

Usage : python3 assets/make_icon.py        (macOS : utilise sips + iconutil pour le .icns)
"""
import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 2048  # dessin en 2× puis réduit à 1024 (anticrénelage)
BG, BG2, ACCENT = (44, 49, 43), (70, 76, 68), (136, 110, 75)


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = int(S * 0.055)  # marge de l'icône macOS
    d.rounded_rectangle((m, m, S - m, S - m), radius=int(S * 0.2), fill=BG)
    # léger dégradé vers le haut
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(m, S - m):
        t = 1 - (y - m) / (S - 2 * m)
        gd.line((m, y, S - m, y), fill=BG2 + (int(90 * t),))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((m, m, S - m, S - m), radius=int(S * 0.2), fill=255)
    img = Image.composite(Image.alpha_composite(img, grad), img, mask)
    d = ImageDraw.Draw(img)

    # cube isométrique
    cx, cy, r = S * 0.5, S * 0.47, S * 0.27
    top = [(cx, cy - r), (cx + r * 0.866, cy - r / 2), (cx, cy), (cx - r * 0.866, cy - r / 2)]
    left = [(cx - r * 0.866, cy - r / 2), (cx, cy), (cx, cy + r), (cx - r * 0.866, cy + r / 2)]
    right = [(cx, cy), (cx + r * 0.866, cy - r / 2), (cx + r * 0.866, cy + r / 2), (cx, cy + r)]
    d.polygon(top, fill=(226, 208, 164))
    d.polygon(left, fill=ACCENT)
    d.polygon(right, fill=(98, 78, 52))
    for poly in (top, left, right):
        d.line(poly + [poly[0]], fill=BG, width=int(S * 0.012), joint="curve")

    # repère XYZ (rouge / vert / bleu), clin d'œil à l'app : l'origine d'AI
    ox, oy, ln, w = S * 0.27, S * 0.80, S * 0.17, int(S * 0.03)
    d.line((ox, oy, ox + ln, oy - ln * 0.35), fill=(255, 70, 70), width=w)
    d.line((ox, oy, ox - ln * 0.12, oy + ln * 0.62), fill=(80, 220, 80), width=w)
    d.line((ox, oy, ox, oy - ln), fill=(90, 140, 255), width=w)
    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    img = draw()
    png = os.path.join(HERE, "icon.png")
    img.save(png)
    img.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon.png et icon.ico générés")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "icon.iconset")
        os.mkdir(iconset)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                img.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))
        if shutil.which("iconutil"):
            subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(HERE, "icon.icns")], check=True)
            print("icon.icns généré")
        else:
            print("iconutil absent (hors macOS) : seul icon.png a été généré")


if __name__ == "__main__":
    main()
