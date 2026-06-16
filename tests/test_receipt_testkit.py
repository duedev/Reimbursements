"""Tests for the synthetic receipt test-bench (generator + scorer + runner)."""
from PIL import Image

import receipt_testkit as rk


def test_suite_is_stable_and_well_formed():
    suite = rk.challenge_suite()
    ids = [c.id for c in suite]
    assert len(ids) == len(set(ids)), "challenge ids must be unique"
    assert {"clean", "rotated_90", "multi_total", "missing_vendor",
            "us_date_ambiguous"} <= set(ids)
    for c in suite:
        # Ground truth is complete and well-typed.
        assert set(c.truth) == {"vendor", "date", "amount", "category"}
        assert c.truth["category"] in {"fuel", "mats", "misc"}
        assert isinstance(c.truth["amount"], (int, float))


def test_build_test_receipts_writes_pngs(tmp_path):
    manifest = rk.build_test_receipts(tmp_path)
    assert len(manifest) == len(rk.challenge_suite())
    for m in manifest:
        p = tmp_path / f"{m['id']}.png"
        assert p.exists()
        with Image.open(p) as img:
            assert img.width > 64 and img.height > 64


def test_rotation_swaps_orientation():
    # The 90° challenge must come out rotated vs. the same content unrotated.
    import dataclasses
    ch = next(c for c in rk.challenge_suite() if c.id == "rotated_90")
    rotated = rk.render_challenge(ch)
    upright = rk.render_challenge(dataclasses.replace(ch, rotate=0))
    assert (rotated.width, rotated.height) == (upright.height, upright.width)


def test_score_perfect_and_partial():
    truth = {"vendor": "Shell", "date": "2026-05-01", "amount": 52.40, "category": "fuel"}
    perfect = rk.score_extraction(truth, {"vendor": "Shell #123", "date": "2026-05-01",
                                          "amount": 52.40, "_category": "fuel"})
    assert perfect["score"] == 1.0
    assert all(perfect["fields"].values())

    wrong_amt = rk.score_extraction(truth, {"vendor": "Shell", "date": "2026-05-01",
                                            "amount": 99.99, "_category": "fuel"})
    assert wrong_amt["fields"]["amount"] is False
    assert wrong_amt["score"] < 1.0


def test_blank_vendor_scoring_rewards_not_fabricating():
    truth = {"vendor": "", "date": "2026-05-08", "amount": 19.25, "category": "misc"}
    # Leaving the vendor blank is correct here …
    assert rk.score_extraction(truth, {"vendor": "", "amount": 19.25,
                                       "date": "2026-05-08", "_category": "misc"})["fields"]["vendor"]
    # … inventing one is wrong.
    assert not rk.score_extraction(truth, {"vendor": "Butchs Grinders", "amount": 19.25,
                                           "date": "2026-05-08", "_category": "misc"})["fields"]["vendor"]


def test_amount_tolerance():
    truth = {"vendor": "X", "date": "2026-01-01", "amount": 10.00, "category": "misc"}
    assert rk.score_extraction(truth, {"vendor": "X", "amount": 10.004,
                                       "date": "2026-01-01", "category": "misc"})["fields"]["amount"]
    assert not rk.score_extraction(truth, {"vendor": "X", "amount": 10.05,
                                           "date": "2026-01-01", "category": "misc"})["fields"]["amount"]


def test_run_benchmark_with_stub_extractor(tmp_path):
    manifest = rk.build_test_receipts(tmp_path)
    truth_by_id = {m["id"]: m["truth"] for m in manifest}
    id_by_path = {m["path"]: m["id"] for m in manifest}

    # A "perfect oracle" extractor returns the ground truth → overall 100%.
    def oracle(path):
        t = truth_by_id[id_by_path[path]]
        return {"vendor": t["vendor"], "amount": t["amount"],
                "date": t["date"], "_category": t["category"]}

    result = rk.run_benchmark(manifest, oracle)
    assert result["count"] == len(manifest)
    assert result["overall"] == 1.0
    assert result["by_field"]["amount"] == 1.0
    assert "OVERALL: 100" in rk.format_scorecard(result, "oracle")
