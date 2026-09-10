-- Adds partial-resolution support to dispute_cases: a credit_memo/write_off
-- can now forgive less than the full disputed amount, leaving a real
-- remaining balance that dunning should resume collecting instead of
-- staying paused forever. Null until resolution; the check constraint
-- keeps it inside [0, amount] at the database level, not just in
-- application code.
alter table dispute_cases
    add column forgiven_amount numeric;

alter table dispute_cases
    add constraint dispute_cases_forgiven_amount_check
    check (forgiven_amount is null or (forgiven_amount >= 0 and forgiven_amount <= amount));
