import os
from polar_sdk import Polar
from sqlalchemy import update, select
from polar_sdk.models import WebhookOrderPaidPayload
from fastapi import APIRouter, Request, HTTPException, Depends, Path
from polar_sdk.webhooks import validate_event, WebhookVerificationError


from services.utils import get_logger, report_and_raise_500
from services.auth import get_current_user
from database import User, get_db, ProcessedOrder

router = APIRouter()

# 1. Initialize the Polar client
# Ensure your environment variables are set before running the app
polar = Polar(access_token=os.getenv("POLAR_API_KEY"))
WEBHOOK_SECRET = os.environ.get("POLAR_WEBHOOK_SECRET", "")

logger = get_logger(__name__)


# 2. Checkout Route
@router.post("/api/checkout/{product_id}")
async def buy_tokens(user=Depends(get_current_user), product_id: str = Path()):
    """Generates a checkout and tags it with your internal user ID."""

    checkout = polar.checkouts.create(
        request={
            "products": [product_id],
            "metadata": {"user_email": user.email},
            "success_url": "https://app.smartstructs.org/success?checkout_id={CHECKOUT_ID}",
        }
    )
    return {"url": checkout.url}


# 3. Webhook Fulfillment Route
@router.post("/api/webhooks/polar")
async def webhook_handler(request: Request, db=Depends(get_db)):
    """Listens for Polar events to grant access or fulfill orders."""
    try:
        payload = await request.body()

        try:
            event = validate_event(payload, dict(request.headers), WEBHOOK_SECRET)
        except WebhookVerificationError:
            raise HTTPException(status_code=403, detail="Invalid webhook signature")

        # listen for actual payments
        if isinstance(event, WebhookOrderPaidPayload):
            order_data = event.data
            product_id = order_data.product_id
            order_id = str(order_data.id)

            # check if the order has already been processed
            existing_order = await db.execute(
                select(ProcessedOrder).where(ProcessedOrder.order_id == order_id)
            )
            if existing_order.scalar_one_or_none():
                logger.exception(f"Duplicate webhook for order {order_id}, skipping.")
                return {"status": "ok"}

            user_email = order_data.metadata.get("user_email")

            # Safety check in case metadata is missing
            if not user_email:
                return {"status": "ok"}

            # Tier logic
            if product_id == "8c5e384a-9151-4da3-b5f6-41b56cbfdedc":
                to_add = 10
            elif product_id == "cda3f0dc-7cf3-440f-83a3-5a91a6c6bf20":
                to_add = 1
            elif product_id == "9bc35b45-4c14-4542-b040-42d324b90086":
                to_add = 5
            else:
                to_add = 20

            await db.execute(
                update(User)
                .where(User.email == user_email)
                .values(tokens=User.tokens + to_add)
            )
            db.add(ProcessedOrder(order_id=order_id, user_email=user_email))
            await db.commit()

            logger.log(
                level=1, msg=f"Successfully added {product_id} tokens for {user_email}"
            )

        return {"status": "ok"}

    except HTTPException as ht:
        await report_and_raise_500(
            ht, context="webhook_handler: HTTP", raise_error=False
        )
        raise

    except Exception as e:
        await report_and_raise_500(e, context="webhook_handler: Exception")
