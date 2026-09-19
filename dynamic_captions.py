"""Readable bottom captions; replace only verified changing English captions."""
import json
from pathlib import Path
import shutil


def chunks(cues):
    for start, end, text in cues:
        groups, group = [], []
        for word in text.upper().split():
            if group and (len(group) == 3 or len(' '.join(group + [word])) > 24):
                groups.append(' '.join(group))
                group = []
            group.append(word)
        if group:
            groups.append(' '.join(group))
        total, consumed, previous = sum(map(len, groups)), 0, start
        for group in groups:
            consumed += len(group)
            until = start + round((end - start) * consumed / total)
            if until > previous:
                yield previous, until, group
            previous = until


def stamp(ms):
    cs = round(ms / 10)
    return f'{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}'


def make_ass(srt, source, duration, width, height):
    from subtitles import parse_captions
    cues = parse_captions(Path(srt).read_text(encoding='utf-8'), 'srt')
    if not cues:
        raise ValueError('empty dynamic captions')
    cw, ch = 720, round(720 * height / width)
    font = max(42, min(64, round(ch * .055)))
    layout_file = Path(srt).with_name('caption_layout.json')
    layout = json.loads(layout_file.read_text(encoding='utf-8')) if layout_file.exists() else {}
    box = layout.get('box')
    if box and not (len(box) == 4 and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
        raise ValueError('invalid verified caption box')
    y, anchor, events = round(ch * .89), 2, []
    if box:
        center = (box[1] + box[3]) * ch / 2
        half = max((box[3] - box[1]) * ch / 2 + 10, font * .8)
        top, bottom = max(0, round(center-half)), min(ch, round(center+half))
        y, anchor = round(center), 5
        draw = f'{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c&H000000&\\alpha&H00&}}m 0 {top} l {cw} {top} {cw} {bottom} 0 {bottom}{{\\p0}}'
        events.append(f'Dialogue: 0,{stamp(0)},{stamp(round(duration*1000))},Mask,,0,0,0,,{draw}')
    font_dir = Path(srt).parent / 'caption_fonts'
    font_dir.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).parent / 'fonts/LuckiestGuy-Regular.ttf', font_dir / 'LuckiestGuy-Regular.ttf')
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {cw}
PlayResY: {ch}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Luckiest Guy,{font},&H0000FFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,{anchor},18,18,0,1
Style: Mask,Luckiest Guy,{font},&H00000000,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    # Verified on-screen phrases stay intact, without borrowing the next word.
    rendered = cues if layout.get('source') == 'burned' else chunks(cues)
    from PIL import ImageFont
    metrics = ImageFont.truetype(str(font_dir / 'LuckiestGuy-Regular.ttf'), font)
    for start, end, text in rendered:
        safe = text.upper().replace('\\', '/').replace('{', '(').replace('}', ')')
        size = min(font, round(font * (cw-40) / max(1, metrics.getlength(safe))))
        size = max(round(font * .7), size)
        tags = f'{{\\an{anchor}\\pos({cw//2},{y})\\fs{size}\\fscx96\\fscy96\\t(0,70,\\fscx100\\fscy100)}}'
        events.append(f'Dialogue: 1,{stamp(start)},{stamp(end)},Default,,0,0,0,,{tags}{safe}')
    path = Path(srt).with_name('italian.ass')
    path.write_text(header + '\n'.join(events) + '\n', encoding='utf-8')
    return path
