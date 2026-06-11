"""Tests for receipt image renaming and collision handling."""
from process_receipts import rename_receipt_image


def _make_img(tmp_path, name="IMG_1234.jpg"):
    p = tmp_path / name
    p.write_bytes(b"fake-jpeg")
    return p


def test_rename_pattern(tmp_path):
    img = _make_img(tmp_path)
    data = {"date": "2024-12-30", "vendor": "Chevron"}
    out = rename_receipt_image(img, data, "fuel")
    assert out.name == "fuel_12-30-24_chevron.jpg"
    assert out.exists()
    assert not img.exists()


def test_rename_moves_to_dest_dir(tmp_path):
    img = _make_img(tmp_path)
    dest = tmp_path / "processed"
    data = {"date": "2024-12-30", "vendor": "Chevron"}
    out = rename_receipt_image(img, data, "fuel", dest)
    assert out.parent == dest
    assert out.exists()


def test_rename_collision_appends_counter(tmp_path):
    data = {"date": "2024-12-30", "vendor": "Chevron"}
    out1 = rename_receipt_image(_make_img(tmp_path, "a.jpg"), data, "fuel")
    out2 = rename_receipt_image(_make_img(tmp_path, "b.jpg"), data, "fuel")
    out3 = rename_receipt_image(_make_img(tmp_path, "c.jpg"), data, "fuel")
    assert out1.name == "fuel_12-30-24_chevron.jpg"
    assert out2.name == "fuel_12-30-24_chevron_2.jpg"
    assert out3.name == "fuel_12-30-24_chevron_3.jpg"


def test_rename_missing_vendor_uses_expense_description(tmp_path):
    img = _make_img(tmp_path)
    data = {"date": "2024-12-30", "expense_description": "Parking garage"}
    out = rename_receipt_image(img, data, "misc")
    assert out.name == "misc_12-30-24_parking_garage.jpg"


def test_rename_no_date(tmp_path):
    img = _make_img(tmp_path)
    out = rename_receipt_image(img, {"vendor": "Shell"}, "fuel")
    assert out.name == "fuel_unknown_shell.jpg"


def test_rename_idempotent_when_already_named(tmp_path):
    data = {"date": "2024-12-30", "vendor": "Chevron"}
    out1 = rename_receipt_image(_make_img(tmp_path), data, "fuel")
    out2 = rename_receipt_image(out1, data, "fuel")
    assert out2 == out1
    assert out1.exists()
