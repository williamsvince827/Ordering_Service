from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, date, timedelta
from database import get_db_connection
from services.promo_engine import apply_promotions
from services.promo_service import fetch_active_promos
import httpx
import asyncio
import logging
import os
import aiofiles
from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost:4000/auth/token")


async def validate_token_and_roles(token: str, allowed_roles: List[str]):
    USER_SERVICE_ME_URL = "http://localhost:4000/auth/users/me"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(USER_SERVICE_ME_URL, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            error_detail = f"Auth service error: {e.response.status_code} - {e.response.text}"
            logger.error(error_detail)
            raise HTTPException(status_code=e.response.status_code, detail=error_detail)
        except httpx.RequestError as e:
            logger.error(f"Auth service unavailable: {e}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Auth service unavailable: {e}")

    user_data = response.json()
    if user_data.get("userRole") not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    return user_data

@router.get("/admin/orders/total")
async def get_total_orders(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "staff"])
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("SELECT COUNT(*) FROM Orders")
        result = await cursor.fetchone()
        total_orders = result[0] if result else 0
    finally:
        await cursor.close()
        await conn.close()
    return {"total_orders": total_orders}



class CartItem(BaseModel):
    username: str
    product_id: int
    product_name: str
    quantity: int
    price: float
    product_type: Optional[str] = None
    product_category: Optional[str] = None
    order_type: str
    is_bogo_selected: Optional[bool] = False
    addons: Optional[List[dict]] = []


class CartResponse(BaseModel):
    order_item_id: int
    product_id: Optional[int]
    product_name: str
    quantity: int
    price: float
    product_type: Optional[str]
    product_category: Optional[str]
    order_type: str
    status: str
    created_at: str


class DeliveryInfoRequest(BaseModel):
    FirstName: str
    MiddleName: Optional[str] = None
    LastName: str
    Address: str
    City: str
    Province: str
    Landmark: Optional[str] = None
    EmailAddress: Optional[str] = None
    PhoneNumber: str
    Notes: Optional[str] = None

class FinalizeOrderRequest(BaseModel):
    username: str
    notes: Optional[str] = None

class UpdatePaymentDetails(BaseModel):
    username: str
    payment_method: str
    subtotal: float
    delivery_fee: float
    total_amount: float
    delivery_notes: Optional[str] = None
    reference_number: Optional[str] = None
    total_discount: Optional[float] = 0.0

class UpdateStatusRequest(BaseModel):
    new_status: str
    cashier_name: Optional[str] = None

@router.get("/admin/orders/manage")
async def get_all_orders(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "staff", "cashier"])

    conn = await get_db_connection()
    cursor = await conn.cursor()

    try:
        # Step 1: Fetch all orders
        await cursor.execute("""
            SELECT
                o.OrderID,
                o.UserName,
                o.OrderDate,
                o.OrderType,
                o.PaymentMethod,
                o.TotalAmount,
                o.DeliveryNotes,
                o.Status,
                o.ReferenceNumber,
                o.CashierName,
                di.EmailAddress,
                di.PhoneNumber,
                di.Address,
                di.City,
                di.Province,
                di.Landmark,
                di.FirstName,
                di.LastName,
                o.TotalDiscount,
                o.DeliveryFee
            FROM Orders o
                LEFT JOIN DeliveryInfo di ON di.OrderID = o.OrderID
            ORDER BY o.OrderDate DESC
        """)

        orders_data = await cursor.fetchall()
        
        if not orders_data:
            return []

        order_ids = [row[0] for row in orders_data]

        # Step 2: Fetch ALL items for ALL orders in ONE query
        order_ids_str = ','.join(str(oid) for oid in order_ids)
        await cursor.execute(f"""
            SELECT OrderID, OrderItemID, ProductName, Quantity, Price, ProductCategory,
                   PromoName, PromoType, PromoValue, PromoDiscountAmount
            FROM OrderItems
            WHERE OrderID IN ({order_ids_str})
        """)
        all_items = await cursor.fetchall()

        # Group items by order_id
        items_by_order = {}
        order_item_ids = []
        for item in all_items:
            order_id = item[0]
            if order_id not in items_by_order:
                items_by_order[order_id] = []
            items_by_order[order_id].append(item)
            order_item_ids.append(item[1])

        # Step 3: Fetch ALL addons for ALL items in ONE query
        addons_by_item = {}
        if order_item_ids:
            order_item_ids_str = ','.join(str(oiid) for oiid in order_item_ids)
            await cursor.execute(f"""
                SELECT OrderItemID, AddOnName, Price, AddOnID
                FROM OrderItemAddOns
                WHERE OrderItemID IN ({order_item_ids_str})
            """)
            all_addons = await cursor.fetchall()

            for addon in all_addons:
                item_id = addon[0]
                if item_id not in addons_by_item:
                    addons_by_item[item_id] = []
                addons_by_item[item_id].append({
                    "addon_name": addon[1],
                    "price": float(addon[2]),
                    "addon_id": addon[3]
                })

        # Step 4: Build response
        # Fallback: fetch user profile names via auth service endpoint when DeliveryInfo names are absent
        name_lookup_usernames = [row[1] for row in orders_data if not row[16] and not row[17]]
        user_name_map = {}
        if name_lookup_usernames:
            async with httpx.AsyncClient() as client:
                for uname in set(name_lookup_usernames):
                    try:
                        # Auth/User service likely mounts user routes under /users
                        resp = await client.get(
                            "http://localhost:4000/users/employee_name",
                            params={"username": uname},
                            headers={"Authorization": f"Bearer {token}"}
                        )
                        if resp.status_code == 200:
                            payload = resp.json()
                            full = payload.get("employee_name", "").strip()
                            logger.info(f"[NameFallback] Got full name '{full}' for username '{uname}'")
                            if full:
                                parts = full.split()
                                fn = parts[0] if parts else ""
                                ln = parts[-1] if len(parts) > 1 else ""
                                if fn or ln:
                                    user_name_map[uname] = (fn, ln)
                        else:
                            logger.warning(f"[NameFallback] Non-200 {resp.status_code} for username '{uname}' body={resp.text}")
                    except Exception as e:
                        logger.warning(f"[NameFallback] fetch failed for {uname}: {e}")

        orders = []
        for row in orders_data:
            order_id = row[0]

            # Get items for this order
            items = items_by_order.get(order_id, [])
            item_list = []
            for item in items:
                order_item_id = item[1]
                addons_list = addons_by_item.get(order_item_id, [])
                
                # Extract promo information
                promo_name = item[6] if len(item) > 6 and item[6] else None
                promo_discount = float(item[9]) if len(item) > 9 and item[9] else 0.0
                
                item_dict = {
                    "name": item[2],
                    "quantity": item[3],
                    "price": float(item[4]),
                    "category": item[5],
                    "addons": addons_list
                }
                
                # Add promo fields if they exist
                if promo_name:
                    item_dict["promo_name"] = promo_name
                    item_dict["applied_promo"] = promo_name
                if promo_discount > 0:
                    item_dict["discount"] = promo_discount
                
                item_list.append(item_dict)

            delivery_address = ", ".join(filter(None, [row[12], row[13], row[14], row[15]]))

            first_name = row[16]
            last_name = row[17]
            if (not first_name and not last_name) and row[1] in user_name_map:
                fetched_fn, fetched_ln = user_name_map[row[1]]
                first_name = first_name or fetched_fn
                last_name = last_name or fetched_ln
            combined_name = (f"{first_name} {last_name}".strip() if first_name and last_name else None)
            normalized_order_type = 'Pickup' if str(row[3]).lower().startswith('pick') else row[3]
            orders.append({
                "order_id": row[0],
                "customer_name": combined_name or row[1],
                "first_name": first_name,
                "last_name": last_name,
                "order_date": row[2].strftime("%Y-%m-%d %H:%M:%S"),
                "order_type": normalized_order_type,
                "payment_method": row[4],
                "total_amount": float(row[5]) if row[5] is not None else 0.0,
                "deliveryNotes": row[6],
                "order_status": row[7],
                "reference_number": row[8],
                "cashier_name": row[9],
                "emailAddress": row[10],
                "phoneNumber": row[11],
                "deliveryAddress": delivery_address,
                "items": item_list,
                "discount": float(row[18]) if row[18] is not None else 0.0,
                "deliveryFee": float(row[19]) if row[19] is not None else 0.0
            })

        return orders

    except Exception as e:
        logger.error(f"Failed to fetch orders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve orders")
    finally:
        await cursor.close()
        await conn.close()


