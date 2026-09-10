-- Stripe subscription billing: two new columns on installations so a
-- webhook event can find its way back to the right installation and
-- reuse the same Stripe Customer across repeat checkouts.
alter table installations add column if not exists stripe_customer_id text;
alter table installations add column if not exists stripe_subscription_id text;
