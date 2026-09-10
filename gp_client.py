"""
Microsoft Dynamics GP integration — built to spec against GP's
documented Company-database schema, 2026-09-10.

HONESTY NOTE, load-bearing for how much to trust this file: unlike
qbo_client.py, nothing in here has ever executed against a real
database. There's no free, self-service GP equivalent of Intuit's
developer sandbox — GP is a licensed, on-prem Microsoft product — so
this was built and reviewed for schema correctness, not verified live,
by the user's own explicit choice ("build to spec now, mark
unverified") after being told plainly that this gap exists and won't
close without an actual GP+SQL Server environment to test against.
Every function below discloses, specifically, which parts are
well-established GP schema knowledge (the core table/column names —
extremely well documented across the GP partner/ISV ecosystem and
Microsoft's own SDK docs) versus genuinely uncertain without a live
system to check (version-dependent quirks, real business-rule
judgment calls this hasn't been able to confirm with any actual
customer).

Architecture: runs from the LOCAL connector (connector_daemon.py), not
the Console — GP has no public API of any kind, unlike QuickBooks
Online, which the Console calls directly. Read-only, via direct SQL
Server queries against the GP company database (NOT the shared
DYNAMICS system database) — this is Microsoft's own supported pattern
for reporting-style integrations; a huge ecosystem of GP reporting/BI
tools works exactly this way. Writing to GP is a materially different,
riskier problem this does not attempt: Microsoft explicitly warns
against direct SQL writes to GP tables bypassing its own business
logic, and a real write path would need eConnect, not raw SQL.

Needs `pyodbc` (pip install pyodbc) and the Microsoft ODBC Driver for
SQL Server installed on the machine running the connector.
"""

import pyodbc  # pip install pyodbc


def build_connection_string(config: dict) -> str:
    """
    config is exactly what installer_skeleton.py's
    collect_gp_connection_config() collects and stores locally (never
    sent to the Console — see that function's docstring).
    """
    driver = "{ODBC Driver 18 for SQL Server}"
    base = f"DRIVER={driver};SERVER={config['host']},{config['port']};DATABASE={config['database']};"
    if config.get("windows_auth", True):
        return base + "Trusted_Connection=yes;"
    return base + f"UID={config['username']};PWD={config['password']};"


def _connect(connection_string: str) -> "pyodbc.Connection":
    return pyodbc.connect(connection_string, timeout=10)


def fetch_open_invoices(connection_string: str) -> list[dict]:
    """
    RM20101 (Receivables Management Open Transactions) — normalized
    into the exact shape ar_agent.py's _fetch_open_invoices() has
    documented since before any real adapter existed for any ERP:
    {"invoice_id", "reference", "amount_due", "due_date",
    "bill_to_contact", "customer_id"}.

    Well-established: RM20101 is GP's real open-transactions table;
    CUSTNMBR/DOCNUMBR/CURTRXAM/DUEDATE/RMDTYPAL are its real, stable
    column names. CURTRXAM (not ORTRXAMT) is deliberate — it's what's
    actually still owed on a partially-paid document, not the original
    billed amount, same reasoning qbo_client.py's Balance-vs-TotalAmt
    choice already documents.

    A real business-rule judgment call this can't verify: RMDTYPAL = 1
    filters to Sales/Invoices specifically (GP's document type codes:
    1=Invoice, 2=Debit Memo, 3=Finance Charge, 4=Service/Repairs,
    5=Warranty, 6=Credit Memo, 7=Payment, 8=Return). Whether Debit
    Memos/Finance Charges should also count as "open AR" for a given
    tenant's own dunning/reporting purposes is a real decision that
    would need to be confirmed against that tenant's actual GP setup,
    not guessed here.

    bill_to_contact is left None here on purpose — see fetch_customers()
    below for why, and _process_dunning_for_invoice()'s existing
    customer-level fallback (built for QBO, applies unchanged here)
    for how that gets resolved anyway.

    GP's older CHAR (not VARCHAR) columns commonly carry trailing
    whitespace — the .strip() calls below are standard practice for any
    real GP SQL integration, not a guess.
    """
    query = """
        select CUSTNMBR, DOCNUMBR, CURTRXAM, DUEDATE
        from RM20101
        where RMDTYPAL = 1 and VOIDSTTS = 0 and CURTRXAM > 0
    """
    conn = _connect(connection_string)
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        return [
            {
                "invoice_id": row.DOCNUMBR.strip(),
                "reference": row.DOCNUMBR.strip(),
                "amount_due": float(row.CURTRXAM),
                "due_date": row.DUEDATE.date().isoformat() if row.DUEDATE else None,
                "bill_to_contact": None,
                "customer_id": row.CUSTNMBR.strip(),
            }
            for row in rows
        ]
    finally:
        conn.close()


