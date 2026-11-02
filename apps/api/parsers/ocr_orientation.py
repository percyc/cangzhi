"""Bounded OCR direction correction; coordinates always map to original page."""
from dataclasses import replace
from io import BytesIO
import time
import math

from PIL import Image


def confidence(lines):
    values = [line.get("confidence") for line in lines if line.get("confidence") is not None]
    return sum(values) / len(values) if values else 0.0


def restore_boxes(lines, angle, width, height):
    def point(x, y):
        if angle == 90:
            return y, height - x
        if angle == 180:
            return width - x, height - y
        return width - y, x
    result = []
    for line in lines:
        item = dict(line)
        if line.get("bbox"):
            x0, y0, x1, y1 = line["bbox"]
            corners = [point(x, y) for x in (x0, x1) for y in (y0, y1)]
            item["bbox"] = [min(p[0] for p in corners), min(p[1] for p in corners),
                            max(p[0] for p in corners), max(p[1] for p in corners)]
        item["rotation_correction"] = angle
        result.append(item)
    return result


def correct_orientation(render, initial, recognize, *, budget_seconds):
    """Only low-confidence pages get at most three extra, time-bounded trials.

    No permanent image changes; no model calls. Prefer unchanged orientation
    unless an alternative has materially better confidence and enough text.
    """
    if confidence(initial.lines) >= 0.65:
        return initial, render, 0
    try:
        image = Image.open(BytesIO(render.png))
        image.load()
    except Exception:
        return initial, render, 0
    deadline = time.monotonic() + budget_seconds
    best, best_render, best_angle = initial, render, 0
    candidates = []
    incomplete = False
    for index, angle in enumerate((90, 270, 180)):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            incomplete = True
            break
        rotated = image.rotate(angle, expand=True)
        output = BytesIO()
        rotated.save(output, format="PNG")
        candidate_render = replace(render, png=output.getvalue(), width_px=rotated.width,
                                   height_px=rotated.height,
                                   page_width_pt=render.page_height_pt if angle != 180 else render.page_width_pt,
                                   page_height_pt=render.page_width_pt if angle != 180 else render.page_height_pt)
        # One slow direction must not consume the entire trial budget. Leave a
        # fair share for the opposite direction and upside-down text as well.
        candidate = recognize(candidate_render, remaining / (3 - index))
        incomplete = incomplete or bool(candidate.error)
        chars = sum(len(line.get("text", "").strip()) for line in candidate.lines)
        if (not candidate.error and chars >= 12 and confidence(candidate.lines) >= 0.55
                and confidence(candidate.lines) >= confidence(initial.lines) + 0.12):
            candidates.append((candidate, candidate_render, angle, chars))
    # Rank all trials against the same baseline, not against the last winner.
    # A short confidently recognised fragment must not defeat a fuller page.
    if candidates:
        most_chars = max(item[3] for item in candidates)
        ranked = sorted(candidates, key=lambda item:
            confidence(item[0].lines) * math.sqrt(item[3] / most_chars), reverse=True)
        best, best_render, best_angle, _ = ranked[0]
        score = lambda item: confidence(item[0].lines) * math.sqrt(item[3] / most_chars)
        uncertain = incomplete or (len(ranked) > 1 and score(ranked[0])-score(ranked[1]) < .10)
        if uncertain:
            best = replace(best, lines=[{**line, "orientation_uncertain": True} for line in best.lines])
    elif incomplete and best.lines:
        best = replace(best, lines=[{**line, "orientation_uncertain": True} for line in best.lines])
    if best_angle:
        best = replace(best, lines=restore_boxes(best.lines, best_angle,
                                               render.page_width_pt, render.page_height_pt))
    return best, best_render, best_angle
