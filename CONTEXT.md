This is a materially different model than a pure cloud multi-tenant SaaS — it's closer to how tools like Datadog's agent or Fivetran's connectors work: something installed at the customer's site, talking to a central console you control. Here's how I'd structure it.
Should the AR Agent run in the background? Yes — and here's why it has to
AR work is inherently continuous, not something a user triggers on demand: invoices post at any hour, payments land in bank feeds overnight, dunning steps are due on a schedule, the inbox needs watching all day. If the AR Agent only ran when someone opened an app, you'd miss all of that. So the connector service must run as an OS-level background service (a Windows Service or Linux systemd daemon, not a foreground app the customer has to keep open).
But there's an important split to make, given your ERP mix:
Keep the installed piece thin. Keep the AI brain central.
•	What runs locally (the installed connector): the narrow, ERP-specific logic — talking to Oracle EBS's database views/interfaces, or QuickBooks Desktop's QBXML, whatever that specific version requires. It polls or listens for changes, pushes data up, and pulls instructions down.
•	What stays in your Console (cloud): the actual AI reasoning — the Master Agent, AR Agent, and all seven sub-agents, plus the policy engine and approval routing.
1. How the AR Agent Coordinates
The AR Agent is the module-level orchestrator. It does not perform AR tasks itself — it:
•	Receives work from the Master Agent (scheduled jobs, events from integrations, or human-initiated tasks from the Console).
•	Breaks work into tasks and routes each to the right sub-agent.
•	Checks every action against the Permissions & Policies employee agent before execution (thresholds, tenant rules, approval requirements).
•	Maintains workflow state per tenant (e.g., "Invoice #1042 is at dunning step 2, promise-to-pay recorded for Friday").
•	Sends anything gated by policy to the human approval queue in the Console.
•	Reports every action and decision to Monitoring & Audit.
Golden rule carried down to every sub-agent: AI reads, drafts, classifies, and prioritizes. Deterministic policy code calculates, decides, and executes. Humans approve high-risk actions.
________________________________________
2. The Seven Sub-Agents
2.1 Invoice Delivery & Validation Sub-Agent
Invoice creation stays in the customer's accounting/ERP system — that is their system of record and we should not duplicate billing logic. This sub-agent makes sure invoices actually reach customers and are correct on the way out.
	
Does	Pulls newly posted invoices from the ERP; validates completeness (PO number present, correct bill-to contact, terms match contract); delivers via email or customer portal; confirms delivery; retries or flags bounces; handles "please resend invoice / statement" requests routed from the Inbox Sub-Agent.
Deterministic	Which invoices to send, when, to whom; validation rules; retry schedule.
AI-assisted	Detecting likely errors a rule can't catch (wrong entity name, mismatched contract reference); drafting the cover email.
Approval gates	None for standard delivery. Any correction to an invoice goes back to the ERP through a human.
Integrations	Accounting/ERP (read), email (send), CRM (contacts).
2.2 Cash Application Sub-Agent
	
Does	Ingests bank feeds, lockbox files, and remittance advices; matches payments to open invoices; posts matched cash to the ERP; parks exceptions (partial pays, overpays, unidentified cash) in a worklist with a suggested resolution.
Deterministic	Match rules and tolerances (exact amount, invoice ref, amount-within-tolerance, multi-invoice sum); posting logic; FX conversion; what counts as an exception.
AI-assisted	Parsing messy remittance advices (scanned PDFs, free-text emails); proposing matches for exceptions with a confidence score — proposals only, never auto-posted below the tenant's confidence threshold.
Approval gates	Posting any AI-proposed match below the tenant-configured confidence/tolerance threshold; writing off residual balances; applying unidentified cash.
Integrations	Banking (read), ERP (read open AR, post receipts), email (remittance inbox).
2.3 Collections & Dunning Sub-Agent
	
Does	Runs aging analysis per tenant; executes the tenant's dunning sequence (reminder → follow-up → escalation letters); drafts personalized outreach using invoice and customer context; records and tracks promises-to-pay; builds a prioritized daily worklist for human collectors; pauses sequences automatically when a dispute is open or a payment lands.
Deterministic	Aging buckets; dunning schedule and step timing; late-fee/interest calculation; when a sequence pauses or escalates; do-not-contact rules.
AI-assisted	Tone and wording of reminders (within approved templates); summarizing account history before a call; ranking accounts by likelihood-to-pay and value at risk.
Approval gates	First-time activation of a dunning sequence for a customer (tenant-configurable); any message deviating from approved templates; final-demand letters; referral to third-party collections; payment plan agreements.
Integrations	ERP (aging data), email (send), CRM (contact history, activity logging).
2.4 Dispute & Deduction Sub-Agent
	