def fetch_customers(connection_string: str) -> list[dict]:
    """
    RM00101 (Customer Master) — normalized into the same shape
    qbo_client.py's fetch_customers() already established:
    {"customer_id", "display_name", "email", "phone"}.

    Genuinely uncertain without a live system to check: GP's own email
    storage on the customer record is inconsistent across versions and
    customizations — some installs keep it on RM00101 itself (the
    INET1 column, attempted below), others move it to a separate
    Internet Addresses table (commonly RM00301, but this varies enough
    by version/ISV customization that this doesn't attempt to join
    against it). If INET1 doesn't exist or is blank on a given
    install's RM00101, email comes back None here — an honest gap,
    not a guess dressed up as a real value.
    """
    query = """
        select CUSTNMBR, CUSTNAME, PHNUMBR1, INET1
        from RM00101
        where INACTIVE = 0
    """
    conn = _connect(connection_string)
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        return [
            {
                "customer_id": row.CUSTNMBR.strip(),
                "display_name": row.CUSTNAME.strip() if row.CUSTNAME else None,
                "email": row.INET1.strip() if getattr(row, "INET1", None) else None,
                "phone": row.PHNUMBR1.strip() if row.PHNUMBR1 else None,
            }
            for row in rows
        ]
    finally:
        conn.close()


def fetch_credit_limits(connection_string: str) -> list[dict]:
    """
    Also RM00101, via CRLMTAMT/CRLMTTYP — GP has a REAL, native credit
    limit field, unlike QuickBooks Online, which had none at all (the
    reason credit limits stayed manual-entry-only for QBO tenants).
    CRLMTTYP = 0 means "no limit enforced" in GP; mapped to None here
    rather than 0, since a real 0 would incorrectly read as "zero
    credit allowed," which isn't what an unlimited-type record means.

    Returns [{"customer_id", "credit_limit"}, ...] matching
    /v1/sync/credit-limits' existing, already-generic payload shape —
    no new server-side endpoint needed for this one.
    """
    query = """
        select CUSTNMBR, CRLMTAMT, CRLMTTYP
        from RM00101
        where INACTIVE = 0
    """
    conn = _connect(connection_string)
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        return [
            {
                "customer_id": row.CUSTNMBR.strip(),
                "credit_limit": float(row.CRLMTAMT) if row.CRLMTTYP == 1 else None,
            }
            for row in rows
            if row.CRLMTTYP == 1
        ]
    finally:
        conn.close()


def fetch_payments(connection_string: str, since: str | None = None) -> list[dict]:
    """
    RM20201 (Receivables Management Apply To) — GP's own structured
    record of which payment applies to which invoice, the same role
    QBO's Payment.Line[].LinkedTxn played for cash_application.match().
    One payment can apply against several invoices; each application
    row becomes its own entry here, same decomposition qbo_client.py's
    fetch_payments() already uses and for the same reason (match() has
    no concept of one event resolving to more than one invoice).

    Unlike QBO (where the payment referenced an invoice by an opaque
    internal Id that had to be resolved back to a human DocNumber),
    GP's APTODCNM already IS the invoice's real document number — the
    same value fetch_open_invoices() above uses as "reference" — so no
    separate resolution step is needed here at all; this can be handed
    straight to cash_application.match() as "reference".

    APTODCTY = 1 filters applications specifically against Sales/
    Invoices, matching RMDTYPAL = 1 in fetch_open_invoices() above.

    `since` (an ISO date string) matters in a way it never did for the
    other three functions here: cash_application_events is an
    append-only log (server-side), not an upsert/full-replace table
    like open_invoices/customers/credit_limits — pushing the same
    application row every sync cycle would create a brand new,
    duplicate ar_tasks/audit_log entry for the same real payment every
    time. connector_daemon.py is responsible for tracking the last
    successfully-synced GLPOSTDT locally and passing it back in here;
    this function only applies the filter, it doesn't track state.
    """
    query = """
        select DOCNUMBR, APTODCNM, APPLDAMT, GLPOSTDT
        from RM20201
        where APTODCTY = 1
    """
    params: tuple = ()
    if since:
        query += " and GLPOSTDT > ?"
        params = (since,)

    conn = _connect(connection_string)
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [
            {
                "gp_payment_doc": row.DOCNUMBR.strip(),
                "gp_line_key": f"{row.DOCNUMBR.strip()}:{row.APTODCNM.strip()}",
                "amount": float(row.APPLDAMT),
                "reference": row.APTODCNM.strip(),
                "received_date": row.GLPOSTDT.date().isoformat() if row.GLPOSTDT else None,
            }
            for row in rows
        ]
    finally:
        conn.close()