@router.get("/admin/orders/today_count")
async def get_todays_orders_count(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "staff"])
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        today_str = date.today().strftime("%Y-%m-%d")
        await cursor.execute("""
            SELECT COUNT(*)
            FROM Orders
            WHERE CONVERT(date, OrderDate) = ?
        """, (today_str,))
        result = await cursor.fetchone()
        count = result[0] if result else 0
    finally:
        await cursor.close()
        await conn.close()
    return {"todays_orders": count}

@router.get("/admin/orders/pending")
async def get_all_pending_orders(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "staff"])

    conn = await get_db_connection()
    cursor = await conn.cursor()

    try:
        await cursor.execute("""
            SELECT 
                o.OrderID,
                o.UserName,
                o.OrderDate,
                o.OrderType,
                o.PaymentMethod,
                o.TotalAmount,
                o.Status,
                STRING_AGG(CONCAT(oi.ProductName, ' (x', CAST(oi.Quantity AS VARCHAR), ')'), ', ') AS Items
            FROM Orders o
            LEFT JOIN OrderItems oi ON o.OrderID = oi.OrderID
            WHERE o.Status = 'Pending'
            GROUP BY o.OrderID, o.UserName, o.OrderDate, o.OrderType, o.PaymentMethod, o.TotalAmount, o.Status
            ORDER BY o.OrderDate DESC
        """)

        rows = await cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch pending orders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve pending orders")
    finally:
        await cursor.close()
        await conn.close()

    orders = []
    for row in rows:
        total_amount = float(row[5]) if row[5] is not None else 0.0  # Gracefully handle null totals
        orders.append({
            "order_id": row[0],
            "customer_name": row[1],
            "order_date": row[2].strftime("%Y-%m-%d %H:%M:%S"),
            "order_type": row[3],
            "payment_method": row[4],
            "total_amount": total_amount,
            "order_status": row[6],
            "items": row[7] if row[7] else ""
        })

    return orders

@router.get("/cart/admin/orders/pending")
async def get_pending_orders_count(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "staff"])
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("SELECT COUNT(*) FROM Orders WHERE Status = 'Pending'")
        result = await cursor.fetchone()
        pending_count = result[0] if result else 0
    finally:
        await cursor.close()
        await conn.close()
    return {"pending_orders_count": pending_count}

