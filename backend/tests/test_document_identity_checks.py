"""IBAN / TRN validation, print-and-rescan and billing-period checks
(field_validation sub-checks iban_trn_validation, rescan_conflict,
period_quantity_consistency). Pure logic, no DB."""
import pytest

from app.services.field_validation_service import validate_fields
from app.services.payment_identifiers import iban_mod97_ok, validate_iban, validate_uae_trn

GIPA_IBAN = "AE250030000260337020001"
HEADSTART_IBAN = "AE450030010364749020001"


def _field(name, value):
    return {"field_name": name, "value": value, "confidence": 0.9, "uncertain": False}


def _validate(additional=None, *, core=None, items=None, pdf_info=None, ocr_text=None):
    fields = {"core_fields": core or {}, "additional_fields": additional or [], "line_items": items or []}
    if pdf_info is not None:
        fields["pdf_info"] = pdf_info
    return validate_fields(fields, ocr_text=ocr_text)["details"]


# --- IBAN ------------------------------------------------------------------------


@pytest.mark.parametrize("iban", [GIPA_IBAN, HEADSTART_IBAN, "GB82WEST12345698765432", "SA0380000000608010167519"])
def test_mod97_valid(iban):
    assert iban_mod97_ok(iban)


@pytest.mark.parametrize("iban", ["AE250030000260337020002", "GB82WEST12345698765433", "AE9900300000010482910482"])
def test_mod97_invalid(iban):
    assert not iban_mod97_ok(iban)


def test_reference_ibans_pass_with_bank_and_account():
    gipa = validate_iban(GIPA_IBAN, bank_name="Abu Dhabi Commercial Bank (ADCB)", account_number="260337020001")
    assert gipa["ok"], gipa
    assert "bank code 003 = ADCB, matching the printed bank name" in gipa["notes"]
    assert "contains the printed account number 260337020001" in gipa["notes"]
    headstart = validate_iban(HEADSTART_IBAN, bank_name="Abu Dhabi Commercial Bank (ADCB)", account_number="10364749020001")
    assert headstart["ok"], headstart


def test_iban_wrong_length_bank_or_account_is_reported():
    assert "22 characters, but AE IBANs have 23" in validate_iban("AE25003000026033702000")["problems"][0]
    wrong_bank = validate_iban(GIPA_IBAN, bank_name="Mashreq Bank")
    assert wrong_bank["problems"] == ["bank code 003 is ADCB, but the bank printed is 'Mashreq Bank'"]
    wrong_account = validate_iban(GIPA_IBAN, account_number="999999")
    assert wrong_account["problems"] == ["does not contain the printed account number 999999"]


def test_unlisted_bank_code_is_not_judged():
    # A valid AE IBAN whose bank code is not in the map: noted, not flagged.
    iban = "AE060991234567890123456"
    result = validate_iban(iban, bank_name="Some Bank")
    assert iban_mod97_ok(iban)
    assert result["ok"] and result["problems"] == []
    assert "bank code 099 not on file; bank name not compared" in result["notes"]


def test_countries_without_iban_are_not_validated():
    assert validate_iban("IN12ABCD0001234567")["ok"]


# --- TRN -------------------------------------------------------------------------


@pytest.mark.parametrize("trn, ok", [
    ("100216382000003", True),
    ("100 2163 8200 0003", True),
    ("١٠٠٢١٦٣٨٢٠٠٠٠٠٣", True),
    ("10021638200000", False),     # 14 digits
    ("991001928400003", False),    # does not start with 100
])
def test_uae_trn_format(trn, ok):
    assert validate_uae_trn(trn)["ok"] is ok


def test_sub_check_passes_on_gipa_identifiers():
    details = _validate(
        [
            _field("trn", "100216382000003"),
            _field("bank_name", "Abu Dhabi Commercial Bank (ADCB)"),
            _field("bank_account_number", "260337020001"),
            _field("bank_iban_number", GIPA_IBAN),
        ],
        core={"amount": {"value": 57182.5, "currency": "AED"}},
    )
    sub = details["iban_trn_validation"]
    assert sub["status"] == "pass", sub
    assert [i["kind"] for i in sub["identifiers"]] == ["IBAN", "TRN"]


