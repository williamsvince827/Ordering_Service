from typing import List, Dict
from decimal import Decimal, ROUND_HALF_UP

# Promo Priority Ranking (higher = better)
PROMO_PRIORITY = {
    "bogo": 3,
    "percentage": 2,
    "fixed": 1
}

def promo_matches_item(promo, item):
    """
    Checks if a promotion matches a specific cart item.
    """
    if promo["applicationType"] == "all_products":
        return True

    if (
        promo["applicationType"] == "specific_products" and
        item["product_name"] in promo["selectedProducts"]
    ):
        return True

    if (
        promo["applicationType"] == "specific_categories" and
        item["product_category"] in promo["selectedCategories"]
    ):
        return True

    return False

def meets_min_quantity(promo, item):
    """
    Checks if an item meets the minimum quantity requirement for a promotion.
    """
    if not promo.get("minQuantity"):
        return True
    return item["quantity"] >= promo["minQuantity"]

def compute_discount(promo, item):
    """
    Computes the discount amount for a specific promotion and item.
    """
    qty = item["quantity"]
    price = Decimal(str(item["price"]))
    total = qty * price

    if promo["promotionType"] == "fixed":
        discount_value = Decimal(str(promo["promotionValue"]))
        return min(discount_value, total)

    if promo["promotionType"] == "percentage":
        percent = Decimal(str(promo["promotionValue"])) / Decimal("100")
        return total * percent

    if promo["promotionType"] == "bogo":
        buy = promo["buyQuantity"]
        get = promo["getQuantity"]
        sets = qty // (buy + get)
        free_qty = sets * get
        
        if promo.get("bogoDiscountType") == "percentage":
            percent = Decimal(str(promo.get("bogoDiscountValue", 100))) / Decimal("100")
            return price * free_qty * percent
        else:
            # Fixed discount per free item
            discount_value = Decimal(str(promo.get("bogoDiscountValue", price)))
            return discount_value * free_qty

    return Decimal("0.00")

def select_best_promo(item, promos):
    """
    Selects the best promotion for a given item based on priority and discount amount.
    Returns (promo, discount) tuple or (None, 0) if no valid promos.
    """
    valid_promos = []
    
    # Check if this item was selected with BOGO intent
    is_bogo_selected = item.get("is_bogo_selected", False)

    for promo in promos:
        # If item was selected as BOGO, ONLY apply BOGO promos (skip percentage/fixed)
        if is_bogo_selected and promo["promotionType"] != "bogo":
            continue
        
        # If item was NOT selected as BOGO, skip BOGO promos (only apply percentage/fixed)
        if not is_bogo_selected and promo["promotionType"] == "bogo":
            continue
            
        if not promo_matches_item(promo, item):
            continue
        if not meets_min_quantity(promo, item):
            continue

        discount = compute_discount(promo, item)
        if discount <= 0:
            continue

        valid_promos.append({
            "promo": promo,
            "discount": discount,
            "priority": PROMO_PRIORITY.get(promo["promotionType"], 0)
        })

    if not valid_promos:
        return None, Decimal("0.00")

    # Sort by priority first (higher is better), then by discount amount (higher is better)
    best = sorted(
        valid_promos,
        key=lambda x: (x["priority"], x["discount"]),
        reverse=True
    )[0]

    return best["promo"], best["discount"]

