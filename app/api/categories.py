from __future__ import annotations

from fastapi import APIRouter, Depends

from ..categories import delete_category, list_categories, reorder_categories, save_category
from ..repository import load_subscription
from ..schemas import ProxyCategoryIn
from .deps import require_auth


router = APIRouter(prefix="/api/subscriptions/{sub_id}/categories", dependencies=[Depends(require_auth)], tags=["categories"])


@router.get("")
def categories(sub_id: int) -> dict[str, object]:
    load_subscription(sub_id, include_secrets=False)
    return {"subscription_id": sub_id, "categories": list_categories(sub_id)}


@router.post("")
def create(sub_id: int, payload: ProxyCategoryIn) -> dict[str, object]:
    return {"ok": True, "category": save_category(sub_id, payload)}


@router.put("/reorder")
def reorder(sub_id: int, category_ids: list[int]) -> dict[str, object]:
    return {"ok": True, "categories": reorder_categories(sub_id, category_ids)}


@router.put("/{category_id}")
def update(sub_id: int, category_id: int, payload: ProxyCategoryIn) -> dict[str, object]:
    return {"ok": True, "category": save_category(sub_id, payload, category_id)}


@router.delete("/{category_id}")
def delete(sub_id: int, category_id: int) -> dict[str, object]:
    delete_category(sub_id, category_id)
    return {"ok": True}
