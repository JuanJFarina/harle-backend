BEGIN;

SET LOCAL search_path = public, pg_temp;

ALTER TABLE public.external_identities
    ADD COLUMN IF NOT EXISTS email TEXT;

UPDATE public.external_identities
SET email = LOWER(BTRIM(email))
WHERE email IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.external_identities'::regclass
            AND conname = 'external_identities_email_valid'
    ) THEN
        ALTER TABLE public.external_identities
            ADD CONSTRAINT external_identities_email_valid
            CHECK (
                email IS NULL
                OR (
                    email = LOWER(BTRIM(email))
                    AND BTRIM(email) <> ''
                )
            );
    END IF;
END
$$;

ALTER TABLE public.plans
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS monthly_price_ars NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS currency CHAR(3),
    ADD COLUMN IF NOT EXISTS billing_interval TEXT;

UPDATE public.plans
SET display_name = CASE code
        WHEN 'free' THEN 'Gratuito'
        WHEN 'basic' THEN 'Básico'
        WHEN 'max' THEN 'Max'
        ELSE code
    END,
    monthly_price_ars = CASE code
        WHEN 'free' THEN 0
        WHEN 'basic' THEN 5000
        WHEN 'max' THEN 15000
        ELSE 0
    END,
    currency = 'ARS',
    billing_interval = 'month'
WHERE display_name IS NULL
    OR monthly_price_ars IS NULL
    OR currency IS NULL
    OR billing_interval IS NULL;

ALTER TABLE public.plans
    ALTER COLUMN display_name SET NOT NULL,
    ALTER COLUMN monthly_price_ars SET NOT NULL,
    ALTER COLUMN currency SET NOT NULL,
    ALTER COLUMN billing_interval SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.plans'::regclass
            AND conname = 'plans_public_contract_valid'
    ) THEN
        ALTER TABLE public.plans
            ADD CONSTRAINT plans_public_contract_valid
            CHECK (
                BTRIM(display_name) <> ''
                AND monthly_price_ars >= 0
                AND currency = 'ARS'
                AND billing_interval = 'month'
            );
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.payment_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL
        REFERENCES public.users(id)
        ON DELETE CASCADE,
    plan_code TEXT NOT NULL
        REFERENCES public.plans(code),
    provider TEXT NOT NULL DEFAULT 'mercado_pago',
    provider_subscription_id TEXT UNIQUE,
    provider_updated_at TIMESTAMPTZ,
    payer_email TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_status TEXT NOT NULL,
    checkout_url TEXT,
    period_starts_at TIMESTAMPTZ,
    period_ends_at TIMESTAMPTZ,
    next_payment_at TIMESTAMPTZ,
    latest_payment_at TIMESTAMPTZ,
    latest_payment_updated_at TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT payment_subscriptions_provider_valid CHECK (
        provider = 'mercado_pago'
    ),
    CONSTRAINT payment_subscriptions_plan_valid CHECK (
        plan_code IN ('basic', 'max')
    ),
    CONSTRAINT payment_subscriptions_email_valid CHECK (
        payer_email = LOWER(BTRIM(payer_email))
        AND BTRIM(payer_email) <> ''
    ),
    CONSTRAINT payment_subscriptions_status_valid CHECK (
        status IN (
            'creating',
            'pending',
            'active',
            'past_due',
            'cancelled',
            'ended',
            'failed'
        )
    ),
    CONSTRAINT payment_subscriptions_period_valid CHECK (
        (
            period_starts_at IS NULL
            AND period_ends_at IS NULL
        )
        OR (
            period_starts_at IS NOT NULL
            AND period_ends_at > period_starts_at
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_subscriptions_user_open
ON public.payment_subscriptions (user_id)
WHERE status IN ('creating', 'pending', 'active', 'past_due', 'cancelled');

CREATE INDEX IF NOT EXISTS idx_payment_subscriptions_provider
ON public.payment_subscriptions (provider_subscription_id)
WHERE provider_subscription_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_payment_subscriptions_cancelled_period
ON public.payment_subscriptions (period_ends_at)
WHERE status = 'cancelled';

CREATE TABLE IF NOT EXISTS public.subscription_payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID NOT NULL
        REFERENCES public.payment_subscriptions(id)
        ON DELETE CASCADE,
    provider_invoice_id TEXT NOT NULL UNIQUE,
    provider_payment_id TEXT,
    status TEXT NOT NULL,
    amount NUMERIC(18, 2) NOT NULL,
    currency CHAR(3) NOT NULL,
    debit_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT subscription_payments_status_valid CHECK (
        status IN (
            'pending',
            'approved',
            'rejected',
            'cancelled',
            'refunded',
            'charged_back'
        )
    ),
    CONSTRAINT subscription_payments_amount_valid CHECK (amount > 0),
    CONSTRAINT subscription_payments_currency_valid CHECK (currency = 'ARS')
);

CREATE TABLE IF NOT EXISTS public.payment_webhook_claims (
    provider_event_id TEXT PRIMARY KEY,
    provider_request_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    CONSTRAINT payment_webhook_claims_status_valid CHECK (
        status IN ('received', 'processing', 'processed', 'failed')
    )
);

COMMIT;
