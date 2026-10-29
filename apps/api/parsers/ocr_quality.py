"""Language-neutral OCR risk signals and bounded region geometry.

These are review heuristics, NOT accuracy estimates or table reconstruction.
No document names, business vocabulary, network calls or model inference.
"""
from dataclasses import replace
from io import BytesIO
from statistics import median

from PIL import Image


def line_risks(lines):
    reasons = []
    known = [line["confidence"] for line in lines if line.get("confidence") is not None]
    if known and sum(value < .5 for value in known) >= max(2, len(known) * .1):
        reasons.append("local_low_quality")
    tails = [line.get("low_word_ratio", 0) for line in lines]
    if tails and sum(value >= .25 for value in tails) >= max(2, len(tails) * .15):
        reasons.append("local_low_quality")
    heights = [line["word_height_px"] for line in lines if line.get("word_height_px", 0) > 0]
    if len(heights) >= 4 and median(heights) < 18:
        reasons.append("small_text")
    return sorted(set(reasons))


def ruled_grid(png):
    """Detect multiple long rules in both axes; bounded to <= 640² pixels.

    Only flags potential table structure. It never invents cells from text.
    """
    try:
        image = Image.open(BytesIO(png)).convert("L")
        image.thumbnail((640, 640))
        def rules(im):
            width, height = im.size
            data = im.tobytes()
            rows = []
            for y in range(height):
                run = longest = 0
                for value in data[y*width:(y+1)*width]:
                    run = run + 1 if value < 190 else 0
                    longest = max(longest, run)
                if longest >= max(30, width * .18):
                    rows.append(y)
            return sum(index == 0 or value > rows[index-1] + 2 for index, value in enumerate(rows))
        return rules(image) >= 3 and rules(image.transpose(Image.Transpose.TRANSPOSE)) >= 3
    except Exception:
        return False


def rotate_render(render, angle):
    if not angle:
        return render
    image = Image.open(BytesIO(render.png)).rotate(angle, expand=True)
    output = BytesIO()
    image.save(output, "PNG")
    return replace(render, png=output.getvalue(), width_px=image.width, height_px=image.height,
        page_width_pt=render.page_height_pt if angle != 180 else render.page_width_pt,
        page_height_pt=render.page_width_pt if angle != 180 else render.page_height_pt)


def regions(render):
    """Four overlapping quadrants, center ownership prevents duplicate evidence."""
    image = Image.open(BytesIO(render.png))
    width, height = image.size
    for row in range(2):
        for column in range(2):
            core = (column * width // 2, row * height // 2,
                    (column + 1) * width // 2, (row + 1) * height // 2)
            # Overlap is context only; retain lines whose center belongs to core.
            pad = min(96, max(24, min(width, height) // 20))
            x0, y0, x1, y1 = (max(0, core[0]-pad), max(0, core[1]-pad),
                              min(width, core[2]+pad), min(height, core[3]+pad))
            crop = image.crop((x0, y0, x1, y1))
            output = BytesIO()
            crop.save(output, "PNG")
            yield replace(render, png=output.getvalue(), width_px=crop.width, height_px=crop.height,
                page_width_pt=crop.width * render.page_width_pt / width,
                page_height_pt=crop.height * render.page_height_pt / height), (x0, y0), core


def restore_region(lines, render, offset, core):
    sx, sy = render.page_width_pt/render.width_px, render.page_height_pt/render.height_px
    result = []
    for line in lines:
        bbox = line.get("bbox")
        if not bbox:
            continue
        # Crop height is implicit in its bottom-left bbox; caller passes the
        # crop's bottom offset, not a top-left pixel translation.
        x0, y0, x1, y1 = bbox
        mapped = [x0+offset[0]*sx, y0+offset[1]*sy, x1+offset[0]*sx, y1+offset[1]*sy]
        cx = (mapped[0]+mapped[2])/2/sx
        cy = render.height_px-(mapped[1]+mapped[3])/2/sy
        if core[0] <= cx < core[2] and core[1] <= cy < core[3]:
            result.append({**line, "bbox": mapped, "region_ocr": True})
    return result


def prefer_candidate(original, candidate):
    """Require confidence-weighted evidence gain without a large text collapse."""
    def score(lines):
        return sum(len(line.get("text", "")) * max(0, line.get("confidence") or 0) for line in lines)
    old_chars = sum(len(line.get("text", "")) for line in original)
    new_chars = sum(len(line.get("text", "")) for line in candidate)
    def mean(lines):
        values = [line["confidence"] for line in lines if line.get("confidence") is not None]
        return sum(values)/len(values) if values else 0
    return (bool(candidate) and mean(candidate) >= max(.5, mean(original)-.05)
            and new_chars >= old_chars * .8 and score(candidate) > score(original) * 1.05)


def direction_probe(render, max_side=1600):
    """Choose rotation on a bounded thumbnail, preserving original page units."""
    if max(render.width_px, render.height_px) <= max_side:
        return render
    image = Image.open(BytesIO(render.png))
    image.thumbnail((max_side, max_side))
    output = BytesIO()
    image.save(output, "PNG")
    return replace(render, width_px=image.width, height_px=image.height, png=output.getvalue())
