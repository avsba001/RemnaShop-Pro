"""Shared paid-status reconciliation; query results use the same identity checks."""
import asyncio
from services.zpay import PaymentError, gateway_message, validate_paid
from storage.payments import get_intent, claim_query, mark_paid


async def reconcile_payment(db_file, payment_id, config, client):
    intent = await asyncio.to_thread(get_intent, db_file, payment_id)
    if not intent or intent["status"] == "paid":
        return intent
    if not await asyncio.to_thread(claim_query, db_file, payment_id):
        return intent
    data = await client.query(payment_id)
    # A failed lookup is not a successful unpaid response. Neither permits
    # resetting the one-shot creation barrier or replaying gateway creation.
    if str(data.get("code")) != "1":
        raise PaymentError("gateway_query_rejected", gateway_message=gateway_message(data, config, payment_id))
    if str(data.get("status")) == "1":
        trade_no = validate_paid(data, intent, config, notification=False)
        await asyncio.to_thread(mark_paid, db_file, payment_id, trade_no)
    return await asyncio.to_thread(get_intent, db_file, payment_id)
