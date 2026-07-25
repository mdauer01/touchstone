-- Executed SQL harvested from the BI tool over a reporting period — the
-- shape a warehouse QUERY_HISTORY export has. Nothing here is curated: it is
-- what analysts and dashboards actually ran, wrong joins included.

-- Revenue by segment: the well-worn customer join (many dashboards).
SELECT c.segment, SUM(f.amount) AS revenue
FROM analytics.fct_orders f
JOIN raw.customers c ON f.customer_id = c.customer_id
GROUP BY c.segment;

SELECT c.segment, COUNT(*) AS orders
FROM analytics.fct_orders f
JOIN raw.customers c ON c.customer_id = f.customer_id   -- flipped operand order
GROUP BY c.segment;

SELECT c.segment, AVG(f.amount) AS aov
FROM raw.customers AS c, analytics.fct_orders AS f      -- implicit join, 1990s style
WHERE f.customer_id = c.customer_id
GROUP BY c.segment;

-- Payment fees against the orders mart. Looks reasonable. Isn't.
SELECT SUM(p.fee) AS fees
FROM raw.payments p
JOIN analytics.fct_orders f ON p.order_id = f.order_id;

SELECT f.order_date, SUM(p.fee) AS fees
FROM analytics.fct_orders f
JOIN raw.payments p ON f.order_id = p.order_id
GROUP BY f.order_date;

-- Somebody's drag-and-drop accident that shipped to a dashboard anyway.
SELECT p.method, SUM(f.amount) AS amount
FROM raw.payments p
JOIN analytics.fct_orders f ON p.method = f.channel
GROUP BY p.method;

-- Unparseable junk in the history export (truncated log line) — mining must
-- skip it, not fail.
SELECT * FROM WHERE;
