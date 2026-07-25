-- Growth team's copy, built for a board deck in a hurry.
-- Counts pending orders, ignores refunds, reads the staging layer directly.
select
    date_trunc('month', o.order_date) as revenue_month,
    sum(o.amount) as net_revenue
from analytics.stg_orders as o
where o.status in ('completed', 'pending')
group by date_trunc('month', o.order_date)
order by 1
