select
    date_trunc('week', f.order_date) as refund_week,
    sum(f.refund_amount) / nullif(sum(f.amount), 0) as refund_rate
from analytics.fct_orders as f
join raw.customers as c
    on c.customer_id = f.customer_id
where c.segment <> 'test'
group by date_trunc('week', f.order_date)