@router.get("/orders/history")
async def get_user_orders(token: str = Depends(oauth2_scheme)):
    user_data = await validate_token_and_roles(token, ["user"])
    username = user_data.get("username")
    conn = await get_db_connection()
    cursor = await conn.cursor()

    # Ensure promo columns exist in OrderItems table
    try:
        await cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoType')
            BEGIN
                ALTER TABLE OrderItems ADD PromoType NVARCHAR(50) NULL
            END
        """)
        await cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoValue')
            BEGIN
                ALTER TABLE OrderItems ADD PromoValue FLOAT NULL
            END
        """)
        await cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoDiscountAmount')
            BEGIN
                ALTER TABLE OrderItems ADD PromoDiscountAmount FLOAT NULL
            END
        """)
        await cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoName')
            BEGIN
                ALTER TABLE OrderItems ADD PromoName NVARCHAR(255) NULL
            END
        """)
        await cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('Orders') AND name = 'DeliveryImage')
            BEGIN
                ALTER TABLE Orders ADD DeliveryImage NVARCHAR(512) NULL
            END
        """)
        await conn.commit()
    except Exception as e:
        logger.warning(f"Failed to create promo columns: {e}")

    # Step 1: Fetch all orders for user
    await cursor.execute("""
        SELECT o.OrderID, o.OrderDate, o.OrderType, o.Status, o.DeliveryFee, o.TotalAmount, o.TotalDiscount, o.DeliveryImage
        FROM Orders o
        WHERE o.UserName = ?
        ORDER BY o.OrderDate DESC
    """, (username,))

    order_rows = await cursor.fetchall()
    
    if not order_rows:
        await cursor.close()
        await conn.close()
        return []

    order_ids = [row[0] for row in order_rows]

    # Step 2: Fetch ALL items for ALL orders in ONE query with promo info
    order_ids_str = ','.join(str(oid) for oid in order_ids)
    await cursor.execute(f"""
        SELECT OrderID, OrderItemID, ProductName, Quantity, Price, 
               PromoType, PromoValue, PromoDiscountAmount, PromoName
        FROM OrderItems
        WHERE OrderID IN ({order_ids_str})
    """)
    all_items = await cursor.fetchall()

    # Group items by order_id
    items_by_order = {}
    order_item_ids = []
    for item in all_items:
        order_id = item[0]
        if order_id not in items_by_order:
            items_by_order[order_id] = []
        items_by_order[order_id].append(item)
        order_item_ids.append(item[1])

    # Step 3: Fetch ALL addons for ALL items in ONE query
    addons_by_item = {}
    if order_item_ids:
        order_item_ids_str = ','.join(str(oiid) for oiid in order_item_ids)
        await cursor.execute(f"""
            SELECT OrderItemID, AddOnName, Price, AddOnID
            FROM OrderItemAddOns
            WHERE OrderItemID IN ({order_item_ids_str})
        """)
        all_addons = await cursor.fetchall()

        for addon in all_addons:
            item_id = addon[0]
            if item_id not in addons_by_item:
                addons_by_item[item_id] = []
            addons_by_item[item_id].append({
                "addon_name": addon[1],
                "price": float(addon[2]),
                "addon_id": addon[3]
            })

    # Step 4: Build response
    orders = []
    for order_row in order_rows:
        order_id = order_row[0]

        # Get items for this order
        items = items_by_order.get(order_id, [])
        products = []
        for item in items:
            order_item_id = item[1]
            addons_list = addons_by_item.get(order_item_id, [])
            
            # Extract promo information
            promo_type = item[5] if len(item) > 5 else None
            promo_value = item[6] if len(item) > 6 else None
            promo_discount = float(item[7]) if len(item) > 7 and item[7] is not None else 0.0
            promo_name = item[8] if len(item) > 8 and item[8] else None
            
            product_data = {
                "name": item[2],
                "quantity": item[3],
                "price": float(item[4]),
                "addons": addons_list,
                "discount": promo_discount
            }
            
            # Add promo name if available
            if promo_name:
                product_data["promo_name"] = promo_name
            elif promo_type and promo_value:
                # Fallback: Generate promo name based on type and value
                if promo_type == 'percentage':
                    promo_name = f"{promo_value}% OFF"
                elif promo_type == 'fixed':
                    promo_name = f"₱{promo_value} OFF"
                elif promo_type == 'bogo':
                    promo_name = "BOGO Promo"
                else:
                    promo_name = "Promo Applied"
                product_data["promo_name"] = promo_name
            
            if promo_type:
                product_data["applied_promo"] = {
                    "promotionType": promo_type,
                    "promotionValue": promo_value,
                    "promotionName": promo_name or "Promo Applied"
                }
            
            products.append(product_data)

        orders.append({
            "id": order_id,
            "date": order_row[1],
            "orderType": order_row[2],
            "status": order_row[3],
            "deliveryFee": float(order_row[4]) if order_row[4] is not None else 0.0,
            "totalAmount": float(order_row[5]) if order_row[5] is not None else 0.0,
            "discount": float(order_row[6]) if order_row[6] is not None else 0.0,
            "deliveryImage": order_row[7] if len(order_row) > 7 and order_row[7] else None,
            "products": products,
        })

    await cursor.close()
    await conn.close()

    return orders

@router.get("/calculate-promos")
async def calculate_promotions_for_cart(
    username: str = Query(...), 
    cart_item_ids: str = Query(None),  # Comma-separated cart item IDs
    order_type: str = Query("Pick Up"),  # Order type from frontend
    token: str = Depends(oauth2_scheme)
):
    """
    Calculate and apply promotions to the user's cart items.
    If no pending order exists, creates one from cart items first.
    This is called at checkout to preview discounts before placing the order.
    
    Args:
        cart_item_ids: Optional comma-separated list of cart item IDs to include (e.g. "123,124,125")
    """
    logger.info(f"[CALCULATE-PROMOS] Called for user={username}, cart_item_ids={cart_item_ids}")
    await validate_token_and_roles(token, ["user", "admin", "staff"])

    conn = await get_db_connection()
    cursor = await conn.cursor()
    
    try:
        # Ensure IsBogoSelected column exists (migration-safe)
        try:
            await cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.columns 
                               WHERE object_id = OBJECT_ID(N'OrderItems') 
                               AND name = 'IsBogoSelected')
                BEGIN
                    ALTER TABLE OrderItems ADD IsBogoSelected BIT DEFAULT 0
                END
            """)
            await conn.commit()
        except Exception as e:
            logger.warning(f"Column IsBogoSelected migration check: {e}")
            # Continue anyway
        
        # Always delete any existing pending unpaid orders to ensure fresh calculation
        # Delete in correct order: OrderItemAddOns -> OrderItems -> Orders to avoid FK constraints
        
        # Step 1: Delete OrderItemAddOns
        await cursor.execute("""
            DELETE oia FROM OrderItemAddOns oia
            INNER JOIN OrderItems oi ON oia.OrderItemID = oi.OrderItemID
            INNER JOIN Orders o ON oi.OrderID = o.OrderID
            WHERE o.UserName = ? AND o.Status = 'Pending' AND o.PaymentStatus = 'Unpaid'
        """, (username,))
        
        # Step 2: Delete OrderItems
        await cursor.execute("""
            DELETE oi FROM OrderItems oi
            INNER JOIN Orders o ON oi.OrderID = o.OrderID
            WHERE o.UserName = ? AND o.Status = 'Pending' AND o.PaymentStatus = 'Unpaid'
        """, (username,))
        
        # Step 3: Delete Orders
        await cursor.execute("""
            DELETE FROM Orders 
            WHERE UserName = ? AND Status = 'Pending' AND PaymentStatus = 'Unpaid'
        """, (username,))
        await conn.commit()
        
        logger.info(f"Cleared any pending orders for {username}, creating fresh from cart...")
        
        # Get cart items from cartItems table
        cart_item_filter = ""
        filter_params = [username]
        
        if cart_item_ids:
            # Parse comma-separated IDs
            ids = [int(id.strip()) for id in cart_item_ids.split(',') if id.strip().isdigit()]
            if ids:
                placeholders = ','.join('?' * len(ids))
                cart_item_filter = f" AND CartItemID IN ({placeholders})"
                filter_params.extend(ids)
                logger.info(f"Filtering to specific cart items: {ids}")
        
        await cursor.execute(f"""
            SELECT ProductID, ProductName, ProductType, ProductCategory, Quantity, Price, CartItemID, ISNULL(IsBogoSelected, 0)
            FROM cartItems
            WHERE Username = ?{cart_item_filter}
        """, tuple(filter_params))
        cart_rows = await cursor.fetchall()
        
        logger.info(f"[CART-ITEMS] Found {len(cart_rows)} items in cart for {username}")
        for row in cart_rows:
            logger.info(f"  CartItem: {row[1]} - Qty: {row[4]} - Price: {row[5]}")

        if not cart_rows:
            return {
                "items": [],
                "subtotal_discount": 0,
                "final_subtotal": 0
            }

        # Create new pending order
        await cursor.execute("""
            INSERT INTO Orders (UserName, OrderDate, Status, PaymentStatus, OrderType)
            VALUES (?, GETDATE(), 'Pending', 'Unpaid', ?)
        """, (username, order_type))
        await conn.commit()
        
        # Get the newly created OrderID
        await cursor.execute("SELECT @@IDENTITY")
        order_id = (await cursor.fetchone())[0]
        
        logger.info(f"Created fresh pending order {order_id} for {username} with {len(cart_rows)} cart items")
        
        # Insert OrderItems from cartItems
        import json
        inserted_count = 0
        for row in cart_rows:
            product_id, product_name, product_type, product_category, quantity, price, cart_item_id, is_bogo_selected = row
            
            logger.info(f"[BOGO FLAG FROM CART] CartItem {cart_item_id}: {product_name}, IsBogoSelected={is_bogo_selected}")
            
            # Get addons from cartItemAddons table
            await cursor.execute("""
                SELECT AddonName, Price, AddonID
                FROM cartItemAddons
                WHERE CartItemID = ?
            """, (cart_item_id,))
            addon_rows = await cursor.fetchall()
            
            # Build addons list
            addons_list = []
            for addon_row in addon_rows:
                addon_name, addon_price, addon_id = addon_row
                addons_list.append({
                    "addon_name": addon_name,
                    "price": float(addon_price),
                    "addon_id": addon_id
                })
            
            # Insert order item with IsBogoSelected flag
            await cursor.execute("""
                INSERT INTO OrderItems 
                (OrderID, ProductName, ProductType, ProductCategory, Quantity, Price, IsBogoSelected)
                OUTPUT INSERTED.OrderItemID
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id,
                product_name,
                product_type or '',
                product_category or '',
                quantity,
                price,
                is_bogo_selected
            ))
            order_item_id = (await cursor.fetchone())[0]
            inserted_count += 1
            logger.info(f"  [INSERT] OrderItem #{inserted_count}: {product_name} x{quantity} @ ₱{price}, IsBogoSelected={is_bogo_selected} -> OrderItemID={order_item_id}")
            
            # Insert addons into OrderItemAddOns table
            for addon in addons_list:
                await cursor.execute("""
                    INSERT INTO OrderItemAddOns (OrderItemID, AddOnName, Price, AddOnID)
                    VALUES (?, ?, ?, ?)
                """, (
                    order_item_id,
                    addon["addon_name"],
                    addon["price"],
                    addon["addon_id"]
                ))
        
        await conn.commit()
        logger.info(f"[COMMIT] Inserted {inserted_count} items into order {order_id}")

        # Now get order items for promo calculation
        await cursor.execute("""
            SELECT OrderItemID, ProductName, ProductCategory, Quantity, Price, ISNULL(IsBogoSelected, 0)
            FROM OrderItems
            WHERE OrderID = ?
        """, (order_id,))
        items = await cursor.fetchall()
        
        logger.info(f"[VERIFY] Found {len(items) if items else 0} items in OrderItems table for order {order_id}")
        for item in items:
            logger.info(f"  DB Item: OrderItemID={item[0]}, Name={item[1]}, Qty={item[3]}")

        if not items:
            # If using existing order with no items, it might be cleared/corrupted
            logger.error(f"Order {order_id} has no items. This might be a stale order.")
            raise HTTPException(status_code=400, detail="Order exists but has no items. Please clear your cart and try again.")

        # Prepare cart items for promo engine
        cart_items = []
        for item in items:
            order_item_id, product_name, product_category, quantity, price, is_bogo_selected = item
            
            logger.info(f"[PROMO DEBUG] Item: {product_name}, IsBogoSelected from DB: {is_bogo_selected}")
            
            # Get addons from OrderItemAddOns table
            await cursor.execute("""
                SELECT AddOnName, Price, AddOnID
                FROM OrderItemAddOns
                WHERE OrderItemID = ?
            """, (order_item_id,))
            addon_rows = await cursor.fetchall()
            
            # Calculate addon total
            addon_total = sum(float(addon[1]) for addon in addon_rows)
            
            item_price = float(price) + addon_total
            
            cart_items.append({
                "order_item_id": order_item_id,
                "product_name": product_name,
                "product_category": product_category,
                "quantity": quantity,
                "price": item_price,
                "is_bogo_selected": bool(is_bogo_selected)
            })
            logger.info(f"[PROMO DEBUG] Added to cart_items with is_bogo_selected={bool(is_bogo_selected)}")

        # Fetch active promos
        promos = await fetch_active_promos(token)
        
        logger.info(f"Found {len(promos) if promos else 0} active promotions")
        if promos:
            for promo in promos:
                logger.info(f"Promo: {promo.get('promotionName')} - Type: {promo.get('promotionType')} - Application: {promo.get('applicationType')}")
        
        logger.info(f"Cart items for promo calculation: {cart_items}")
        
        if not promos:
            logger.info(f"No active promotions available for order {order_id}")
            subtotal = sum(item["price"] * item["quantity"] for item in cart_items)
            return {
                "order_id": order_id,
                "items": [
                    {
                        "order_item_id": item["order_item_id"],
                        "product_name": item["product_name"],
                        "original_total": item["price"] * item["quantity"],
                        "discount": 0,
                        "final_total": item["price"] * item["quantity"],
                        "applied_promo": None
                    }
                    for item in cart_items
                ],
                "subtotal_discount": 0,
                "final_subtotal": subtotal
            }

        # Apply promotions
        promo_result = apply_promotions(cart_items, promos)
        
        logger.info(f"Promo calculation result for order {order_id}:")
        for idx, item_result in enumerate(promo_result["items"]):
            logger.info(f"  Item {idx+1}: {item_result['product_name']} - Discount: ₱{item_result['discount']} - Applied: {item_result.get('applied_promo', {}).get('promotionName') if item_result.get('applied_promo') else 'None'}")
        logger.info(f"Total subtotal discount: ₱{promo_result['subtotal_discount']}")

        # Ensure promo columns exist in OrderItems table
        try:
            await cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoType')
                BEGIN
                    ALTER TABLE OrderItems ADD PromoType NVARCHAR(50) NULL
                END
            """)
            await cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoValue')
                BEGIN
                    ALTER TABLE OrderItems ADD PromoValue FLOAT NULL
                END
            """)
            await cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoDiscountAmount')
                BEGIN
                    ALTER TABLE OrderItems ADD PromoDiscountAmount FLOAT NULL
                END
            """)
            await cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('OrderItems') AND name = 'PromoName')
                BEGIN
                    ALTER TABLE OrderItems ADD PromoName NVARCHAR(255) NULL
                END
            """)
            await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to create promo columns: {e}")

        # Update OrderItems with promo details
        for idx, item_result in enumerate(promo_result["items"]):
            # Match by index position since cart_items and promo_result["items"] are in same order
            if idx < len(cart_items):
                order_item_id = cart_items[idx]["order_item_id"]
                
                if item_result["applied_promo"]:
                    promo = item_result["applied_promo"]
                    logger.info(f"  [UPDATE PROMO] OrderItemID={order_item_id}: {item_result['product_name']} - {promo.get('promotionName')} - Discount: ₱{item_result['discount']}")
                    await cursor.execute("""
                        UPDATE OrderItems
                        SET PromoType = ?,
                            PromoValue = ?,
                            PromoDiscountAmount = ?,
                            PromoName = ?
                        WHERE OrderItemID = ?
                    """, (
                        promo.get("promotionType"),
                        promo.get("promotionValue"),
                        item_result["discount"],
                        promo.get("promotionName"),
                        order_item_id
                    ))
                else:
                    logger.info(f"  [NO PROMO] OrderItemID={order_item_id}: {item_result['product_name']} - No promo applied")

        await conn.commit()
        
        logger.info(
            f"Applied promotions to order {order_id}: "
            f"Total discount = {promo_result['subtotal_discount']}"
        )

        return {
            "order_id": order_id,
            **promo_result
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating promotions for cart: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to calculate promotions")
    finally:
        await cursor.close()
        await conn.close()

@router.put("/update-payment")
async def update_payment_details(payload: UpdatePaymentDetails, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("""
            SELECT OrderID FROM Orders
            WHERE UserName = ? AND Status = 'Pending' AND PaymentStatus = 'Paid'
            ORDER BY OrderDate DESC
            OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY
        """, (payload.username,))
        order = await cursor.fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="No paid order found to update")

        order_id = order[0]

        await cursor.execute("""
            UPDATE Orders
            SET PaymentMethod = ?, Subtotal = ?, DeliveryFee = ?, TotalAmount = ?, DeliveryNotes = ?, ReferenceNumber = ?, TotalDiscount = ?
            WHERE OrderID = ?
        """, (
            payload.payment_method,
            payload.subtotal,
            payload.delivery_fee,
            payload.total_amount,
            payload.delivery_notes or '',
            payload.reference_number,
            payload.total_discount,
            order_id
        ))

        await conn.commit()
        # ✅ Notify user when order is placed (payment details updated)
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "http://localhost:7002/notifications/notifications/create",
                    params={
                        "username": payload.username,
                        "title": "Order Placed",
                        "message": f"Your order {payload.reference_number} has been placed successfully!",
                        "type": "Order",
                        "order_id": order_id
                    }
                )
        except Exception as notify_err:
            logger.error(f"Failed to send notification: {notify_err}")
    except Exception as e:
        logger.error(f"Error updating order payment details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update order payment details")
    finally:
        await cursor.close()
        await conn.close()

    return {"message": "Order payment details updated successfully"}



@router.post("/deliveryinfo", status_code=status.HTTP_201_CREATED)
async def add_delivery_info(delivery_info: DeliveryInfoRequest, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("""
            INSERT INTO DeliveryInfo (
                FirstName, MiddleName, LastName, Address, City, Province, Landmark, EmailAddress, PhoneNumber, Notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            delivery_info.FirstName,
            delivery_info.MiddleName,
            delivery_info.LastName,
            delivery_info.Address,
            delivery_info.City,
            delivery_info.Province,
            delivery_info.Landmark,
            delivery_info.EmailAddress,
            delivery_info.PhoneNumber,
            delivery_info.Notes
        ))
        await conn.commit()
    except Exception as e:
        logger.error(f"Error adding delivery info: {e}")
        raise HTTPException(status_code=500, detail="Failed to add delivery info")
    finally:
        await cursor.close()
        await conn.close()
    return {"message": "Delivery info added successfully"}


