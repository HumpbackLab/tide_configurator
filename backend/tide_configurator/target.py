"""Screen AprilGrid and its physical scale, with Kalibr's two-cell black border.

Uses Kalibr's tag36h11 code family and default rotation=2 marker orientation.
IDs start at the bottom left. No page size or screen DPI is assumed.
"""
from functools import lru_cache
import math

COLS, ROWS, SPACING = 6, 4, 0.3
UNIT, MARGIN = 100, 40
WIDTH = COLS + (COLS + 1) * SPACING
HEIGHT = ROWS + (ROWS + 1) * SPACING
CODES = (
    57401312644, 58383764297, 59366215950, 61331119256, 63296022562, 65260925868,
    1453707397, 4401062356, 9313320621, 10295772274, 14225578886, 17172933845,
    18155385498, 19137837151, 21102740457, 22085192110, 24050095416, 27979902028,
    28962353681, 33874611946, 34857063599, 35839515252, 37804418558, 42716676823,
)


def configuration(width_mm):
    if (isinstance(width_mm, bool) or not isinstance(width_mm, (int, float))
            or not math.isfinite(width_mm) or not 0 < width_mm <= 10000):
        raise ValueError('请输入录制时左右最外侧黑色小方块外缘之间的实测跨度（mm）')
    return dict(target_type='aprilgrid', tagCols=COLS, tagRows=ROWS,
                tagSize=width_mm / 1000 / WIDTH, tagSpacing=SPACING)


def specification():
    return dict(cols=COLS, rows=ROWS, spacing=SPACING, span_tags=WIDTH,
                view_width=round(WIDTH * UNIT + 2 * MARGIN),
                view_height=round(HEIGHT * UNIT + 2 * MARGIN),
                span_start=MARGIN, span_end=round(MARGIN + WIDTH * UNIT))


@lru_cache(maxsize=1)
def svg():
    spec = specification()
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'viewBox="0 0 {spec["view_width"]} {spec["view_height"]}" '
             'shape-rendering="crispEdges">',
             '<title>TideSense AprilGrid 6 × 4</title>',
             '<rect width="100%" height="100%" fill="white"/>', '<g fill="black">']

    def rect(x, y, w, h, fill='black'):
        parts.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" fill="{fill}"/>')

    gap = SPACING * UNIT
    # The outer squares make the measurement endpoints unambiguous. Their full
    # outer-edge span is cols + (cols + 1) * spacing, including both outer gaps.
    for row in range(ROWS + 1):
        for col in range(COLS + 1):
            rect(MARGIN + col * (UNIT + gap), MARGIN + row * (UNIT + gap), gap, gap)
    for row in range(ROWS):
        for col in range(COLS):
            identifier = (ROWS - 1 - row) * COLS + col
            x, y = MARGIN + gap + col * (UNIT + gap), MARGIN + gap + row * (UNIT + gap)
            parts.append(f'<g data-tag-id="{identifier}">')
            rect(x, y, UNIT, UNIT)
            for bit_row in range(6):
                for bit_col in range(6):
                    if CODES[identifier] & (1 << ((5 - bit_row) * 6 + 5 - bit_col)):
                        rect(x + (bit_col + 2) * 10, y + (bit_row + 2) * 10, 10, 10, 'white')
            parts.append('</g>')
    return ''.join(parts) + '</g></svg>'
