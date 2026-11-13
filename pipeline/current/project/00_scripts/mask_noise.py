"""Non-destructive 8-connected small-region detection, in source pixels."""
from PIL import Image


def candidates(mask, max_area, exclude_border=False):
    """8-connected components via row spans, avoiding Python per-pixel walks."""
    import re
    from PIL import ImageDraw
    width, height = mask.size
    output = Image.new('L', mask.size)
    limit = int(max_area)
    if limit <= 0:
        return output, []
    data = mask.convert('L').tobytes()
    parents = []
    spans = []
    previous = []
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    pattern = re.compile(b'[^\x00]+')
    for y in range(height):
        current = []
        left = 0
        for match in pattern.finditer(data, y*width, (y+1)*width):
            x0, x1 = match.start()-y*width, match.end()-y*width-1
            label = len(parents)
            parents.append(label)
            while left < len(previous) and previous[left][1] < x0-1:
                left += 1
            j = left
            while j < len(previous) and previous[j][0] <= x1+1:
                other = root(previous[j][2])
                here = root(label)
                if here != other:
                    parents[here] = other
                j += 1
            current.append((x0, x1, label))
            spans.append((x0, x1, y, label))
        previous = current
    sizes = {}
    border = set()
    for x0, x1, y, label in spans:
        r = root(label)
        sizes[r] = sizes.get(r, 0) + x1-x0+1
        if x0 == 0 or x1 == width-1 or y == 0 or y == height-1:
            border.add(r)
    accepted = {r for r, area in sizes.items()
                if area <= limit and not (exclude_border and r in border)}
    draw = ImageDraw.Draw(output)
    for x0, x1, y, label in spans:
        if root(label) in accepted:
            draw.line((x0, y, x1, y), fill=255)
    return output, [area for r, area in sizes.items() if r in accepted]


def remove_candidates(mask, selection):
    result = mask.copy()
    result.paste(0, (0, 0), selection)
    return result


def component_at(selection, x, y):
    """Return only the 8-connected candidate at a source-image coordinate."""
    width, height = selection.size
    result = Image.new('L', selection.size)
    if not (0 <= x < width and 0 <= y < height):
        return result
    pixels = selection.load()
    if not pixels[x, y]:
        return result
    target = result.load()
    target[x, y] = 255
    stack = [(x, y)]
    while stack:
        x, y = stack.pop()
        for yy in range(max(0, y-1), min(height, y+2)):
            for xx in range(max(0, x-1), min(width, x+2)):
                if pixels[xx, yy] and not target[xx, yy]:
                    target[xx, yy] = 255
                    stack.append((xx, yy))
    return result


def source_point(x, y, size, display_size):
    """Map display coordinates back to pixels, including outside-image clicks."""
    import math
    return (math.floor(x * size[0] / display_size[0]),
            math.floor(y * size[1] / display_size[1]))


def hole_candidates(mask, max_area):
    """Only enclosed background components; even diagonal exterior paths stay open."""
    background = mask.convert('L').point(lambda v: 255 if v == 0 else 0)
    return candidates(background, max_area, exclude_border=True)


def fill_candidates(mask, selection):
    result = mask.copy()
    result.paste(255, (0, 0), selection)
    return result
