from app.services.supplier_matcher import attempt_supplier_auto_link, find_supplier_match_result


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.filters = []

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        rows = self.client.tables.get(self.table_name, [])
        for key, value in self.filters:
            rows = [row for row in rows if row.get(key) == value]
        return _Result([row.copy() for row in rows])


class _Client:
    def __init__(self, *, threshold=2, amount_tiers=None, suppliers=None):
        self.tables = {
            "organisations": [{
                "id": "org-1",
                "supplier_auto_link_min_matches": threshold,
                "auto_link_amount_tiers": amount_tiers or [],
            }],
            "suppliers": suppliers or [],
        }

    def table(self, name):
        return _Query(self, name)


def _supplier(**patch):
    row = {
        "id": "supplier-1",
        "organisation_id": "org-1",
        "active": True,
        "supplier_name": "Example Supplier (Pty) Ltd",
        "trading_name": "Example Trading",
        "vat_number": "4111111111",
        "company_registration_number": "REG-1",
        "account_number": "ACC-1",
        "bank_account_number": "123456",
        "phone": "031 555 0101",
        "default_email": "accounts@example.test",
        "accounting_email": "finance@example.test",
    }
    row.update(patch)
    return row


def test_vat_alone_does_not_auto_link_when_threshold_is_two():
    client = _Client(suppliers=[_supplier()])

    assert attempt_supplier_auto_link(
        client,
        org_id="org-1",
        vat_number_extracted="411 111 1111",
    ) is None


def test_vat_and_trading_name_auto_link_when_threshold_is_two():
    client = _Client(suppliers=[_supplier()])

    assert attempt_supplier_auto_link(
        client,
        org_id="org-1",
        vat_number_extracted="411 111 1111",
        supplier_name_extracted="Example Trading",
    ) == "supplier-1"


def test_telephone_and_email_auto_link_when_threshold_is_two():
    client = _Client(suppliers=[_supplier()])

    assert attempt_supplier_auto_link(
        client,
        org_id="org-1",
        supplier_telephone_extracted="0315550101",
        supplier_email_extracted="ACCOUNTS@example.test",
    ) == "supplier-1"


def test_part_name_match_alone_only_suggests():
    client = _Client(suppliers=[_supplier()])

    result = find_supplier_match_result(
        client,
        org_id="org-1",
        supplier_name_extracted="Example Supplier",
    )

    assert result is not None
    assert result["supplier_id"] == "supplier-1"
    assert result["match_count"] == 1
    assert result["auto_link"] is False


def test_tied_candidates_do_not_auto_link():
    client = _Client(suppliers=[
        _supplier(id="supplier-1", supplier_name="Example A"),
        _supplier(id="supplier-2", supplier_name="Example B"),
    ])

    result = find_supplier_match_result(
        client,
        org_id="org-1",
        vat_number_extracted="4111111111",
        supplier_telephone_extracted="0315550101",
    )

    assert result is not None
    assert result["ambiguous"] is True
    assert result["auto_link"] is False


def test_threshold_three_requires_three_signals():
    client = _Client(threshold=3, suppliers=[_supplier()])

    assert attempt_supplier_auto_link(
        client,
        org_id="org-1",
        vat_number_extracted="4111111111",
        supplier_telephone_extracted="0315550101",
    ) is None

    assert attempt_supplier_auto_link(
        client,
        org_id="org-1",
        vat_number_extracted="4111111111",
        supplier_telephone_extracted="0315550101",
        supplier_email_extracted="accounts@example.test",
    ) == "supplier-1"


def test_amount_tier_boundary_uses_inclusive_maximum():
    client = _Client(
        threshold=2,
        amount_tiers=[
            {"max_amount": 1000, "required_matches": 2},
            {"max_amount": None, "required_matches": 4},
        ],
        suppliers=[_supplier()],
    )

    result = find_supplier_match_result(
        client,
        org_id="org-1",
        invoice_total=1000,
        vat_number_extracted="4111111111",
        supplier_telephone_extracted="0315550101",
    )
    assert result["threshold"] == 2
    assert result["auto_link"] is True

    result = find_supplier_match_result(
        client,
        org_id="org-1",
        invoice_total=1000.01,
        vat_number_extracted="4111111111",
        supplier_telephone_extracted="0315550101",
    )
    assert result["threshold"] == 4
    assert result["auto_link"] is False