Does	Detects short-pays and disputes (from Cash Application exceptions and inbound emails); classifies the reason (pricing, quantity, damaged goods, tax, duplicate billing); opens a dispute case; routes it to the right internal owner; tracks aging of the dispute itself; drafts resolution communications; prepares credit memo requests.
Deterministic	Dispute case lifecycle and SLA timers; routing rules by reason code and amount; which resolutions are even allowed per policy.
AI-assisted	Reading the customer's claim and classifying the reason code; summarizing evidence from both sides; drafting the proposed resolution.
Approval gates	Every credit memo and write-off requires human approval — no exceptions in V1. Amount-tiered approvers (e.g., analyst < $500, controller < $5,000, CFO above).
Integrations	ERP (invoices, credit memos), email, CRM/ticketing.
2.5 Credit Risk Sub-Agent
	
Does	Monitors exposure vs. credit limits; watches payment-behavior trends (slipping DSO, broken promises); recommends credit holds, releases, and limit changes; flags new-order risk when a customer is over limit.
Deterministic	Exposure math; limit checks; the scoring formula itself (weights are configuration, not LLM output).
AI-assisted	Narrative risk summaries ("payments have stretched from 32 to 58 days over two quarters; two broken promises-to-pay"); surfacing external signals if the tenant enables a credit-data integration.
Approval gates	All credit limit changes, holds, and releases are recommendations only — a human executes or approves them.
Integrations	ERP (exposure, orders), optional credit bureaus.
2.6 Customer Inbox Sub-Agent
The shared front door for inbound AR communication.
	
Does	Monitors the tenant's AR mailbox; classifies each message (remittance advice, invoice copy request, dispute, payment promise, W-9/tax doc request, general question); routes it to the right sub-agent; drafts replies for routine requests; escalates anything angry, legal-sounding, or unclassifiable to a human.
Deterministic	Routing map from classification → sub-agent; auto-reply eligibility rules; escalation triggers.
AI-assisted	Classification; extraction (invoice numbers, amounts, dates); reply drafting.
Approval gates	Tenant-configurable: start in "draft-only" mode (human sends everything), graduate to auto-send for whitelisted request types. Legal threats and complaints always escalate.
Integrations	Email (read/send), CRM.
2.7 AR Reporting & Forecast Sub-Agent
	
