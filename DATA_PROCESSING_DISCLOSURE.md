# Data Processing Disclosure — AR Automation Service

**Status: DRAFT. Not legal advice. Requires review by qualified counsel
before publication, incorporation into a binding DPA/ToS, or reliance
in any compliance representation.**

Purpose of this document: fill a specific gap identified 2026-09-08 —
this project's only existing tenant consent language
(`acknowledged_security_notice`, shown by the installer) discloses "we
don't store your passwords" and the reason for version collection, but
says nothing about (a) qumaxa personnel's ability to view tenant
financial data through the internal admin console, or (b) how the
tenant's own customers' ("Debtor") data is handled. This document is
the factual **Processing Schedule** content meant to slot into a
lawyer-drafted DPA framework — it deliberately does not attempt the
surrounding legal boilerplate (liability, indemnification, audit
rights, breach-notification timelines, governing law, international
transfer mechanisms), which needs a lawyer, not an engineering-informed
draft. Every factual claim below is checked against this project's
actual code as of 2026-09-08, not aspirational — see the Drafting Notes
at the end for gaps that exist in reality and must be closed, not
worded around, before publication.

---

## 1. Roles

With respect to **Tenant Data** (defined below), qumaxa acts as
processor/service provider and Tenant acts as controller/business.

With respect to **Debtor Data** (defined below) — data about Tenant's
own customers — Tenant is the controller, and qumaxa processes it only
as Tenant's sub-processor/service provider, strictly to deliver the
Service as configured by Tenant. Tenant is responsible for having its
own lawful basis and customer-facing notice covering this processing
(see Section 8).

## 2. Categories of Data Processed

**Tenant business data:** company name, signup email address, detected
or declared ERP/accounting system type and version, connector and
agent version identifiers, and the approval-routing policy configured
during installation (approver names/emails and dollar thresholds per
action type).

**Tenant financial data**, synced from Tenant's own accounting/ERP
system by the connector installed at Tenant's site: open invoice
records (amounts, due dates, references), aging and exposure figures,
cash-application match outcomes, dispute case records and amounts,
credit-risk assessments, and the resulting AR summary reports.

**Debtor data** (Tenant's own customers), to the extent it appears
within the data above or within inbound correspondence Tenant's AR
mailbox integration is configured to process: names, email addresses,
amounts owed, payment/remittance details, and the content of
collections, invoice-delivery, and dispute-related correspondence.

**Credentials — explicitly limited.** qumaxa never requests, transmits,
or stores Tenant's accounting/ERP system login credentials. Where
Tenant configures the Service's email integration, those mail server
credentials are stored only on Tenant's own locally-installed connector
and are never transmitted to or stored by qumaxa's servers.

## 3. Purpose of Processing

Solely to provide the AR Automation Service as configured by Tenant:
invoice delivery and validation, cash application matching, collections
and dunning communications, dispute and deduction tracking, credit risk
monitoring, AR reporting and forecasting, and inbound AR-correspondence
handling.

## 4. qumaxa Personnel Access

qumaxa personnel with an operational or support need may access Tenant
Data — including financial figures such as invoice amounts, aging,
exposure, and dispute amounts, and any Debtor Data contained within
it — through qumaxa's internal administrative tools, for the purposes
of providing support, operating and maintaining the Service,
troubleshooting, and fulfilling qumaxa's obligations under this
Agreement. Access is restricted to authorized qumaxa personnel and is
never made available to any other Tenant or unrelated third party.

## 5. Sub-processors

qumaxa currently uses the following sub-processor for infrastructure
hosting: **DigitalOcean, LLC** (server and database hosting). No other
third-party sub-processor is used to process Tenant Data or Debtor
Data. Where Tenant configures the Service's email integration, Tenant's
own email provider is used directly by Tenant's locally-installed
connector for sending and receiving AR-related correspondence — that
provider is Tenant's own vendor relationship, not a qumaxa
sub-processor, and qumaxa does not have access to that mailbox or its
credentials.

## 6. Security Measures

All data transmitted between Tenant's connector and qumaxa's servers is
encrypted in transit (HTTPS/TLS). Authentication tokens are
cryptographically hashed; qumaxa does not store or transmit them in raw
form. Access to qumaxa's internal administrative tools requires
authentication. Tenant's connector operates outbound-only and does not
require any inbound network access at Tenant's site.

## 7. Data Retention & Deletion

Upon a valid deletion request, qumaxa will permanently delete all
Tenant Data and Debtor Data associated with the requesting Tenant's
account — including installation records, financial and AR data,
correspondence content, and credential material — across all systems
in which it is stored. Deletion is irreversible. qumaxa retains a
record that a deletion occurred (the requesting party, the date, and
which categories of data were removed) as evidence of compliance with
this commitment; that record does not itself contain the deleted
Tenant Data or Debtor Data. qumaxa does not currently maintain backups
of Tenant Data, so no separate backup-purge process applies.

**[DRAFTING NOTE: as of 2026-09-08, deletion is performed by a qumaxa
operator on request (e.g. via a support channel), not initiated
directly by Tenant through self-service tooling — confirm with
engineering whether self-service deletion is expected before
publication, and update this section's wording ("upon a valid deletion
request") to name the actual request channel once decided (support
email, a tenant portal, etc.).]**

## 8. Tenant Responsibilities for Debtor Data

Tenant represents that it has the necessary legal basis and has
provided any required notice to its own customers (Debtors) for qumaxa
to process Debtor Data as Tenant's sub-processor for the purposes
described in Section 3. qumaxa processes Debtor Data only as directed
by Tenant and only to deliver the Service.

---

## Drafting Notes — NOT for publication, remove before this goes to counsel

- **Legal review is mandatory.** This document was drafted from the
  codebase's actual behavior, not by counsel. It is missing every
  standard DPA clause that requires legal judgment: liability
  allocation, audit rights, breach notification timelines,
  international data transfer mechanisms, term and termination, and
  governing law.
- **Section 7 update, 2026-09-08 (same day as the rest of this
  document): a real deletion mechanism now exists** — an operator can
  hard-delete a tenant's data across every table via the admin
  dashboard, with a durable `data_deletion_log` record proving it
  happened (row counts, requester, timestamp) and the connector wiping
  its own local credential files once it detects the installation is
  gone. What's *still* not built: any way for Tenant to request
  deletion directly (today it's operator-triggered only, per the
  drafting note in Section 7 itself) and any deletion of the
  `data_deletion_log` record's own metadata (which is by design — it's
  the evidence trail, not itself Tenant Data).
- **No certification exists.** Do not add SOC 2, ISO 27001, or similar
  language anywhere — none has been obtained.
- **"Encryption at rest" is deliberately absent from Section 6** —
  Postgres is not encrypted at rest today. Do not add that claim until
  it's actually true.
- **Personnel access today is realistically one person** (the
  operator) using a single shared API key, with no view-level audit
  log of who accessed what tenant's data — only agent *decisions* are
  logged, not admin dashboard *views*. Section 4's language describes
  a policy of restricted access, which is accurate, but don't let
  anyone read it as implying formal role-based access control or
  access logging exists yet — it doesn't. See
  `skills.md` in this repo for the full technical state.
- **Cross-reference:** the installer's existing `SECURITY_NOTICE`
  (installer_skeleton.py) already discloses "we never store your
  passwords" and the version-collection rationale. Consider extending
  that in-product notice to reference this document once it's
  finalized, so the disclosure exists both at install time and in the
  signed agreement — not built as part of this draft.
