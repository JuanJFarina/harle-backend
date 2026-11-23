BEGIN;

SET LOCAL search_path = public, pg_temp;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.income_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    amount NUMERIC(18, 2) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'ARS',
    effective_on DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT income_history_amount_positive CHECK (amount > 0),
    CONSTRAINT income_history_currency_valid CHECK (currency = 'ARS'),
    CONSTRAINT income_history_user_effective_unique UNIQUE (user_id, effective_on)
);

CREATE INDEX IF NOT EXISTS idx_income_history_user_effective
ON public.income_history (user_id, effective_on DESC);

CREATE TABLE IF NOT EXISTS public.finance_budget_settings (
    user_id UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    rent_percentage NUMERIC(5, 2) NOT NULL,
    essential_services_percentage NUMERIC(5, 2) NOT NULL,
    non_essential_services_percentage NUMERIC(5, 2) NOT NULL,
    home_percentage NUMERIC(5, 2) NOT NULL,
    transport_percentage NUMERIC(5, 2) NOT NULL,
    outings_percentage NUMERIC(5, 2) NOT NULL,
    shopping_percentage NUMERIC(5, 2) NOT NULL,
    other_percentage NUMERIC(5, 2) NOT NULL,
    savings_percentage NUMERIC(5, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT finance_budget_percentages_in_range CHECK (
        rent_percentage BETWEEN 0 AND 100
        AND essential_services_percentage BETWEEN 0 AND 100
        AND non_essential_services_percentage BETWEEN 0 AND 100
        AND home_percentage BETWEEN 0 AND 100
        AND transport_percentage BETWEEN 0 AND 100
        AND outings_percentage BETWEEN 0 AND 100
        AND shopping_percentage BETWEEN 0 AND 100
        AND other_percentage BETWEEN 0 AND 100
        AND savings_percentage BETWEEN 0 AND 100
    ),
    CONSTRAINT finance_budget_percentages_total CHECK (
        rent_percentage
        + essential_services_percentage
        + non_essential_services_percentage
        + home_percentage
        + transport_percentage
        + outings_percentage
        + shopping_percentage
        + other_percentage
        + savings_percentage = 100.00
    )
);

COMMIT;
