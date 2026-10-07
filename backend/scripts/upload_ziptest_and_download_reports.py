"""
Upload ziptest.zip via bulk upload API, wait for automated processing,
and download the forensic PDF report for each case into the report/ directory.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
import httpx

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from app.core.security import create_access_token
from app.db.session import system_session
from app.models.user import User, UserRole

BASE_URL = "http://localhost:8000"
ZIP_PATH = Path("D:/Document-Authenticator-Tool/sample-documents/tampered_test_samples/ziptest.zip")
REPORT_DIR = Path("D:/Document-Authenticator-Tool/report")


def get_reviewer_token() -> tuple[str, str]:
    """Get a valid token for a reviewer in testcompany."""
    with system_session() as db:
        user = db.query(User).filter(User.email == "test1reviewerl1@gmail.com").first()
        if not user:
            user = db.query(User).filter(User.role.in_([UserRole.reviewer_l1, UserRole.reviewer_l2])).first()
        if not user:
            raise RuntimeError("No reviewer user found in database.")
        
        claims = {
            "role": user.role.value,
            "company_id": str(user.company_id) if user.company_id else None,
            "is_platform_admin": user.is_platform_admin,
        }
        token = create_access_token(str(user.id), extra_claims=claims)
        return token, user.email


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>| ]+', "_", name).strip("_")


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not ZIP_PATH.exists():
        print(f"Error: {ZIP_PATH} not found!")
        sys.exit(1)

    token, email = get_reviewer_token()
    headers = {"Authorization": f"Bearer {token}"}
    client = httpx.Client(base_url=BASE_URL, timeout=120.0)

    print(f"1. Authenticated as: {email}")
    print(f"2. Reading zip file: {ZIP_PATH} ({ZIP_PATH.stat().st_size:,} bytes)")
    zip_bytes = ZIP_PATH.read_bytes()

    print("3. Submitting bulk upload to /bulk-uploads ...")
    upload_resp = client.post(
        "/bulk-uploads",
        params={"case_type": "school_document", "filename": "ziptest.zip"},
        headers={**headers, "Content-Type": "application/zip"},
        content=zip_bytes,
    )
    if upload_resp.status_code != 202:
        print(f"Failed to upload zip: {upload_resp.status_code} {upload_resp.text}")
        sys.exit(1)

    bulk_data = upload_resp.json()
    bulk_id = bulk_data["id"]
    print(f"Bulk upload created successfully! ID: {bulk_id}")
    print(f"Plan identified {len(bulk_data.get('cases', []))} potential case folder(s).")

    print("\n4. Waiting for bulk ingestion task to unpack and create cases...")
    all_cases: list[dict] = []
    start_time = time.time()
    while True:
        resp = client.get(f"/bulk-uploads/{bulk_id}", headers=headers)
        if resp.status_code != 200:
            print(f"Warning: polling bulk upload failed: {resp.status_code}")
            time.sleep(2)
            continue
        data = resp.json()
        cases = data.get("cases", [])
        status = data.get("status")
        
        # Check if all cases have reached terminal ingestion status (completed/failed/rejected/skipped)
        pending_ingestion = [c for c in cases if c.get("status") == "pending"]
        created_cases = [c for c in cases if c.get("case_id")]
        
        print(f"   [Ingestion] Status: {status} | Total folders: {len(cases)} | Cases created: {len(created_cases)} | Pending unpack: {len(pending_ingestion)}")
        
        if not pending_ingestion or status in ("completed", "failed", "partial"):
            all_cases = cases
            break
        
        time.sleep(3)
        if time.time() - start_time > 180:
            print("Timed out waiting for zip ingestion to complete.")
            all_cases = cases
            break

    valid_cases = [c for c in all_cases if c.get("case_id")]
    print(f"\nIngestion finished. Total valid cases created: {len(valid_cases)}")
    for c in all_cases:
        print(f" - Folder: {c.get('folder')} | Status: {c.get('status')} | Case: {c.get('case_number')} ({c.get('case_id')})")

    if not valid_cases:
        print("No cases created from the zip file.")
        return

    print("\n5. Waiting for each case pipeline to complete processing & generating reports...")
    for idx, c_info in enumerate(valid_cases, 1):
        case_id = c_info["case_id"]
        case_number = c_info.get("case_number", case_id[:8])
        folder_name = c_info.get("folder", f"case_{idx}")
        print(f"\n--- [{idx}/{len(valid_cases)}] Processing Case: {case_number} ({folder_name}) ---")

        case_wait_start = time.time()
        final_case_data = None
        while True:
            c_resp = client.get(f"/cases/{case_id}", headers=headers)
            if c_resp.status_code != 200:
                print(f"   Failed to fetch case: {c_resp.status_code}")
                time.sleep(3)
                continue
            case_data = c_resp.json()
            case_status = case_data.get("status")
            docs = case_data.get("documents", [])
            doc_statuses = [d.get("processing_status") for d in docs]
            all_docs_done = all(s in ("complete", "failed") for s in doc_statuses) if docs else False

            print(f"   Status: {case_status} | Docs: {doc_statuses}")

            if case_status != "under_automated_review" and all_docs_done:
                final_case_data = case_data
                break

            # If all docs are done, wait up to 10 more seconds for scoring task
            if all_docs_done and (time.time() - case_wait_start > 15):
                final_case_data = case_data
                break

            time.sleep(4)
            if time.time() - case_wait_start > 300:
                print("   Timed out waiting for case processing. Proceeding to generate report anyway.")
                final_case_data = case_data
                break

        # Generate report
        print(f"   Generating PDF report for {case_number}...")
        gen_resp = client.post(f"/cases/{case_id}/reports", headers=headers)
        if gen_resp.status_code not in (200, 201):
            print(f"   Failed to generate report: {gen_resp.status_code} {gen_resp.text}")
            continue

        report_info = gen_resp.json()
        download_url = report_info.get("download_url")
        if not download_url:
            print("   No download URL returned.")
            continue

        # Download the report PDF
        pdf_resp = client.get(download_url)
        if pdf_resp.status_code != 200:
            # If download_url is relative
            if download_url.startswith("/"):
                pdf_resp = client.get(download_url, headers=headers)
            else:
                # Try direct get
                pdf_resp = httpx.get(download_url, timeout=60.0)

        if pdf_resp.status_code == 200:
            out_filename = f"{case_number}_{sanitize_filename(folder_name)}.pdf"
            out_path = REPORT_DIR / out_filename
            out_path.write_bytes(pdf_resp.content)
            print(f"   Saved report: {out_path} ({len(pdf_resp.content):,} bytes)")
        else:
            print(f"   Failed to download PDF: {pdf_resp.status_code}")

    print(f"\nAll done! Reports saved in: {REPORT_DIR}")


if __name__ == "__main__":
    main()
