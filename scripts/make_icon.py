"""生成“智能错题本”应用图标。"""
from pathlib import Path

from PIL import Image, ImageDraw


def make_icon() -> Image.Image:
    scale = 3
    size = 512
    width = size * scale
    image = Image.new("RGBA", (width, width), (0, 0, 0, 0))
    base = Image.new("RGBA", (width, width))
    draw = ImageDraw.Draw(base)
    start = (30, 64, 175)
    end = (13, 148, 136)
    for y in range(width):
        for x in range(0, width, 8):
            t = min(1.0, x / width * 0.35 + y / width * 0.65)
            color = tuple(
                round(a + (b - a) * t) for a, b in zip(start, end, strict=True)
            ) + (255,)
            draw.rectangle((x, y, min(x + 7, width - 1), y), fill=color)
    mask = Image.new("L", (width, width), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, width - 1), radius=112 * scale, fill=255
    )
    image.paste(base, (0, 0), mask)

    highlight = Image.new("RGBA", (width, width), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse(
        (-90 * scale, -150 * scale, 410 * scale, 300 * scale),
        fill=(255, 255, 255, 22),
    )
    image.alpha_composite(highlight)

    draw = ImageDraw.Draw(image)

    def box(values):
        return tuple(round(value * scale) for value in values)

    def points(values):
        return [(round(x * scale), round(y * scale)) for x, y in values]

    draw.polygon(
        points([(116, 181), (172, 163), (221, 164), (256, 184), (256, 357),
                (220, 340), (169, 341), (116, 361)]),
        fill=(248, 253, 255, 255),
    )
    draw.polygon(
        points([(256, 184), (291, 164), (340, 163), (396, 181), (396, 361),
                (343, 341), (292, 340), (256, 357)]),
        fill=(224, 247, 250, 255),
    )
    draw.line(points([(256, 187), (256, 354)]), fill=(147, 197, 220, 255), width=3 * scale)
    draw.line(points([(119, 183), (119, 358)]), fill=(255, 255, 255, 180), width=2 * scale)
    for y, right in [(214, 218), (240, 211), (266, 224)]:
        draw.rounded_rectangle(
            box((145, y, right, y + 7)), radius=3 * scale, fill=(117, 160, 196, 255)
        )
    draw.line(
        points([(287, 259), (309, 281), (354, 224)]),
        fill=(13, 148, 136, 255),
        width=15 * scale,
        joint="curve",
    )
    draw.ellipse(box((279, 251, 295, 267)), fill=(13, 148, 136, 255))
    draw.ellipse(box((346, 216, 362, 232)), fill=(13, 148, 136, 255))
    draw.polygon(
        points([(358, 90), (367, 116), (393, 125), (367, 134), (358, 160),
                (349, 134), (323, 125), (349, 116)]),
        fill=(207, 250, 254, 255),
    )
    draw.ellipse(box((389, 91, 403, 105)), fill=(165, 243, 252, 255))
    return image.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    static_dir = Path(__file__).resolve().parents[1] / "static"
    icon = make_icon()
    icon.save(static_dir / "icon.png")
    icon.resize((192, 192), Image.Resampling.LANCZOS).save(static_dir / "icon-192.png")
    print("icons written")
