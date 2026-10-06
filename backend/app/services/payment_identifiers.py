"""
Format checks for the payment identifiers printed on a document — IBAN and
UAE Tax Registration Number (TRN). Pure functions, no I/O; used by the
`iban_trn_validation` sub-check of app/services/field_validation_service.py.

IBAN (ISO 13616): the length fixed for its country, and the mod-97 checksum
(the whole IBAN, rearranged and read as a number, mod 97 == 1). For UAE IBANs
(AE + 2 check digits + 3-digit bank code + 16-digit account) two more
consistency checks with the rest of the document:
  - the bank code names the same bank as the printed bank name (for the bank
    codes in _AE_BANK_CODES; any other code is reported, not judged);
  - the printed account number is the tail of the IBAN's account part.

UAE TRN: 15 digits beginning with "100" (Federal Tax Authority format).
Only applied to documents that are UAE ones (an AE IBAN or AED amounts) —
a Saudi VAT number is also 15 digits, but starts and ends with 3.
"""
from __future__ import annotations

import re
from typing import Any

# ISO 13616 IBAN length per country (registry subset: the GCC, the wider
# Middle East and the countries most often seen on international invoices).
# India has no IBAN.
IBAN_LENGTHS: dict[str, int] = {
    "AE": 23, "SA": 24, "QA": 29, "KW": 30, "BH": 22, "OM": 23, "JO": 30, "LB": 28, "EG": 29, "TR": 26,
    "PK": 24, "IL": 23, "IQ": 23, "GB": 22, "IE": 22, "DE": 22, "FR": 27, "IT": 27, "ES": 24, "NL": 18,
    "BE": 16, "CH": 21, "AT": 20, "LU": 20, "PT": 25, "SE": 24, "NO": 15, "DK": 18, "FI": 18, "PL": 28,
    "CZ": 24, "GR": 27, "RO": 24, "HU": 28, "CY": 28, "MT": 31,
}
IBAN_NOT_USED = {"IN", "US", "CA", "AU", "CN", "JP", "SG", "HK"}

# UAE IBAN bank codes (characters 5-7) -> names a document may print for that
# bank. Only codes known with confidence are listed; an unlisted code is not
# judged.
_AE_BANK_CODES: dict[str, tuple[str, ...]] = {
    "003": ("ADCB", "Abu Dhabi Commercial Bank"),
    "026": ("ENBD", "Emirates NBD", "Emirates National Bank of Dubai"),
    "033": ("Mashreq", "Mashreqbank"),
    "035": ("FAB", "First Abu Dhabi Bank", "National Bank of Abu Dhabi", "NBAD"),
    "050": ("ADIB", "Abu Dhabi Islamic Bank"),
}

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize(value: str | None) -> str:
    return re.sub(r"[\s\-]", "", (value or "").translate(_ARABIC_DIGITS)).upper()


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", (value or "").translate(_ARABIC_DIGITS))


def iban_mod97_ok(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    if not rearranged.isalnum():
        return False
    number = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(number) % 97 == 1


def _bank_names_match(names: tuple[str, ...], printed: str) -> bool:
    text = re.sub(r"[^a-z0-9 ]", " ", printed.lower())
    words = set(text.split())
    for name in names:
        key = name.lower()
        if " " not in key and key in words:
            return True
        if " " in key and key in " ".join(text.split()):
            return True
    return False


def validate_iban(iban_raw: str, *, bank_name: str | None = None, account_number: str | None = None) -> dict[str, Any]:
    """{"value", "ok", "problems": [...], "notes": [...]} for one printed IBAN."""
    iban = normalize(iban_raw)
    problems: list[str] = []
    notes: list[str] = []
    country = iban[:2]
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", iban):
        problems.append(f"'{iban_raw}' is not shaped like an IBAN")
        return {"value": iban, "ok": False, "problems": problems, "notes": notes}
    if country in IBAN_NOT_USED:
        notes.append(f"{country} does not use IBANs; not validated")
        return {"value": iban, "ok": True, "problems": problems, "notes": notes}
    expected = IBAN_LENGTHS.get(country)
    if expected is None:
        notes.append(f"length for country {country} not on file")
    elif len(iban) != expected:
        problems.append(f"{len(iban)} characters, but {country} IBANs have {expected}")
    if iban_mod97_ok(iban):
        notes.append("checksum (mod 97) valid")
    else:
        problems.append("checksum (mod 97) fails")
    if country == "AE" and len(iban) == 23:
        code, bban = iban[4:7], iban[4:]
        known = _AE_BANK_CODES.get(code)
        if known and bank_name:
            if _bank_names_match(known, bank_name):
                notes.append(f"bank code {code} = {known[0]}, matching the printed bank name")
            else:
                problems.append(f"bank code {code} is {known[0]}, but the bank printed is '{bank_name}'")
        elif known:
            notes.append(f"bank code {code} = {known[0]}")
        else:
            notes.append(f"bank code {code} not on file; bank name not compared")
        account = digits(account_number)
        if account:
            if bban.endswith(account):
                notes.append(f"contains the printed account number {account}")
            else:
                problems.append(f"does not contain the printed account number {account}")
    return {"value": iban, "ok": not problems, "problems": problems, "notes": notes}


def validate_uae_trn(trn_raw: str) -> dict[str, Any]:
    value = digits(trn_raw)
    problems = []
    if len(value) != 15:
        problems.append(f"{len(value)} digits, but a UAE TRN has 15")
    elif not value.startswith("100"):
        problems.append("does not start with 100, as UAE TRNs do")
    notes = [] if problems else ["15 digits, starts with 100"]
    return {"value": value, "ok": not problems, "problems": problems, "notes": notes}
