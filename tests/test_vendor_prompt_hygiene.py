"""Guard against the model defaulting the vendor to a prompt example.

The distillation/vision prompts used to include a concrete example vendor
("Lunch at Butchs Grinders"). When OCR couldn't read a vendor, the model would
echo that example as the vendor name. The prompts must (a) not name a specific
example store and (b) instruct the model to leave the vendor blank rather than
inventing one.
"""
import process_receipts as pr


PROMPTS = (
    pr._UNIFIED_DISTILLATION_TEMPLATE,
    pr._GEMMA_VISION_TEMPLATE,
)


def test_prompts_have_no_named_example_vendor():
    for tmpl in PROMPTS:
        assert "Butchs Grinders" not in tmpl
        assert "Butch" not in tmpl


def test_prompts_tell_model_not_to_invent_vendor():
    for tmpl in PROMPTS:
        low = tmpl.lower()
        assert "never guess" in low or "never guess, invent" in low
        # An empty-string fallback for an unreadable vendor must be spelled out.
        assert "empty string" in low
