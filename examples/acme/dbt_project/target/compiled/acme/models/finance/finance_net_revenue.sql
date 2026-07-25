-- Finance-owned net revenue: completed orders only, refunds subtracted.
with completed_orders as (
    select
        order_date,
        amount,
        refund_amount
    from analytics.fct_orders
    where status = 'completed'
)

select
    date_trunc('month', order_date) as revenue_month,
    sum(amount - refund_amount) as net_revenue
from completed_orders
group by 1
