"""The edge-projection auto-crop must work where corner-background subtraction
failed: a receipt sitting on a non-uniform (gradient/shadowed) background.

The old detector sampled the four corners for "the background" and bailed when
the corners weren't uniform — so a gradient desk or shadow left the crop a no-op
no matter how high the aggressiveness slider went. The projection method keys off
edge energy (the receipt's sharp borders + its text), which the smooth gradient
lacks, so it still finds the receipt.
"""
from PIL import Image, ImageDraw

from process_receipts import (
    autocrop_analyze, _content_bbox_by_edges, _content_bbox_by_corner_bg,
)


def _receipt_on_gradient(size=(1000, 1000), box=(250, 200, 750, 800)):
    """White receipt with dark text bars on a top→bottom grey gradient."""
    w, h = size
    img = Image.new("L", size, 0)
    px = img.load()
    for y in range(h):                      # smooth vertical gradient: corners differ a lot
        v = int(40 + (200 - 40) * (y / h))
        for x in range(w):
            px[x, y] = v
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=255)              # the receipt paper
    for i in range(6):                      # a few "text" lines inside it
        ty = box[1] + 40 + i * 80
        d.rectangle((box[0] + 30, ty, box[2] - 30, ty + 16), fill=30)
    return img.convert("RGB")


def test_edge_detector_finds_receipt_on_gradient():
    img = _receipt_on_gradient()
    bbox = _content_bbox_by_edges(img.convert("L"), 0.3)
    assert bbox is not None
    left, top, right, bottom = bbox
    # Should land near the receipt box (250,200,750,800), not the whole frame.
    assert 220 <= left <= 280 and 720 <= right <= 780
    assert 170 <= top <= 240 and 770 <= bottom <= 830


def test_autocrop_crops_receipt_on_gradient():
    img = _receipt_on_gradient()
    info = autocrop_analyze(img)            # default aggressiveness
    assert info["would_crop"] is True
    assert 0.25 <= info["kept_ratio"] <= 0.55


def test_edge_method_is_much_tighter_than_corner_bg_on_gradient():
    # Documents *why* the method was changed: on a gradient the legacy corner-bg
    # detector treats much of the background as content, so its box is far looser
    # than the projection method's, which keys off the receipt's edges.
    img = _receipt_on_gradient().convert("L")
    w, h = img.size

    def area(b):
        return (b[2] - b[0]) * (b[3] - b[1]) / float(w * h)

    legacy = _content_bbox_by_corner_bg(img, 30)
    edges = _content_bbox_by_edges(img, 0.3)
    assert area(edges) < area(legacy) * 0.6   # projection box is far tighter
