select
    date_trunc('month', order_date) as gmv_month,
    sum(amount) as total_gmv
from analytics.fct_orders
where not status = 'cancelled' and channel <> 'internal'
group by date_trunc('month', order_date)
