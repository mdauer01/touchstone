-- Same math as total_gmv, written by a different team in a different style.
with base as (
    select *
    from analytics.fct_orders
    where channel <> 'internal' and status <> 'cancelled'
)

select
    date_trunc('month', order_date) as reporting_month,
    sum(amount) as gross_merchandise_value
from base
group by 1