Does	Produces aging summaries, DSO, CEI (collection effectiveness), dispute aging, cash-inflow forecasts from due dates + promises + payment-behavior history; feeds Console dashboards; answers natural-language questions ("what's driving the jump in 60+ days?").
Deterministic	Every metric calculation and the forecast model itself. Numbers must be reproducible and auditable — an LLM never computes a figure.
AI-assisted	Explaining the numbers in plain English; anomaly narration; drafting the weekly AR summary email.
Approval gates	None — read-only. External sharing of reports follows tenant permission rules.
Integrations	ERP (read), Console (dashboards).
3. Design How They Work Together
4. Version tracking — build this as a first-class data model, not an afterthought
Given your plan to expand from EBS versions → Oracle Fusion Cloud → other systems, every installation record needs to carry a full version fingerprint, not just "which customer":
Field	Example
tenant_id	acme-corp
erp_type + erp_version	Oracle EBS / 10.2.8
connector_module + connector_version	oracle-ebs-adapter / v1.4.2
ar_agent_version	v1.0
sub_agent_versions	cash-application: v1.1, collections: v1.0, ...
policy_config_version	v3 (tenant edited approval routing)
install_date, last_heartbeat	for detecting dead/stale installs
demo_start, demo_end	start + 7 days
plan, payment_date, license_status	trial / active / expired
When an error comes in, it should be tagged with this entire fingerprint — not just "customer X had an error." That's what lets you say "this only breaks on EBS 10.2.8 with connector v1.4.2" instead of guessing. This slots directly into the Version Management and Monitoring & Audit employee agents you already have in the architecture — it's a refinement of those, not a new module.
As you add ERP types, treat each one as a pluggable adapter with its own declared support status (alpha/beta/stable), tested version range, and known-issues log — a living compatibility matrix rather than hardcoded logic.
Approval routing, tenant-configurable at install 
Since that's confirmed, I'd add one specific step to the installer questionnaire: after collecting ERP/tool info, the installer should walk the customer through defining their approval roles (whatever titles they actually use — analyst/controller/CFO or their own), a threshold per action type (credit memo amount tiers, credit limit changes, dunning-sequence activation, etc.), and named approver emails. That gets stored as a versioned policy config per tenant — editable later from the Console, not locked in at install time.
Manual email collection breaks the thing version-tracking exists for. If a human types "Oracle EBS 10.2" into an email and someone at your end keys it into a spreadsheet, you've already lost the precision you need — is it 10.2.8 or 10.2.9? Patch level? Free text drifts, gets stale the moment the customer upgrades, and can't be trusted to correlate against an error six weeks later. The entire reason you asked me to design version fingerprinting was so a support engineer could say "this only breaks on 10.2.8" — that only works if the version came from the system itself, not from someone's memory of an email thread.
So: the installer self-reports, automatically, at install time — with marketing email capture feeding it from the top, not replacing it.
The two options combine like this:
Stage	Role
Marketing → lead capture	Gets the email address, creates a prospect record, sends the installer download link. This is identity and distribution — it's how the customer finds you and how you re-contact them.
Installer → self-reporting	At install time, auto-detects what it can (registry keys, config files, DB version tables) and falls back to asking the human to confirm/select when it can't detect reliably (e.g., QuickBooks Desktop version isn't always readable pre-connection). This is technical truth — it's what feeds your version matrix and error correlation.
Email capture answers "who is this customer and how do I reach them." The installer answers "what exact system am I integrating with." Don't make one do the other's job.
One framing choice baked into the notice text worth flagging: I wrote the version-necessity line as a benefit to the customer ("this is what makes support effective") rather than a bare data-collection justification. That tends to land better and reduces the temptation to fudge the version — but if your legal/compliance side wants specific regulatory language (e.g. GDPR-style data-use disclosure) instead of this conversational tone, that's a different draft and worth having someone with that background review before it ships.
What's now baked into the design:
•	Expiry is a hard stop. connector_should_run: false in the entitlements response the moment license_status hits expired — no read-only fallback, full stop.
•	License watcher job (Section 6, and run_license_watcher() in the API scaffold): runs hourly, sends up to 3 reminder emails on a configurable day-threshold schedule, BCCs your address on every one, and is idempotent (renewal_reminders_sent prevents duplicate sends). Resets to zero on successful payment so the next term starts its own countdown.
•	Major-version-only accepted. The installer now offers "not sure of the exact patch?" as a real path — it records e.g. 10.2.x, sets needs_version_follow_up = true, and doesn't block the install. That flag is meant to surface as a worklist in the Console so those records get pinned down before they're needed for real triage, not forgotten.
•	Outbound-only confirmed as a config value (connectivity_mode), not a structural assumption — so if it ever needs to flip for one tenant, that's a setting, not a redesign.
The fix — two tokens instead of one, with different lifespans:
1.	Access token — short-lived (e.g. 1 hour). This is what's actually sent with every heartbeat/sync call. If it leaks, the damage window is small — it stops working on its own within the hour.
2.	Refresh token — longer-lived (e.g. 30–90 days), stored more carefully. Its only job is: when the access token expires, the connector quietly uses the refresh token to get a new access token, with no human involved.
So the connector never has to ask the customer to re-enter anything, but a leaked access token self-destructs quickly, and even a leaked refresh token can be revoked from your Console (kicking that one installation off) without you having to reissue the customer's whole registration.

Three files, both syntax-checked:
•	installation-registration-design.md — the full flow (lead capture → auto-detecting installer → registration → background service → licensing), the auto-detection strategy per ERP, the data model (SQL for tenants, installations, policy_configs, error_events — note error_events denormalizes the full version fingerprint onto every row on purpose), and the API contract.
•	console_registration_api.py — a runnable FastAPI scaffold for /v1/leads, /v1/registrations, /v1/heartbeat, /v1/errors, /v1/billing/webhook. In-memory storage stands in for the real DB so you can wire it up and test the flow shape before committing to Postgres.
•	installer_skeleton.py — the customer-side installer: tries auto-detection first (detect_erp()), falls back to asking, runs the approval-routing questionnaire, registers with the console, and stubs out background-service registration for both systemd and Windows.
Three files explanation:
•	Installer now shows the security & data-use notice as Step 0, before any code, ERP info, or policy data is collected — the disclosure comes first, not as fine print at the end.
store_token now notes that the background connector daemon, not the installer itself, owns refreshing this pair before the hour is up.
•	Design doc documents this as Step 0 in the flow and adds an acknowledged_security_notice audit trail rationale — you get a timestamped record that the disclosure was shown, which is useful both in support conversations and if a customer's IT/security team ever asks for proof.
New Section 9 documents the 1h/30d split and the rotate-on-use pattern; the decisions log (Section 10) is now fully green.
•	Registration API now requires acknowledged_security_notice: true before it will create an installation record, and stores the timestamp — so this isn't just a UI nicety, it's enforced server-side.
Real access/refresh token pairs with actual expiry checks (_require_installation now rejects an expired access token and points the connector at /v1/token/refresh), plus a working /v1/token/refresh endpoint that rotates the refresh token on every use so a captured old one can't be replayed.
