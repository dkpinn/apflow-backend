from app.services.review_queue import get_review_counts, list_review_items


def test_review_queue_excludes_statement_documents(stub_db):
    db = stub_db(
        {
            "invoices_extracted": [
                {
                    "id": "invoice-1",
                    "organisation_id": "org-1",
                    "invoice_number": "INV-1",
                    "invoice_date": "2026-07-01",
                    "total_amount": "100.00",
                    "document_type": "tax_invoice",
                    "review_status": "pending",
                    "posting_status": "unposted",
                },
                {
                    "id": "statement-1",
                    "organisation_id": "org-1",
                    "invoice_number": "STMT-1",
                    "invoice_date": "2026-07-02",
                    "total_amount": "250.00",
                    "document_type": "statement",
                    "review_status": "pending",
                    "posting_status": "unposted",
                },
            ],
        }
    )

    items = list_review_items(db, "org-1")

    assert [item["id"] for item in items] == ["invoice-1"]


def test_review_counts_exclude_statement_documents(stub_db):
    db = stub_db(
        {
            "invoices_extracted": [
                {
                    "id": "invoice-1",
                    "organisation_id": "org-1",
                    "document_type": "tax_invoice",
                    "review_status": "pending",
                    "posting_status": "unposted",
                },
                {
                    "id": "statement-1",
                    "organisation_id": "org-1",
                    "document_type": "statement",
                    "review_status": "pending",
                    "posting_status": "unposted",
                },
                {
                    "id": "statement-2",
                    "organisation_id": "org-1",
                    "document_type": "supplier statement",
                    "review_status": "approved",
                    "posting_status": "unposted",
                },
            ],
        }
    )

    assert get_review_counts(db, "org-1") == {
        "pending": 1,
        "needs_info": 0,
        "approved": 0,
        "ignored": 0,
    }