def apply_promotions(cart_items: List[dict], promos: List[dict]) -> dict:
    """
    Applies the best promotion to each cart item.
    Handles both same-product and cross-product BOGOs.
    Returns detailed breakdown with per-item discount information.
    """
    result = []
    subtotal_discount = Decimal("0.00")
    
    # Separate BOGO items from regular items
    bogo_items = [item for item in cart_items if item.get("is_bogo_selected", False)]
    regular_items = [item for item in cart_items if not item.get("is_bogo_selected", False)]
    
    # Process BOGO items first (try to match cross-product BOGO)
    if bogo_items:
        bogo_processed = process_cross_product_bogo(bogo_items, promos)
        result.extend(bogo_processed["items"])
        subtotal_discount += Decimal(str(bogo_processed["discount"]))
    
    # Process regular items with normal promo logic
    for item in regular_items:
        promo, discount = select_best_promo(item, promos)

        price = Decimal(str(item["price"]))
        qty = item["quantity"]
        original_total = price * qty
        final_total = original_total - discount

        subtotal_discount += discount

        result.append({
            "product_name": item["product_name"],
            "original_total": float(original_total.quantize(Decimal("0.01"))),
            "discount": float(discount.quantize(Decimal("0.01"))),
            "final_total": float(max(final_total, Decimal("0.00")).quantize(Decimal("0.01"))),
            "applied_promo": {
                "promotionName": promo.get("promotionName"),
                "promotionType": promo.get("promotionType"),
                "promotionValue": promo.get("promotionValue")
            } if promo else None
        })

    final_subtotal = sum(Decimal(str(i["final_total"])) for i in result)

    return {
        "items": result,
        "subtotal_discount": float(subtotal_discount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "final_subtotal": float(final_subtotal.quantize(Decimal("0.01")))
    }

def process_cross_product_bogo(bogo_items: List[dict], promos: List[dict]) -> dict:
    """
    Process cross-product BOGO items.
    Can handle multiple different BOGO deals in the same cart.
    Returns dict with items array and total discount.
    """
    result_items = []
    total_discount = Decimal("0.00")
    unmatched_items = list(bogo_items)
    
    # Try to match BOGO promos one by one
    for promo in promos:
        if promo["promotionType"] != "bogo":
            continue
        if promo["applicationType"] != "specific_products":
            continue
        
        selected_products = set(promo.get("selectedProducts", []))
        buy_qty = promo.get("buyQuantity", 1)
        get_qty = promo.get("getQuantity", 1)
        
        # Find items that match this promo's selected products
        matching_items = [item for item in unmatched_items if item["product_name"] in selected_products]
        
        # Get unique product names in matching items
        matching_product_names = set([item["product_name"] for item in matching_items])
        
        # Check if we have enough items for this BOGO deal
        required_products = buy_qty + get_qty
        
        # For cross-product BOGO: need different products
        # For same-product BOGO: can be same product with qty >= buy+get
        if len(matching_items) >= required_products:
            # Check if it's cross-product (multiple different products) or same-product
            is_cross_product = len(matching_product_names) > 1
            
            if is_cross_product and len(matching_product_names) >= required_products:
                # Cross-product BOGO: need at least buy+get different products
                print(f"[PROMO ENGINE] Found matching cross-product BOGO: {promo.get('promotionName')}")
                print(f"[PROMO ENGINE] Matching products: {matching_product_names}")
                
                discount_value = Decimal(str(promo.get("bogoDiscountValue", 100)))
                
                # Apply discount - sort by price and discount the cheaper items
                sorted_matching = sorted(matching_items[:required_products], key=lambda x: x["price"])
                
                for idx, item in enumerate(sorted_matching):
                    price = Decimal(str(item["price"]))
                    qty = item["quantity"]
                    original_total = price * qty
                    
                    if idx >= buy_qty:
                        # This is a "get" item
                        discount_percent = discount_value / Decimal("100")
                        discount = original_total * discount_percent
                        total_discount += discount
                        print(f"[PROMO ENGINE] {item['product_name']} - GET item - Discount: ₱{discount}")
                    else:
                        # This is a "buy" item (no discount)
                        discount = Decimal("0.00")
                        print(f"[PROMO ENGINE] {item['product_name']} - BUY item - No discount")
                    
                    final_total = original_total - discount
                    
                    result_items.append({
                        "product_name": item["product_name"],
                        "original_total": float(original_total.quantize(Decimal("0.01"))),
                        "discount": float(discount.quantize(Decimal("0.01"))),
                        "final_total": float(max(final_total, Decimal("0.00")).quantize(Decimal("0.01"))),
                        "applied_promo": {
                            "promotionName": promo.get("promotionName"),
                            "promotionType": promo.get("promotionType"),
                            "promotionValue": promo.get("promotionValue")
                        }
                    })
                    
                    # Remove from unmatched
                    unmatched_items.remove(item)
                    
            elif not is_cross_product and matching_items[0]["quantity"] >= required_products:
                # Same-product BOGO: single product with enough quantity
                print(f"[PROMO ENGINE] Found matching same-product BOGO: {promo.get('promotionName')}")
                
                item = matching_items[0]
                price = Decimal(str(item["price"]))
                qty = item["quantity"]
                
                # Calculate BOGO discount for same product
                sets = qty // required_products
                free_qty = sets * get_qty
                
                discount_value = Decimal(str(promo.get("bogoDiscountValue", 100)))
                discount_percent = discount_value / Decimal("100")
                discount = price * free_qty * discount_percent
                total_discount += discount
                
                original_total = price * qty
                final_total = original_total - discount
                
                print(f"[PROMO ENGINE] {item['product_name']} - {sets} sets - Discount: ₱{discount}")
                
                result_items.append({
                    "product_name": item["product_name"],
                    "original_total": float(original_total.quantize(Decimal("0.01"))),
                    "discount": float(discount.quantize(Decimal("0.01"))),
                    "final_total": float(max(final_total, Decimal("0.00")).quantize(Decimal("0.01"))),
                    "applied_promo": {
                        "promotionName": promo.get("promotionName"),
                        "promotionType": promo.get("promotionType"),
                        "promotionValue": promo.get("promotionValue")
                    }
                })
                
                # Remove from unmatched
                unmatched_items.remove(item)
    
    # Process any remaining unmatched BOGO items
    if unmatched_items:
        print(f"[PROMO ENGINE] Processing {len(unmatched_items)} unmatched BOGO items")
        for item in unmatched_items:
            promo, discount = select_best_promo(item, promos)
            price = Decimal(str(item["price"]))
            qty = item["quantity"]
            original_total = price * qty
            final_total = original_total - discount
            total_discount += discount
            
            result_items.append({
                "product_name": item["product_name"],
                "original_total": float(original_total.quantize(Decimal("0.01"))),
                "discount": float(discount.quantize(Decimal("0.01"))),
                "final_total": float(max(final_total, Decimal("0.00")).quantize(Decimal("0.01"))),
                "applied_promo": {
                    "promotionName": promo.get("promotionName"),
                    "promotionType": promo.get("promotionType"),
                    "promotionValue": promo.get("promotionValue")
                } if promo else None
            })
    
    return {
        "items": result_items,
        "discount": total_discount
    }
