"""Prepare a small, static JPEG for embedding; Pillow is optional outside Docker."""
import warnings
from pathlib import Path


def prepare_cover(output, title, author, source=None):
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
    except ImportError as error:
        raise ValueError('补封面需要 Pillow，请安装系统 python3-pil 后重启。') from error
    if source is not None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    if image.format not in {'JPEG', 'PNG'} or image.width * image.height > 20000000 or getattr(image, 'n_frames', 1) != 1:
                        raise ValueError('封面须为单帧 JPEG/PNG，最多 2000 万像素。')
                    image.load()
                    image = ImageOps.exif_transpose(image).convert('RGBA')
                    image.thumbnail((900, 1200), Image.Resampling.LANCZOS)
                    paper = Image.new('RGB', image.size, 'white')
                    paper.paste(image, mask=image.getchannel('A'))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ValueError('图片无效、损坏或像素过大，请选择 JPEG/PNG。') from error
    else:
        font_path = next((p for p in ('/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
                                     '/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc') if Path(p).is_file()), None)
        if not font_path:
            raise ValueError('生成中文封面需要 Noto CJK 字体，请安装 fonts-noto-cjk。')
        paper = Image.new('RGB', (900, 1200), '#f3efe3')
        draw = ImageDraw.Draw(paper)
        draw.rectangle((34, 34, 865, 1165), outline='#615a49', width=2)
        draw.line((96, 150, 188, 150), fill='#9b692e', width=5)
        for text, region, initial_size in ((title, (96, 220, 804, 860), 66), (author or '作者待补充', (96, 960, 804, 1100), 30)):
            left, top, right, bottom = region
            for size in range(initial_size, 9, -2):
                font = ImageFont.truetype(font_path, size)
                lines, line = [], ''
                for char in ' '.join(text.split()):
                    if line and draw.textlength(line + char, font=font) > right - left:
                        lines.append(line); line = ''
                    line += char
                lines.append(line)
                rendered = '\n'.join(lines)
                box = draw.multiline_textbbox((0, 0), rendered, font=font, spacing=size // 3)
                if box[3] - box[1] <= bottom - top:
                    break
            draw.multiline_text((left, top - box[1]), rendered, font=font, spacing=size // 3, fill='#29271f')
    paper.save(output, 'JPEG', quality=90)
