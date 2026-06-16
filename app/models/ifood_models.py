from pydantic import BaseModel, Field
from typing import Optional, List


# ── Token ──────────────────────────────────────────────────────

class IFoodToken(BaseModel):
    """iFood OAuth 2.0 token response."""
    access_token: str
    expires_in: int = 3600
    token_type: str = "Bearer"


# ── Delivery Address ──────────────────────────────────────────

class IFoodDeliveryAddress(BaseModel):
    """iFood delivery address data."""
    street: Optional[str] = None
    number: Optional[str] = None
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipCode: Optional[str] = None
    complement: Optional[str] = None
    reference: Optional[str] = None
    recipientName: Optional[str] = None
    phone: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# ── Payment ───────────────────────────────────────────────────

class IFoodPayment(BaseModel):
    """iFood payment information."""
    method: Optional[str] = None
    value: Optional[float] = 0.0
    prepaid: Optional[bool] = False
    change_for: Optional[float] = None
    target_wallet: Optional[str] = None


# ── Customer ──────────────────────────────────────────────────

class IFoodCustomer(BaseModel):
    """iFood customer data."""
    id: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    addresses: Optional[List[IFoodDeliveryAddress]] = None
    document: Optional[str] = None


# ── Item / Product ────────────────────────────────────────────

class IFoodItem(BaseModel):
    """iFood order item (product line)."""
    id: Optional[str] = None
    name: Optional[str] = None
    sku: Optional[str] = None
    quantity: Optional[int] = 1
    unitPrice: Optional[float] = 0.0
    totalPrice: Optional[float] = 0.0
    observation: Optional[str] = None
    category: Optional[dict] = None
    extras: Optional[List[dict]] = Field(default_factory=list)
    options: Optional[List[dict]] = Field(default_factory=list)
    index: Optional[int] = None


# ── Order ─────────────────────────────────────────────────────

class IFoodOrder(BaseModel):
    """iFood order data."""
    id: Optional[str] = None
    displayId: Optional[str] = None
    createdAt: Optional[str] = None
    orderType: Optional[str] = None  # "DELIVERY" or "TAKEOUT"
    customer: Optional[IFoodCustomer] = None
    items: Optional[List[IFoodItem]] = Field(default_factory=list)
    deliveryAddress: Optional[IFoodDeliveryAddress] = None
    payment: Optional[IFoodPayment] = None
    total: Optional[float] = 0.0
    subtotal: Optional[float] = 0.0
    deliveryFee: Optional[float] = 0.0
    serviceFee: Optional[float] = 0.0
    status: Optional[str] = None
    merchantId: Optional[str] = None
    orderId: Optional[str] = None
    customerId: Optional[str] = None
    isSchedule: Optional[bool] = False
    scheduleDate: Optional[str] = None
    finishTimer: Optional[int] = None
    preparationTime: Optional[int] = None


# ── Webhook Event ─────────────────────────────────────────────

class IFoodWebhookEvent(BaseModel):
    """iFood webhook event payload."""
    eventType: Optional[str] = None  # orderCreated, orderStatusChanged, etc.
    orderId: Optional[str] = None
    merchantId: Optional[str] = None
    createdAt: Optional[str] = None
    newStatus: Optional[str] = None
    status: Optional[str] = None

    class Config:
        """Allow extra fields from iFood that we might not model."""
        extra = "allow"


# ── Catalog ───────────────────────────────────────────────────

class IFoodCatalogItem(BaseModel):
    """iFood catalog item."""
    id: Optional[str] = None
    name: Optional[str] = None
    sku: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = 0.0
    category: Optional[str] = None
    available: Optional[bool] = True
    image: Optional[str] = None