def test_sub_check_flags_a_bad_checksum():
    sub = _validate([_field("iban", "AE250030000260337020002")])["iban_trn_validation"]
    assert sub["status"] == "flag" and "checksum (mod 97) fails" in sub["reason"]


def test_saudi_vat_number_is_not_judged_as_a_uae_trn():
    details = _validate([_field("vat_number", "300291849100003")], core={"amount": {"value": 10, "currency": "SAR"}})
    assert details["iban_trn_validation"]["status"] == "skipped"


def test_iban_read_from_text_when_not_extracted():
    sub = _validate(ocr_text=f"Bank: ADCB\nIBAN: {HEADSTART_IBAN}\n")["iban_trn_validation"]
    assert sub["status"] == "pass" and sub["identifiers"][0]["value"] == HEADSTART_IBAN


# --- rescan ----------------------------------------------------------------------

MFP = {"Producer": "Develop ineo+ 759", "Creator": "PPS - SFO2-A-MFP01"}


def test_phone_watermark_on_mfp_pdf_is_flagged():
    sub = _validate(pdf_info=MFP, ocr_text="... www.headstartnursery.ae Scanned with CamScanner")["rescan_conflict"]
    assert sub["status"] == "flag"
    assert sub["reason"].startswith("Printed and re-scanned")
    assert "would not be visible to error level analysis" in sub["reason"]


@pytest.mark.parametrize("pdf_info, text", [
    (MFP, "An ordinary scan with no app watermark"),                         # MFP alone
    ({"Producer": "CamScanner", "Creator": "CamScanner"}, "Scanned with CamScanner"),  # watermark alone
    ({"Producer": "Microsoft Word"}, "Adobe Scan"),                          # watermark, not an MFP
])
def test_watermark_or_mfp_alone_is_not_a_finding(pdf_info, text):
    assert _validate(pdf_info=pdf_info, ocr_text=text)["rescan_conflict"]["status"] == "pass"


@pytest.mark.parametrize("app", ["Adobe Scan", "Microsoft Lens", "Genius Scan", "TapScanner"])
def test_other_phone_apps_with_canon_mfp(app):
    sub = _validate(pdf_info={"Producer": "Canon iR-ADV C5535"}, ocr_text=f"Page 1 {app}")["rescan_conflict"]
    assert sub["status"] == "flag"


def test_rescan_skipped_without_producer_info():
    assert _validate(ocr_text="Scanned with CamScanner")["rescan_conflict"]["status"] == "skipped"


# --- billing periods ---------------------------------------------------------------


def _term(qty, due, paid=(), unit="MONTH", description="Term"):
    return {"description": description, "quantity": qty, "unit_price": 5500.0, "line_total": 5500.0 * qty,
            "unit": unit, "due_periods": list(due), "paid_periods": list(paid), "unmarked_periods": [],
            "counts_toward_total": True}


def test_headstart_term1_charges_four_months_for_three_due():
    items = [
        _term(4, ["October", "November", "December"], ["September"], description="FEE FOR TERM 1"),
        _term(3, ["January", "February", "March"], description="FEE FOR TERM 2"),
    ]
    sub = _validate(items=items)["period_quantity_consistency"]
    assert sub["status"] == "flag"
    assert "3 months listed as due (October, November, December; September marked paid), × 4 charged" in sub["reason"]
    assert "TERM 2" not in sub["reason"]


def test_matching_periods_pass_and_other_units_are_skipped():
    assert _validate(items=[_term(3, ["April", "May", "June"])])["period_quantity_consistency"]["status"] == "pass"
    assert _validate(items=[_term(2, ["April"], unit="WEEK")])["period_quantity_consistency"]["status"] == "skipped"
    assert _validate(items=[{"description": "Books", "line_total": 650.0}])["period_quantity_consistency"]["status"] == "skipped"
