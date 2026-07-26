"""_supplier_matching.py
Supplier name correction and auto-link helpers for the extraction pipeline.

Two responsibilities:
  _correct_extracted_supplier  — fix the extracted supplier name using document
                                  direction context (e.g. OCR picked up the
                                  recipient block instead of the issuer block).
  _attempt_supplier_auto_link  — try to match and link a supplier record using
                                  the identity-threshold matcher after extraction.
"""
from __future__ import annotations

import logging

from app.services.invoice_data_builders import (
    MISSING_SUPPLIER_VALIDATION_STATUS,
    utc_now_iso,
)
from app.services.invoice_extraction.entity_detection import name_matches_org, normalise_name
from app.services.invoice_extraction.extraction_rules import looks_like_address_value, looks_like_location_cluster
from app.services.invoice_extraction.supplier_parser import (
    extract_supplier_name,
    is_valid_supplier_candidate,
)

logger = logging.getLogger(__name__)


def _correct_extracted_supplier(
    parsed_data: dict,
    direction_result,
    text: str,
    organisation: dict | None = None,
) -> tuple[str | None, str | None]:
    """Correct the extracted supplier name using document direction context.

    Modifies *parsed_data* in place (supplier_name_extracted, validation_status,
    validation_notes, supplier_candidate_rejected).

    Returns ``(correction_reason, rejected_candidate)`` — both may be None.
    """
    original_supplier_norm = normalise_name(parsed_data.get("supplier_name_extracted"))
    issuer_norm = normalise_name(direction_result.issuer_name)
    recipient_norm = normalise_name(direction_result.recipient_name)
    supplier_correction_reason: str | None = None
    rejected_supplier_candidate: str | None = None

    # Correct the common AP extraction error where the parser picks the
    # recipient/customer block as the supplier. In APPayPal, "supplier" means
    # the invoice issuer/vendor, not the recipient/customer.
    if direction_result.issuer_name and original_supplier_norm == recipient_norm and recipient_norm:
        if direction_result.document_direction == "customer_sales_invoice":
            parsed_data["supplier_name_extracted"] = None
            supplier_correction_reason = (
                "Original supplier candidate matched the invoice recipient. "
                "Document appears to be a customer sales invoice, so supplier was cleared."
            )
        else:
            parsed_data["supplier_name_extracted"] = direction_result.issuer_name
            supplier_correction_reason = (
                "Original supplier candidate matched the invoice recipient. "
                "Supplier corrected to detected invoice issuer."
            )
    elif (
        direction_result.document_direction == "supplier_invoice_payable"
        and direction_result.issuer_name
        and not parsed_data.get("supplier_name_extracted")
    ):
        parsed_data["supplier_name_extracted"] = direction_result.issuer_name
        supplier_correction_reason = (
            "Supplier was missing. Supplier set to detected invoice issuer "
            "because selected organisation appears to be the recipient."
        )
    elif (
        direction_result.document_direction == "supplier_invoice_payable"
        and direction_result.issuer_name
        and issuer_norm
        and original_supplier_norm
        and original_supplier_norm != issuer_norm
    ):
        rejected_supplier_candidate = parsed_data.get("supplier_name_extracted")
        parsed_data["supplier_name_extracted"] = direction_result.issuer_name
        supplier_correction_reason = (
            "Supplier candidate differed from detected invoice issuer. "
            "Supplier corrected to issuer because selected organisation appears to be the recipient."
        )

    current_supplier_name = parsed_data.get("supplier_name_extracted")
    candidate_is_organisation = bool(
        current_supplier_name and name_matches_org(str(current_supplier_name), organisation or {})
    )
    candidate_is_location = bool(
        current_supplier_name
        and (
            looks_like_address_value(str(current_supplier_name))
            or looks_like_location_cluster(str(current_supplier_name))
        )
    )
    if current_supplier_name and (
        candidate_is_organisation
        or candidate_is_location
        or not is_valid_supplier_candidate(str(current_supplier_name))
    ):
        rejected_supplier_candidate = current_supplier_name
        recovered_supplier_name = direction_result.issuer_name or extract_supplier_name(text)
        recovered_is_organisation = bool(
            recovered_supplier_name and name_matches_org(recovered_supplier_name, organisation or {})
        )
        if (
            recovered_supplier_name
            and not recovered_is_organisation
            and is_valid_supplier_candidate(recovered_supplier_name)
            and not looks_like_address_value(recovered_supplier_name)
            and not looks_like_location_cluster(recovered_supplier_name)
        ):
            parsed_data["supplier_name_extracted"] = recovered_supplier_name
            supplier_correction_reason = (
                "Supplier candidate matched the selected organisation or looked like address/document metadata. "
                "Supplier recovered from the document header."
            )
        else:
            parsed_data["supplier_name_extracted"] = None
            if parsed_data.get("validation_status") != MISSING_SUPPLIER_VALIDATION_STATUS:
                parsed_data["validation_status"] = "needs_review"
            rejection_note = (
                f"Rejected supplier candidate '{rejected_supplier_candidate}' because it matched the selected "
                "organisation or looked like an address/document metadata. Manual supplier review is required."
            )
            parsed_data["validation_notes"] = (
                (parsed_data.get("validation_notes") + " " if parsed_data.get("validation_notes") else "")
                + rejection_note
            )
            parsed_data["supplier_candidate_rejected"] = True

    return supplier_correction_reason, rejected_supplier_candidate


def _attempt_supplier_auto_link(
    supabase,
    org_id: str,
    invoice_raw_id: str,
    parsed_data: dict,
    extracted_payload: dict,
) -> tuple[str | None, dict | None]:
    """Attempt to auto-link a supplier via identity-threshold matching.

    Modifies *extracted_payload["supplier_id"]* in place when a match is found.
    Returns ``(auto_linked_supplier_id, match_result)`` or ``(None, None)``.
    """
    if extracted_payload.get("supplier_id"):
        return None, None

    try:
        from app.services.supplier_matcher import find_supplier_match_result  # noqa: PLC0415

        match_result = find_supplier_match_result(
            supabase,
            org_id=org_id,
            invoice_total=parsed_data.get("total_amount"),
            supplier_name_extracted=parsed_data.get("supplier_name_extracted"),
            vat_number_extracted=parsed_data.get("vat_number_extracted"),
            company_registration_number_extracted=parsed_data.get("company_registration_number_extracted"),
            cus_code_extracted=parsed_data.get("cus_code_extracted"),
            bank_account_number_extracted=parsed_data.get("bank_account_number_extracted"),
            supplier_telephone_extracted=(
                parsed_data.get("supplier_telephone_extracted")
                or parsed_data.get("supplier_cell_extracted")
            ),
            supplier_email_extracted=parsed_data.get("supplier_email_extracted"),
            supplier_acc_email_extracted=parsed_data.get("supplier_acc_email_extracted"),
        )
        if match_result and match_result.get("auto_link"):
            matched_id = str(match_result["supplier_id"])
            extracted_payload["supplier_id"] = matched_id
            try:
                supabase.table("invoices_raw").update({
                    "supplier_id": matched_id,
                    "updated_at": utc_now_iso(),
                }).eq("id", invoice_raw_id).execute()
            except Exception:
                logger.exception("invoices_raw auto-link update failed for invoice=%s supplier=%s", invoice_raw_id, matched_id)
            logger.info("Auto-linked supplier %s via identity threshold for invoice=%s", matched_id, invoice_raw_id)
            return matched_id, match_result
    except Exception:
        logger.exception("Supplier auto-match failed for invoice=%s", invoice_raw_id)

    return None, None
