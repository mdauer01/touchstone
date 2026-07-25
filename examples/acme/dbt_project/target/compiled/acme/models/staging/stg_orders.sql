select
    order_id,
    customer_id,
    order_date,
    lower(status) as status,
    amount,
    refund_amount,
    channel
from raw.orders
