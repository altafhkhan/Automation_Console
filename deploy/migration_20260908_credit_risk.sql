create table if not exists credit_risk_assessments (
    assessment_id       uuid primary key default gen_random_uuid(),
    installation_id     uuid not null references installations(installation_id),
    customer_id         text not null,
    exposure            numeric not null,
    credit_limit        numeric not null,
    risk_level          text not null default 'normal',
    last_recommendation text,
    last_assessed_at    timestamptz not null default now(),
    unique (installation_id, customer_id)
);
create index if not exists idx_credit_risk_assessments_installation_id on credit_risk_assessments(installation_id);
