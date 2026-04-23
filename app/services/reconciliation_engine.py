from uuid import uuid4
from app.models.schemas import (
    RunReconciliationRequest,
    RunReconciliationResponse,
)


def queue_reconciliation_job(
    request: RunReconciliationRequest,
) -> RunReconciliationResponse:
    """
    Temporary placeholder service.
    Later this will:
    - load statement lines
    - load invoices
    - match them
    - write reconciliation results to the database
    """
    return RunReconciliationResponse(
        job_id=uuid4(),
        reconciliation_id=uuid4(),
        status="queued",
    )