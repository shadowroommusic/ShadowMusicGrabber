# -*- coding: utf-8 -*-
"""生成应用图标：纯黑圆角底 + 白色 S。

用法:
    python tools/make_icon.py

产物:
    assets/icon.png   1024x1024 主图
    assets/icon.ico   多尺寸 Windows 图标(16~256)

设计要点: 超采样绘制后缩放以获得干净边缘; S 单独渲染再做轻微倾斜,
倾斜以画布中心为轴, 避免字形跑偏。
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

SIZE = 1024
SUPERSAMPLE = 2
BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)
CORNER_RATIO = 0.16     # 圆角半径占边长比例
LETTER_RATIO = 0.72     # S 的字号占边长比例
SKEW_DEGREES = 8.0      # 轻微倾斜, 让 S 更有动感
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\ariblk.ttf",   # Arial Black
    r"C:\Windows\Fonts\seguibl.ttf",  # Segoe UI Black
    r"C:\Windows\Fonts\arialbd.ttf",  # Arial Bold
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _load_font(px: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return ImageFont.truetype(path, px)
    raise SystemExit("找不到可用的粗体字体，请把字体路径加入 FONT_CANDIDATES")


def render(size: int = SIZE) -> Image.Image:
    """返回 size×size 的 RGBA 图标。"""
    canvas = size * SUPERSAMPLE
    base = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ImageDraw.Draw(base).rounded_rectangle(
        [0, 0, canvas - 1, canvas - 1],
        radius=int(canvas * CORNER_RATIO),
        fill=BLACK,
    )

    # 字母单独一层：先居中绘制，再以画布中心为轴做水平倾斜。
    layer = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _load_font(int(canvas * LETTER_RATIO))
    left, top, right, bottom = draw.textbbox((0, 0), "S", font=font)
    draw.text(
        (canvas / 2 - (right - left) / 2 - left, canvas / 2 - (bottom - top) / 2 - top),
        "S",
        font=font,
        fill=WHITE,
    )
    shear = math.tan(math.radians(SKEW_DEGREES))
    layer = layer.transform(
        (canvas, canvas),
        Image.AFFINE,
        (1, shear, -shear * canvas / 2, 0, 1, 0),
        resample=Image.BICUBIC,
    )
    base.alpha_composite(layer)
    return base.resize((size, size), Image.LANCZOS)


def check(icon: Image.Image) -> list[str]:
    """基础自检：圆角透明、底色纯黑、中央有白色字形。"""
    size = icon.width
    problems = []
    if icon.getpixel((0, 0))[3] != 0:
        problems.append("左上角应为透明（圆角）")
    if icon.getpixel((size // 2, 6))[:3] != (0, 0, 0):
        problems.append("顶部边缘应为纯黑")
    box = (int(size * 0.3), int(size * 0.3), int(size * 0.7), int(size * 0.7))
    center = icon.crop(box).convert("RGBA")
    white = sum(1 for px in center.getdata() if px[0] > 230 and px[1] > 230 and px[2] > 230)
    ratio = white / (center.width * center.height)
    if ratio < 0.15:
        problems.append(f"中央白色字形占比过低({ratio:.2f})")
    return problems


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "assets")
    os.makedirs(out_dir, exist_ok=True)

    icon = render()
    png_path = os.path.join(out_dir, "icon.png")
    ico_path = os.path.join(out_dir, "icon.ico")
    icon.save(png_path, format="PNG")
    icon.save(ico_path, format="ICO", sizes=ICO_SIZES)

    problems = check(icon)
    print(f"icon.png -> {png_path} ({os.path.getsize(png_path)} bytes)")
    print(f"icon.ico -> {ico_path} ({os.path.getsize(ico_path)} bytes, {len(ICO_SIZES)} 种尺寸)")
    print("自检:", "通过" if not problems else "发现问题: " + "; ".join(problems))
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
