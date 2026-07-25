-- Seed data for the acme demo estate (DuckDB dialect).
-- Engineered so the net_revenue contradiction is *measurable*: finance
-- (completed only, refunds subtracted) totals 84,000 while growth (pending
-- included, refunds ignored) totals 114,000 — a 30,000 / +35.7% divergence.
CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE raw.orders (
    order_id      BIGINT,
    customer_id   BIGINT,
    order_date    DATE,
    status        VARCHAR,
    amount        DECIMAL(18,2),
    refund_amount DECIMAL(18,2),
    channel       VARCHAR
);
INSERT INTO raw.orders VALUES
  (1, 1, DATE '2026-01-05', 'completed', 20000, 0,    'web'),
  (2, 1, DATE '2026-01-18', 'completed', 10000, 2000, 'web'),
  (3, 2, DATE '2026-02-03', 'completed', 15000, 0,    'web'),
  (4, 2, DATE '2026-02-14', 'completed', 25000, 4000, 'web'),
  (5, 1, DATE '2026-03-02', 'completed', 12000, 0,    'web'),
  (6, 2, DATE '2026-03-21', 'completed',  8000, 0,    'web'),
  (7, 1, DATE '2026-03-25', 'pending',   14000, 0,    'web'),
  (8, 2, DATE '2026-03-28', 'pending',   10000, 0,    'web'),
  (9, 1, DATE '2026-02-20', 'cancelled',  5000, 0,    'web');

CREATE TABLE raw.customers (
    customer_id BIGINT,
    signup_date DATE,
    segment     VARCHAR
);
INSERT INTO raw.customers VALUES
  (1, DATE '2025-06-01', 'consumer'),
  (2, DATE '2025-09-15', 'smb');

CREATE TABLE raw.payments (
    payment_id  BIGINT,
    order_id    BIGINT,
    method      VARCHAR,
    fee         DECIMAL(18,2),
    captured_at TIMESTAMP
);
-- Order 2 was paid in two installments (order_id is NOT unique here — any
-- fee-per-order join fans out). Payment 105 belongs to order 9, which is
-- cancelled and therefore absent from analytics.fct_orders; payment 106
-- references an order that no longer exists at all. Both are ordinary
-- warehouse entropy, and both break the payments↔fct_orders join that six
-- dashboards use.
INSERT INTO raw.payments VALUES
  (101, 1, 'card', 400, TIMESTAMP '2026-01-05 10:00:00'),
  (102, 2, 'card', 120, TIMESTAMP '2026-01-18 11:00:00'),
  (103, 2, 'card',  80, TIMESTAMP '2026-01-19 09:30:00'),
  (104, 3, 'ach',  150, TIMESTAMP '2026-02-03 14:00:00'),
  (105, 9, 'card',  50, TIMESTAMP '2026-02-20 16:00:00'),
  (106, 99, 'ach',  75, TIMESTAMP '2026-03-30 12:00:00');

CREATE TABLE raw.channels (
    channel_code VARCHAR,
    channel_name VARCHAR
);
INSERT INTO raw.channels VALUES
  ('web',    'Website'),
  ('retail', 'Retail Store');