@router.get("/{username}", response_model=List[CartResponse])
async def get_cart(username: str, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])
    conn = await get_db_connection()
    cursor = await conn.cursor()

    await cursor.execute("""
        SELECT OrderID, OrderDate, Status
        FROM Orders
        WHERE UserName = ? AND Status = 'Pending' AND PaymentStatus = 'Unpaid'
        ORDER BY OrderDate DESC
        OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY
    """, (username,))
    order = await cursor.fetchone()

    if not order:
        await cursor.close()
        await conn.close()
        return []

    order_id = order[0]

    await cursor.execute("""
        SELECT OrderItemID, ProductName, ProductType, ProductCategory, Quantity, Price
        FROM OrderItems
        WHERE OrderID = ?
    """, (order_id,))
    items = await cursor.fetchall()
    await cursor.close()
    await conn.close()

    cart = []
    for item in items:
        cart.append(CartResponse(
            order_item_id=item[0],
            product_id=None,
            product_name=item[1],
            product_type=item[2],
            product_category=item[3],
            quantity=item[4],
            price=float(item[5]),
            order_type=order[2],
            status=order[2],
            created_at=order[1].strftime("%Y-%m-%d %H:%M:%S")
        ))
    return cart


@router.post("/", status_code=status.HTTP_201_CREATED)
async def add_to_cart(item: CartItem, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])
    logger.info(f"Adding to cart: {item.dict()}")
    logger.info(f"[BOGO FLAG] is_bogo_selected received: {item.is_bogo_selected}")
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # Look for unpaid & pending order
        await cursor.execute("""
            SELECT OrderID
            FROM Orders
            WHERE UserName = ? AND Status = 'Pending' AND PaymentStatus = 'Unpaid'
            ORDER BY OrderDate DESC
            OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY
        """, (item.username,))
        order = await cursor.fetchone()

        if order:
            order_id = order[0]
        else:
            await cursor.execute("""
                INSERT INTO Orders (
                    UserName, OrderType, PaymentMethod, Subtotal, DeliveryFee,
                    TotalAmount, DeliveryNotes, Status, PaymentStatus
                )
                OUTPUT INSERTED.OrderID
                VALUES (?, ?, ?, 0, 0, 0, '', 'Pending', 'Unpaid')
            """, (item.username, item.order_type, 'Cash'))
            row = await cursor.fetchone()
            order_id = row[0] if row else None

        

        # Check if item already in cart
        await cursor.execute("""
            SELECT OrderItemID, Quantity FROM OrderItems
            WHERE OrderID = ? AND ProductName = ? AND ProductType = ? AND ProductCategory = ?
        """, (
            order_id, item.product_name,
            item.product_type or '', item.product_category or ''
        ))
        existing_item = await cursor.fetchone()

        merge = False
        if existing_item:
            order_item_id, current_qty = existing_item
            # Get existing addons
            await cursor.execute("""
                SELECT AddOnName FROM OrderItemAddOns WHERE OrderItemID = ? ORDER BY AddOnName
            """, (order_item_id,))
            existing_addons = [row[0] for row in await cursor.fetchall()]
            incoming_addons = sorted([a.get("name") or a.get("addon_name") for a in item.addons or []])
            if existing_addons == incoming_addons:
                merge = True
                await cursor.execute("""
                    UPDATE OrderItems SET Quantity = Quantity + ? WHERE OrderItemID = ?
                """, (item.quantity, order_item_id))

        if not merge:
            # Try to add IsBogoSelected column if it doesn't exist (migration-safe)
            try:
                await cursor.execute("""
                    IF NOT EXISTS (SELECT * FROM sys.columns 
                                   WHERE object_id = OBJECT_ID(N'OrderItems') 
                                   AND name = 'IsBogoSelected')
                    BEGIN
                        ALTER TABLE OrderItems ADD IsBogoSelected BIT DEFAULT 0
                    END
                """)
                await conn.commit()
            except Exception as e:
                logger.warning(f"Column IsBogoSelected may already exist or error adding: {e}")
                # Continue anyway
            
            await cursor.execute("""
                INSERT INTO OrderItems (OrderID, ProductName, ProductType, ProductCategory, Quantity, Price, IsBogoSelected)
                OUTPUT INSERTED.OrderItemID
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id, item.product_name, item.product_type or '',
                item.product_category or '', item.quantity, item.price, item.is_bogo_selected
            ))
            row = await cursor.fetchone()
            order_item_id = row[0] if row else None

            if item.addons:
                logger.info(f"Processing addons for new item {order_item_id}: {item.addons}")
                for addon in item.addons:
                    try:
                        addon_name = addon.get("name") or addon.get("addon_name")
                        if not addon_name:
                            continue

                        await cursor.execute("""
                            INSERT INTO OrderItemAddOns (OrderItemID, AddOnName, Price, AddOnID)
                            VALUES (?, ?, ?, ?)
                        """, (
                            order_item_id,
                            addon_name,
                            addon.get("price", 0),
                            addon.get("addon_id")
                        ))
                        logger.info(f"Successfully inserted addon {addon_name} for new item {order_item_id}")
                    except Exception as e:
                        logger.error(f"Error inserting addon for new item {order_item_id}: {e}")
                        raise

        await conn.commit()

    except Exception as e:
        logger.error(f"Error adding to cart: {e}")
        raise HTTPException(status_code=500, detail="Failed to add item to cart")
    finally:
        await cursor.close()
        await conn.close()

    return {"message": "Item added to cart", "order_item_id": order_item_id}


@router.put("/quantity/{order_item_id}")
async def update_quantity(order_item_id: int, new_quantity: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])
    if new_quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be at least 1")

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("""
            UPDATE OrderItems
            SET Quantity = ?
            WHERE OrderItemID = ?
        """, (new_quantity, order_item_id))
        await conn.commit()
    except Exception as e:
        logger.error(f"Error updating quantity: {e}")
        raise HTTPException(status_code=500, detail="Failed to update quantity")
    finally:
        await cursor.close()
        await conn.close()
    return {"message": "Quantity updated"}


@router.delete("/{order_item_id}", status_code=status.HTTP_200_OK)
async def remove_from_cart(order_item_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        logger.info(f"Removing from cart: {order_item_id}")
        await cursor.execute("DELETE FROM OrderItems WHERE OrderItemID = ?", (order_item_id,))
        await conn.commit()
    except Exception as e:
        logger.error(f"Error removing from cart: {e}")
        raise HTTPException(status_code=500, detail="Failed to remove item from cart")
    finally:
        await cursor.close()
        await conn.close()
    return {"message": "Item removed from cart"}


@router.post("/finalize", status_code=status.HTTP_200_OK)
async def finalize_order(username: str, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # Select only unpaid orders
        await cursor.execute("""
            SELECT OrderID FROM Orders
            WHERE UserName = ? AND Status = 'Pending' AND PaymentStatus = 'Unpaid'
            ORDER BY OrderDate DESC
            OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY
        """, (username,))
        order = await cursor.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="No pending unpaid order found")

        order_id = order[0]

        # Finalize by setting PaymentStatus to Paid
        await cursor.execute("""
            UPDATE Orders
            SET PaymentStatus = 'Paid'
            WHERE OrderID = ?
        """, (order_id,))
        await conn.commit()

        

    except Exception as e:
        logger.error(f"Error finalizing order: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to finalize order: {str(e)}")
    finally:
        await cursor.close()
        await conn.close()

    return {"message": "Order finalized and marked as Paid"}

@router.post("/update_addons", status_code=status.HTTP_200_OK)
async def update_addons_for_item(order_item_id: int, addons: List[dict], token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])
    logger.info(f"Updating addons for order_item_id {order_item_id}: {addons}")
    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        for addon in addons:
            try:
                logger.info(f"Checking addon: {addon}")
                await cursor.execute("""
                    SELECT COUNT(*) FROM OrderItemAddOns WHERE OrderItemID = ? AND AddOnName = ?
                """, (order_item_id, addon.get("addon_name")))
                exists = await cursor.fetchone()
                logger.info(f"Addon exists check: {exists}")
                if exists and exists[0] == 0:
                    logger.info(f"Inserting addon: {addon}")
                    await cursor.execute("""
                        INSERT INTO OrderItemAddOns (OrderItemID, AddOnName, Price, AddOnID)
                        VALUES (?, ?, ?, ?)
                    """, (
                        order_item_id,
                        addon.get("addon_name"),
                        addon.get("price", 0),
                        addon.get("addon_id")
                    ))
                    logger.info(f"Successfully inserted addon {addon.get('addon_name')} for item {order_item_id}")
                else:
                    logger.info(f"Addon {addon.get('addon_name')} already exists for item {order_item_id}")
            except Exception as e:
                logger.error(f"Error inserting addon {addon.get('addon_name')} for item {order_item_id}: {e}")
                raise
        await conn.commit()
    except Exception as e:
        logger.error(f"Error updating addons for order_item_id {order_item_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update addons")
    finally:
        await cursor.close()
        await conn.close()

    return {"message": "Addons updated successfully"}

@router.patch("/admin/orders/{order_id}/status", status_code=status.HTTP_200_OK)
async def update_order_status(
    order_id: int,
    request: UpdateStatusRequest,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, ["admin", "staff", "cashier", "user"])
    username = user_data.get("username")
    user_role = user_data.get("userRole")

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # Check if the order exists
        await cursor.execute("SELECT OrderID, UserName FROM Orders WHERE OrderID = ?", (order_id,))
        order = await cursor.fetchone()
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

        order_owner = order[1]

        # If user is not admin/staff/cashier, check ownership
        if user_role == "user" and order_owner != username:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only update your own orders"
            )

        # ✅ Update status with CashierName for different scenarios
        if request.new_status == "PREPARING":
            # When accepting order, use the logged-in username
            await cursor.execute(
                "UPDATE Orders SET Status = ?, CashierName = ? WHERE OrderID = ?",
                (request.new_status, username, order_id)
            )
        elif request.new_status == "CANCELLED" and request.cashier_name:
            # When cancelling, use the provided cashier_name from frontend
            await cursor.execute(
                "UPDATE Orders SET Status = ?, CashierName = ? WHERE OrderID = ?",
                (request.new_status, request.cashier_name, order_id)
            )
        else:
            # For other status updates
            await cursor.execute(
                "UPDATE Orders SET Status = ? WHERE OrderID = ?",
                (request.new_status, order_id)
            )

        await conn.commit()

        logger.info(f"Updated status for order {order_id} to {request.new_status}")

        # Get reference number
        await cursor.execute("SELECT ReferenceNumber FROM Orders WHERE OrderID = ?", (order_id,))
        ref = await cursor.fetchone()

        reference_number = ref[0] if ref else f"#{order_id}"
        
        # ✅ Notify customer about the status update
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "http://localhost:7002/notifications/notifications/create",
                    params={
                        "username": order_owner,
                        "title": "Order Update",
                        "message": f"Your order {reference_number} is now {request.new_status}.",
                        "type": "Order",
                        "order_id": order_id
                    }
                )
        except Exception as notify_err:
            logger.error(f"Failed to send order update notification: {notify_err}")

        return {"message": f"Order status successfully updated to {request.new_status}"}

    except Exception as e:
        await conn.rollback()
        logger.error(f"Error updating order status for OrderID {order_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update order status."
        )
    finally:
        await cursor.close()
        await conn.close()

@router.patch("/rider/orders/{order_id}/status", status_code=status.HTTP_200_OK)
async def update_rider_order_status(
    order_id: int,
    request: UpdateStatusRequest,
    token: str = Depends(oauth2_scheme)
):
    user_data = await validate_token_and_roles(token, ["rider"])
    username = user_data.get("username")
    rider_id = user_data.get("id") or user_data.get("userId")  # Assuming user_data has 'id' or 'userId'

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # Check if the order exists and is assigned to this rider
        await cursor.execute("SELECT OrderID FROM Orders WHERE OrderID = ? AND AssignedRiderID = ?", (order_id, rider_id))
        order = await cursor.fetchone()
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found or not assigned to you")

        # Update status
        await cursor.execute(
            "UPDATE Orders SET Status = ? WHERE OrderID = ?",
            (request.new_status, order_id)
        )

        await conn.commit()

        logger.info(f"Rider {username} updated status for order {order_id} to {request.new_status}")
        return {"message": f"Order status successfully updated to {request.new_status}"}

    except Exception as e:
        await conn.rollback()
        logger.error(f"Error updating rider order status for OrderID {order_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update order status."
        )
    finally:
        await cursor.close()
        await conn.close()

@router.patch("/admin/orders/auto-cancel/{reference_number}")
async def auto_cancel_order_by_reference(
    reference_number: str,
    cancellation_data: dict
):
    """
    Endpoint called by POS to cancel an OOS order that has expired.
    This is called by the POS system's auto-cancel task.
    """
    conn = await get_db_connection()
    cursor = await conn.cursor()
    
    try:
        # Find the order by reference number
        await cursor.execute("""
            SELECT OrderID, UserName, Status 
            FROM Orders 
            WHERE ReferenceNumber = ?
        """, (reference_number,))
        
        order = await cursor.fetchone()
        
        if not order:
            logger.warning(f"Order with reference {reference_number} not found in OOS")
            raise HTTPException(status_code=404, detail="Order not found")
        
        order_id = order[0]
        username = order[1]
        current_status = order[2]
        
        # Only cancel if still pending
        if current_status.lower() != 'pending':
            logger.info(f"Order {order_id} is {current_status}, skipping auto-cancel")
            return {
                "message": f"Order already has status: {current_status}",
                "order_id": order_id
            }
        
        # Update order status to cancelled
        await cursor.execute("""
            UPDATE Orders 
            SET Status = 'Cancelled', 
                DeliveryNotes = CONCAT(
                    ISNULL(DeliveryNotes, ''), 
                    ' [AUTO-CANCELLED: Order expired after 30 minutes]'
                )
            WHERE OrderID = ?
        """, (order_id,))
        
        await conn.commit()
        
        logger.info(
            f"✅ Auto-cancelled OOS OrderID={order_id}, "
            f"Ref={reference_number}, Reason={cancellation_data.get('reason')}"
        )
        
        # Send notification to customer
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    "http://localhost:7002/notifications/notifications/create",
                    params={
                        "username": username,
                        "title": "Order Automatically Cancelled",
                        "message": f"Your order #{order_id} was automatically cancelled due to payment timeout (30 minutes).",
                        "type": "Order",
                        "order_id": order_id
                    }
                )
        except Exception as notify_err:
            logger.error(f"Failed to send cancellation notification: {notify_err}")
        
        return {
            "message": "Order successfully auto-cancelled",
            "order_id": order_id,
            "reference_number": reference_number,
            "reason": cancellation_data.get("reason")
        }
        
    except Exception as e:
        await conn.rollback()
        logger.error(f"Error auto-cancelling order {reference_number}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to cancel order: {str(e)}")
    finally:
        await cursor.close()
        await conn.close()


# Also add a background task for OOS to check its own orders
async def auto_cancel_expired_oos_orders():
    """
    Background task for OOS to independently check and cancel expired orders
    """
    while True:
        try:
            logger.info("🔍 OOS: Checking for expired pending orders...")
            conn = await get_db_connection()
            cursor = await conn.cursor()
            
            # Find orders pending for more than 30 minutes
            expiration_time = datetime.now() - timedelta(minutes=30)
            
            await cursor.execute("""
                SELECT OrderID, UserName, ReferenceNumber
                FROM Orders
                WHERE Status = 'Pending' 
                AND PaymentStatus = 'Unpaid'
                AND OrderDate < ?
            """, (expiration_time,))
            
            expired_orders = await cursor.fetchall()
            
            if expired_orders:
                logger.info(f"Found {len(expired_orders)} expired OOS orders")
                
                for order in expired_orders:
                    order_id = order[0]
                    username = order[1]
                    reference_number = order[2]
                    
                    try:
                        await cursor.execute("""
                            UPDATE Orders 
                            SET Status = 'Cancelled',
                                DeliveryNotes = CONCAT(
                                    ISNULL(DeliveryNotes, ''), 
                                    ' [AUTO-CANCELLED: Payment not completed within 30 minutes]'
                                )
                            WHERE OrderID = ?
                        """, (order_id,))
                        
                        await conn.commit()
                        
                        logger.info(f"✅ OOS auto-cancelled OrderID={order_id}")
                        
                        # Notify customer
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                await client.post(
                                    "http://localhost:7002/notifications/notifications/create",
                                    params={
                                        "username": username,
                                        "title": "Order Automatically Cancelled",
                                        "message": f"Your order #{order_id} was cancelled due to payment timeout.",
                                        "type": "Order",
                                        "order_id": order_id
                                    }
                                )
                        except Exception as notify_err:
                            logger.error(f"Failed to send notification: {notify_err}")
                            
                    except Exception as e:
                        await conn.rollback()
                        logger.error(f"Failed to cancel order {order_id}: {e}")
                        continue
            else:
                logger.info("No expired OOS orders found")
                
            await cursor.close()
            await conn.close()
            
        except Exception as e:
            logger.error(f"Error in OOS auto-cancel task: {e}", exc_info=True)
        
        # Check every 5 minutes
        await asyncio.sleep(300)


@router.post("/rider/orders/{order_id}/delivery-image", status_code=status.HTTP_200_OK)
async def upload_delivery_image(
    order_id: int,
    image: UploadFile = File(...),
    token: str = Depends(oauth2_scheme)
):
    """
    Upload proof of delivery image for an order.
    Called by rider when marking order as delivered.
    """
    user_data = await validate_token_and_roles(token, ["rider"])
    rider_id = user_data.get("id") or user_data.get("userId")
    
    conn = await get_db_connection()
    cursor = await conn.cursor()
    
    try:
        # Verify the order exists and is assigned to this rider
        await cursor.execute(
            "SELECT OrderID FROM Orders WHERE OrderID = ? AND AssignedRiderID = ?",
            (order_id, rider_id)
        )
        order = await cursor.fetchone()
        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found or not assigned to you"
            )
        
        # Create uploads directory if it doesn't exist
        upload_dir = Path("uploads/delivery_images")
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate unique filename
        file_extension = os.path.splitext(image.filename)[1]
        filename = f"delivery_{order_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{file_extension}"
        file_path = upload_dir / filename
        
        # Save the file
        async with aiofiles.open(file_path, 'wb') as f:
            content = await image.read()
            await f.write(content)
        
        # Update the database with the image path
        image_url = f"/uploads/delivery_images/{filename}"
        await cursor.execute(
            "UPDATE Orders SET DeliveryImage = ? WHERE OrderID = ?",
            (image_url, order_id)
        )
        await conn.commit()
        
        logger.info(f"Delivery image uploaded for order {order_id}: {image_url}")
        return {
            "message": "Delivery image uploaded successfully",
            "image_url": image_url
        }
        
    except Exception as e:
        await conn.rollback()
        logger.error(f"Error uploading delivery image for order {order_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload delivery image: {str(e)}"
        )
    finally:
        await cursor.close()
        await conn.close()