from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import Optional
from database import get_db_connection
from datetime import date, timedelta
import httpx
import logging
import asyncio

logger = logging.getLogger(__name__)
router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost:4000/auth/token")

# --- AUTH VALIDATOR ---
async def validate_token_and_roles(token: str, allowed_roles: list[str]):
    USER_SERVICE_ME_URL = "http://localhost:4000/auth/users/me"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(USER_SERVICE_ME_URL, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"Auth service error: {e.response.status_code} - {e.response.text}")
            raise HTTPException(status_code=e.response.status_code, detail="Authentication failed.")
        except httpx.RequestError as e:
            logger.error(f"Auth service unavailable: {e}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth service unavailable.")

    user_data = response.json()
    if user_data.get("userRole") not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    return user_data.get("username")

# --- DELIVERY INFO MODEL ---
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

class UpdateDeliveryStatusRequest(BaseModel):
    status: str
# --- ROUTE: Get Delivery Info by OrderID ---
@router.get("/info/{order_id}")
async def get_delivery_info(order_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "staff"])

    conn = await get_db_connection()
    cursor = await conn.cursor()

    try:
        await cursor.execute("""
            SELECT FirstName, MiddleName, LastName, Address,
                   City, Province, Landmark, EmailAddress,
                   PhoneNumber, Notes
            FROM DeliveryInfo
            WHERE OrderID = ?
        """, (order_id,))
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Delivery info not found for this OrderID")

        return {
            "FirstName": row[0],
            "MiddleName": row[1],
            "LastName": row[2],
            "Address": row[3],
            "City": row[4],
            "Province": row[5],
            "Landmark": row[6],
            "EmailAddress": row[7],
            "PhoneNumber": row[8],
            "Notes": row[9]
        }

    except Exception as e:
        logger.error(f"Error retrieving delivery info: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve delivery info")

    finally:
        await cursor.close()
        await conn.close()

# --- Rider Earnings Endpoints (On-the-Fly Calculation) ---

@router.get("/rider/{rider_id}/earnings/yearly")
async def get_rider_yearly_earnings(
    rider_id: int,
    year: int = Query(..., description="The year to calculate earnings for, e.g., 2023"),
    token: str = Depends(oauth2_scheme)
):
    """
    Calculates total earnings for a specific rider for a given year.
    """
    await validate_token_and_roles(token, ["rider", "admin"])

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("""
            SELECT
                ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings,
                COUNT(o.OrderID) AS TotalDeliveries
            FROM Orders o
            WHERE o.AssignedRiderID = ?
              AND LOWER(o.Status) = 'delivered'
              AND YEAR(o.OrderDate) = ?
        """, (rider_id, year))
        row = await cursor.fetchone()

        if not row:
            # This case is unlikely with SUM/COUNT but good practice
            return {"riderID": rider_id, "year": year, "totalEarnings": 0.0, "totalDeliveries": 0}

        return {
            "riderID": rider_id,
            "year": year,
            "totalEarnings": float(row[0]),
            "totalDeliveries": row[1]
        }
    finally:
        await cursor.close()
        await conn.close()


@router.get("/rider/{rider_id}/earnings/monthly")
async def get_rider_monthly_earnings(
    rider_id: int,
    year: int = Query(..., description="The year for the month, e.g., 2023"),
    month: int = Query(..., ge=1, le=12, description="The month to calculate earnings for (1-12)"),
    token: str = Depends(oauth2_scheme)
):
    """
    Calculates total earnings for a specific rider for a given month and year.
    """
    await validate_token_and_roles(token, ["rider", "admin"])

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("""
            SELECT
                ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings,
                COUNT(o.OrderID) AS TotalDeliveries
            FROM Orders o
            WHERE o.AssignedRiderID = ?
              AND LOWER(o.Status) = 'delivered'
              AND YEAR(o.OrderDate) = ?
              AND MONTH(o.OrderDate) = ?
        """, (rider_id, year, month))
        row = await cursor.fetchone()

        if not row:
            return {"riderID": rider_id, "year": year, "month": month, "totalEarnings": 0.0, "totalDeliveries": 0}

        return {
            "riderID": rider_id,
            "year": year,
            "month": month,
            "totalEarnings": float(row[0]),
            "totalDeliveries": row[1]
        }
    finally:
        await cursor.close()
        await conn.close()


@router.get("/rider/{rider_id}/earnings/weekly")
async def get_rider_weekly_earnings(
    rider_id: int,
    target_date: date = Query(..., description="A date within the desired week (YYYY-MM-DD)"),
    token: str = Depends(oauth2_scheme)
):
    """
    Calculates total earnings for a specific rider for the week containing the target_date.
    The week is considered to run from Monday to Sunday.
    """
    await validate_token_and_roles(token, ["rider", "admin"])

    # Calculate the start of the week (Monday) and end of the week (Sunday)
    start_of_week = target_date - timedelta(days=target_date.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # Using BETWEEN for the date range. Note that OrderDate should be a DATE or DATETIME type.
        await cursor.execute("""
            SELECT
                ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings,
                COUNT(o.OrderID) AS TotalDeliveries
            FROM Orders o
            WHERE o.AssignedRiderID = ?
              AND LOWER(o.Status) = 'delivered'
              AND o.OrderDate >= ?
              AND o.OrderDate < ?
        """, (rider_id, start_of_week, end_of_week + timedelta(days=1))) # Use < next day for DATETIME compatibility
        row = await cursor.fetchone()

        if not row:
            return {
                "riderID": rider_id,
                "weekOf": str(start_of_week),
                "totalEarnings": 0.0,
                "totalDeliveries": 0
            }

        return {
            "riderID": rider_id,
            "weekOf": str(start_of_week),
            "totalEarnings": float(row[0]),
            "totalDeliveries": row[1]
        }
    finally:
        await cursor.close()
        await conn.close()


@router.get("/rider/{rider_id}/earnings/daily")
async def get_rider_daily_earnings(
    rider_id: int,
    target_date: date = Query(..., description="The date to calculate earnings for (YYYY-MM-DD)"),
    token: str = Depends(oauth2_scheme)
):
    """
    Calculates total earnings for a specific rider for a given day.
    """
    await validate_token_and_roles(token, ["rider", "admin"])

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        await cursor.execute("""
            SELECT
                ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings,
                COUNT(o.OrderID) AS TotalDeliveries
            FROM Orders o
            WHERE o.AssignedRiderID = ?
              AND LOWER(o.Status) = 'delivered'
              AND CAST(o.OrderDate AS DATE) = ?
        """, (rider_id, target_date))
        row = await cursor.fetchone()

        return {
            "riderID": rider_id,
            "date": str(target_date),
            "totalEarnings": float(row[0]) if row else 0.0,
            "totalDeliveries": row[1] if row else 0
        }
    finally:
        await cursor.close()
        await conn.close()


@router.put("/orders/{order_id}/assign-rider")
async def assign_rider(order_id: int, rider_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "staff"])  

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # update Orders table
        await cursor.execute("""
            UPDATE Orders
            SET AssignedRiderID = ?
            WHERE OrderID = ?
        """, (rider_id, order_id))

        # update RiderOrders table (insert if not exists)
        await cursor.execute("""
            SELECT RiderOrderID FROM RiderOrders WHERE OrderID = ?
        """, (order_id,))
        existing = await cursor.fetchone()

        if existing:
            await cursor.execute("""
                UPDATE RiderOrders
                SET RiderID = ?, OrderStatus = 'Assigned'
                WHERE OrderID = ?
            """, (rider_id, order_id))
        else:
            await cursor.execute("""
                INSERT INTO RiderOrders (RiderID, OrderID, OrderStatus, CompletedOrders, Earnings)
                VALUES (?, ?, 'Assigned', 0, 0)
            """, (rider_id, order_id))

        await conn.commit()
        return {"message": "Rider assigned successfully", "order_id": order_id, "rider_id": rider_id}

    finally:
        await cursor.close()
        await conn.close()


@router.post("/info", status_code=status.HTTP_201_CREATED)
async def add_delivery_info(delivery_info: DeliveryInfoRequest, token: str = Depends(oauth2_scheme)):
    username = await validate_token_and_roles(token, ["user", "admin", "staff"])

    conn = await get_db_connection()
    cursor = await conn.cursor()

    try:
        # Find latest order for this user
        await cursor.execute("""
            SELECT TOP 1 OrderID
            FROM Orders
            WHERE UserName = ? AND Status = 'Pending'
            ORDER BY OrderDate DESC
        """, (username,))
        order = await cursor.fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="No active pending order found for user")

        order_id = order[0]

        # Now check payment
        await cursor.execute("SELECT PaymentStatus FROM Orders WHERE OrderID = ?", (order_id,))
        payment = await cursor.fetchone()
        if not payment or payment[0] != "Paid":
            raise HTTPException(status_code=400, detail="Order not paid yet")

        # Insert delivery info
        await cursor.execute("""
            INSERT INTO DeliveryInfo (
                FirstName, MiddleName, LastName, Address,
                City, Province, Landmark, EmailAddress,
                PhoneNumber, Notes, OrderID
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            delivery_info.Notes,
            order_id
        ))
        await conn.commit()

    except Exception as e:
        logger.error(f"Error adding delivery info: {e}")
        raise HTTPException(status_code=500, detail="Failed to add delivery info")
    finally:
        await cursor.close()
        await conn.close()

    return {"message": "Delivery info added successfully", "order_id": order_id}

@router.put("/orders/{order_id}/status", status_code=status.HTTP_200_OK)
async def update_delivery_order_status(
    order_id: int,
    request: UpdateDeliveryStatusRequest,
    token: str = Depends(oauth2_scheme)
):
    await validate_token_and_roles(token, ["rider", "admin", "staff"])

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # Check if the order exists
        await cursor.execute("SELECT OrderID FROM Orders WHERE OrderID = ?", (order_id,))
        order = await cursor.fetchone()
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

        # Update status
        await cursor.execute(
            "UPDATE Orders SET Status = ? WHERE OrderID = ?",
            (request.status, order_id)
        )

        await conn.commit()

        # --- Update Rider Earnings if order is delivered ---
        if request.status.lower() == 'delivered':
            try:
                # Get rider ID and delivery fee
                await cursor.execute("""
                    SELECT AssignedRiderID, DeliveryFee
                    FROM Orders
                    WHERE OrderID = ?
                """, (order_id,))
                order_info = await cursor.fetchone()
                if order_info and order_info[0]:  # rider_id exists
                    rider_id = order_info[0]
                    delivery_fee = float(order_info[1]) if order_info[1] else 0.0

                    # Update RiderOrders: increment CompletedOrders and add to Earnings
                    await cursor.execute("""
                        UPDATE RiderOrders
                        SET CompletedOrders = CompletedOrders + 1,
                            Earnings = Earnings + ?
                        WHERE RiderID = ? AND OrderID = ?
                    """, (delivery_fee, rider_id, order_id))

                    await conn.commit()
                    logger.info(f"Updated earnings for rider {rider_id}, order {order_id}: +{delivery_fee}")
            except Exception as earn_err:
                logger.warning(f"Failed to update rider earnings: {earn_err}")

        # --- Notify user about status change ---
        try:
            # Fetch username, reference number, and assigned rider ID
            await cursor.execute("""
                SELECT UserName, ReferenceNumber, AssignedRiderID
                FROM Orders
                WHERE OrderID = ?
            """, (order_id,))
            order_row = await cursor.fetchone()

            if order_row:
                username, reference_number, rider_id = order_row

                notif_title = "Order Update"
                notif_type = "OrderStatus"
                notif_message = f"Your order #{reference_number} is now {request.status.capitalize()}."

                # ✅ Match lowercase "pickedup" (no space)
                if request.status.strip().lower() == "pickedup" and rider_id:
                    try:
                        async with httpx.AsyncClient() as client:
                            rider_res = await client.get(f"http://localhost:4000/users/riders/{rider_id}")
                            if rider_res.status_code == 200:
                                rider = rider_res.json()
                                rider_name = rider.get("FullName", "your rider")
                                notif_message = f"Your order #{reference_number} has been picked up by {rider_name}."
                    except Exception as re:
                        print(f"⚠️ Failed to fetch rider info for notification: {re}")

                # Send notification to Notification microservice
                async with httpx.AsyncClient() as client:
                    await client.post(
                        "http://localhost:7002/notifications/create",
                        params={
                            "username": username,
                            "title": notif_title,
                            "message": notif_message,
                            "type": notif_type,
                            "order_id": order_id,
                        },
                        headers={"Authorization": f"Bearer {token}"}
                    )

        except Exception as notify_err:
            logger.warning(f"⚠️ Failed to send notification: {notify_err}")

        logger.info(f"Updated delivery status for order {order_id} to {request.status}")
        return {"message": f"Order status successfully updated to {request.status}"}

    except Exception as e:
        await conn.rollback()
        logger.error(f"Error updating delivery order status for OrderID {order_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update order status."
        )
    finally:
        await cursor.close()
        await conn.close()

@router.get("/rider/{rider_id}/orders")
async def get_rider_orders(rider_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["rider","admin"])  # only riders

    conn = await get_db_connection()
    cursor = await conn.cursor()
    try:
        # Ensure DeliveryImage column exists
        try:
            await cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('Orders') AND name = 'DeliveryImage')
                BEGIN
                    ALTER TABLE Orders ADD DeliveryImage NVARCHAR(512) NULL
                END
            """)
            await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to create DeliveryImage column: {e}")
        
        await cursor.execute("""
            SELECT
                o.OrderID, o.UserName, o.OrderDate, o.Status, o.PaymentMethod,
                o.TotalAmount, di.FirstName, di.MiddleName, di.LastName,
                di.PhoneNumber, di.Address, di.City, di.Province, di.Notes, di.Landmark,
                o.ReferenceNumber, o.TotalDiscount, o.DeliveryFee, o.DeliveryImage
            FROM Orders o
            LEFT JOIN DeliveryInfo di ON o.OrderID = di.OrderID
            WHERE o.AssignedRiderID = ?
            ORDER BY o.OrderDate DESC
        """, (rider_id,))
        rows = await cursor.fetchall()

        orders = []
        for row in rows:
            order_id = row[0]
            username = row[1]
            reference_number = row[15] # Get the reference number

            await cursor.execute("""
                SELECT ProductName, Quantity, Price, PromoName, PromoType, PromoValue, PromoDiscountAmount
                FROM OrderItems
                WHERE OrderID = ?
            """, (order_id,))
            items = await cursor.fetchall()

            item_list = []
            for i in items:
                promo_name = i[3] if len(i) > 3 and i[3] else None
                promo_discount = float(i[6]) if len(i) > 6 and i[6] else 0.0
                
                item_dict = {
                    "name": i[0],
                    "quantity": i[1],
                    "price": float(i[2])
                }
                
                # Add promo fields if they exist
                if promo_name:
                    item_dict["promo_name"] = promo_name
                    item_dict["applied_promo"] = promo_name
                if promo_discount > 0:
                    item_dict["discount"] = promo_discount
                
                item_list.append(item_dict)

            # Compute customer details with fallback to user service if DeliveryInfo missing
            first_name = row[6]
            middle_name = row[7]
            last_name = row[8]
            phone = row[9]
            address = ", ".join(filter(None, [row[10], row[11], row[12]]))

            if not first_name:
                # Fetch from user service
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(f"http://localhost:4000/users/{username}")
                        if response.status_code == 200:
                            user_data = response.json()
                            first_name = user_data.get("firstName") or ""
                            middle_name = user_data.get("middleName") or ""
                            last_name = user_data.get("lastName") or ""
                            phone = user_data.get("phone") or phone
                            address = user_data.get("address") or address
                except Exception as e:
                    logger.warning(f"Failed to fetch user info for {username}: {e}")

            customer_name = f"{first_name or ''} {middle_name or ''} {last_name or ''}".strip() or "Unknown Customer"
            phone = phone or ""
            address = address or "Address not available"

            orders.append({
                "id": order_id,
                "referenceNumber": reference_number, # Add it to the response
                "customerName": customer_name,
                "phone": phone,
                "address": address,
                "orderedAt": row[2].strftime("%Y-%m-%d %H:%M:%S"),
                "currentStatus": row[3].lower().replace(" ", ""),
                "paymentMethod": row[4],
                "total": float(row[5]) if row[5] else 0,
                "notes": row[13],
                "items": item_list,
                "discount": float(row[16]) if row[16] else 0,
                "deliveryFee": float(row[17]) if row[17] else 0,
                "deliveryImage": row[18] if len(row) > 18 and row[18] else None
            })

        return orders

    finally:
        await cursor.close()
        await conn.close()

# --- GET Delivery Orders with Items + Delivery Info (OPTIMIZED) ---
@router.get("/admin/delivery/orders")
async def get_delivery_orders(token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin", "staff"])

    conn = await get_db_connection()
    cursor = await conn.cursor()

    try:
        # Ensure DeliveryImage column exists
        try:
            await cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID('Orders') AND name = 'DeliveryImage')
                BEGIN
                    ALTER TABLE Orders ADD DeliveryImage NVARCHAR(512) NULL
                END
            """)
            await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to create DeliveryImage column: {e}")
        
        # Step 1: Fetch all orders
        await cursor.execute("""
            SELECT
                o.OrderID, o.UserName, o.OrderDate, o.Status, o.PaymentMethod,
                o.TotalAmount, di.FirstName, di.MiddleName, di.LastName,
                di.PhoneNumber, di.Address, di.City, di.Province, di.Notes, di.Landmark,
                o.AssignedRiderID, o.TotalDiscount, o.DeliveryFee, o.DeliveryImage
            FROM Orders o
            LEFT JOIN DeliveryInfo di ON o.OrderID = di.OrderID
            WHERE o.OrderType = 'Delivery'
            ORDER BY o.OrderDate DESC
        """)
        rows = await cursor.fetchall()
        
        if not rows:
            return []
        
        order_ids = [row[0] for row in rows]
        order_dict = {row[0]: row for row in rows}

        # Step 2: Fetch ALL items for ALL orders in ONE query
        order_ids_str = ','.join(str(oid) for oid in order_ids)
        await cursor.execute(f"""
            SELECT OrderID, OrderItemID, ProductName, Quantity, Price,
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

        # Step 4: Fetch ALL unique rider info in BATCH
        unique_rider_ids = list(set(row[15] for row in rows if row[15]))
        riders_cache = {}
        if unique_rider_ids:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    # Batch fetch all riders
                    tasks = [client.get(f"http://localhost:4000/users/riders/{rid}") for rid in unique_rider_ids]
                    responses = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    for rid, response in zip(unique_rider_ids, responses):
                        if isinstance(response, Exception):
                            logger.warning(f"Failed to fetch rider {rid}: {response}")
                            continue
                        if response.status_code == 200:
                            riders_cache[rid] = response.json()
            except Exception as e:
                logger.warning(f"Batch rider fetch failed: {e}")

        # Step 5: Build response
        orders = []
        for row in rows:
            order_id = row[0]
            assigned_rider_id = row[15]

            # Get items for this order
            items = items_by_order.get(order_id, [])
            item_list = []
            for item in items:
                order_item_id = item[1]
                addons_list = addons_by_item.get(order_item_id, [])
                
                # Extract promo information
                promo_name = item[5] if len(item) > 5 and item[5] else None
                promo_discount = float(item[8]) if len(item) > 8 and item[8] else 0.0
                
                item_dict = {
                    "name": item[2],
                    "quantity": item[3],
                    "price": float(item[4]),
                    "addons": addons_list
                }
                
                # Add promo fields if they exist
                if promo_name:
                    item_dict["promo_name"] = promo_name
                    item_dict["applied_promo"] = promo_name
                if promo_discount > 0:
                    item_dict["discount"] = promo_discount
                
                item_list.append(item_dict)

            # Get cached rider info
            rider_info = riders_cache.get(assigned_rider_id)

            # Handle customer info
            first_name = row[6] or ""
            middle_name = row[7] or ""
            last_name = row[8] or ""
            customer_name = f"{first_name} {middle_name} {last_name}".strip()
            phone_number = row[9] if row[9] else None
            address_parts = [row[10], row[11], row[12]]
            address = ", ".join(filter(None, address_parts)) or "Address not available"

            orders.append({
                "id": order_id,
                "customerName": customer_name,
                "phone": phone_number,
                "address": address,
                "orderedAt": row[2].strftime("%Y-%m-%d %H:%M:%S"),
                "currentStatus": row[3].lower().replace(" ", ""),
                "paymentMethod": row[4],
                "total": float(row[5]) if row[5] else 0,
                "notes": row[13],
                "items": item_list,
                "assignedRider": str(assigned_rider_id) if assigned_rider_id else None,
                "discount": float(row[16]) if row[16] else 0,
                "deliveryFee": float(row[17]) if row[17] else 0,
                "deliveryImage": row[18] if len(row) > 18 and row[18] else None
            })

        return orders

    except Exception as e:
        logger.error(f"Error fetching delivery orders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch delivery orders")

    finally:
        await cursor.close()
        await conn.close()

@router.get("/admin/rider-earnings/aggregated/{filter}")
async def get_aggregated_rider_earnings(filter: str, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["admin"])

    conn = await get_db_connection()
    cursor = await conn.cursor()

    try:
        now = date.today()
        periods = []

        if filter.lower() == 'daily':
            periods = [now - timedelta(days=i) for i in range(7)][::-1]
        elif filter.lower() == 'weekly':
            periods = [now - timedelta(weeks=i) for i in range(4)][::-1]
        elif filter.lower() == 'monthly':
            periods = [(now.year, now.month - i) for i in range(12)]
            periods = [(y if m > 0 else y-1, m if m > 0 else m+12) for y, m in periods]
            periods.reverse()
        else:
            raise HTTPException(status_code=400, detail="Invalid filter")

        # Aggregate earnings per period
        period_earnings = []
        for period in periods:
            if filter.lower() == 'daily':
                query = """
                SELECT ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings
                FROM Orders o
                WHERE LOWER(o.Status) = 'delivered' AND CAST(o.OrderDate AS DATE) = ?
                """
                params = (period,)
            elif filter.lower() == 'weekly':
                start = period - timedelta(days=period.weekday())
                end = start + timedelta(days=6)
                query = """
                SELECT ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings
                FROM Orders o
                WHERE LOWER(o.Status) = 'delivered' AND o.OrderDate >= ? AND o.OrderDate < ?
                """
                params = (start, end + timedelta(days=1))
            elif filter.lower() == 'monthly':
                year, month = period
                query = """
                SELECT ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings
                FROM Orders o
                WHERE LOWER(o.Status) = 'delivered' AND YEAR(o.OrderDate) = ? AND MONTH(o.OrderDate) = ?
                """
                params = (year, month)

            await cursor.execute(query, params)
            row = await cursor.fetchone()
            earnings = float(row[0]) if row else 0.0
            period_earnings.append(earnings)

        # For top riders, sum earnings over the periods
        rider_totals = {}
        if filter.lower() == 'daily':
            start_date = periods[0]
            end_date = periods[-1]
            rider_query = """
            SELECT o.AssignedRiderID, ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings
            FROM Orders o
            WHERE LOWER(o.Status) = 'delivered' AND CAST(o.OrderDate AS DATE) >= ? AND CAST(o.OrderDate AS DATE) <= ?
            GROUP BY o.AssignedRiderID
            """
            params_rider = (start_date, end_date)
        elif filter.lower() == 'weekly':
            start_date = periods[0] - timedelta(days=periods[0].weekday())
            end_date = periods[-1] + timedelta(days=6 - periods[-1].weekday())
            rider_query = """
            SELECT o.AssignedRiderID, ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings
            FROM Orders o
            WHERE LOWER(o.Status) = 'delivered' AND o.OrderDate >= ? AND o.OrderDate <= ?
            GROUP BY o.AssignedRiderID
            """
            params_rider = (start_date, end_date)
        elif filter.lower() == 'monthly':
            min_year = min(p[0] for p in periods)
            max_year = max(p[0] for p in periods)
            min_month = min(p[1] for p in periods if p[0] == min_year)
            max_month = max(p[1] for p in periods if p[0] == max_year)
            rider_query = """
            SELECT o.AssignedRiderID, ISNULL(SUM(o.DeliveryFee), 0) AS TotalEarnings
            FROM Orders o
            WHERE LOWER(o.Status) = 'delivered' AND YEAR(o.OrderDate) >= ? AND YEAR(o.OrderDate) <= ? AND MONTH(o.OrderDate) >= ? AND MONTH(o.OrderDate) <= ?
            GROUP BY o.AssignedRiderID
            """
            params_rider = (min_year, max_year, min_month, max_month)

        await cursor.execute(rider_query, params_rider)
        rider_rows = await cursor.fetchall()
        rider_totals = {row[0]: float(row[1]) for row in rider_rows if row[0]}

        # Get rider names
        top_riders = []
        for rider_id, earnings in rider_totals.items():
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"http://localhost:4000/users/riders/{rider_id}")
                    if response.status_code == 200:
                        rider_data = response.json()
                        name = rider_data.get("FullName", f"Rider {rider_id}")
                        top_riders.append({"name": name, "earnings": earnings})
            except Exception as e:
                logger.warning(f"Failed to fetch rider info for {rider_id}: {e}")
                top_riders.append({"name": f"Rider {rider_id}", "earnings": earnings})

        top_riders.sort(key=lambda x: x['earnings'], reverse=True)
        top_riders = top_riders[:10]

        # Format periods
        formatted_periods = []
        for i, p in enumerate(periods):
            if filter.lower() == 'daily':
                name = p.strftime('%b %d')
            elif filter.lower() == 'weekly':
                name = f"Week of {p.strftime('%b %d')}"
            elif filter.lower() == 'monthly':
                name = date(p[0], p[1], 1).strftime('%b')
            formatted_periods.append({"name": name, "earnings": period_earnings[i]})

        return {"periods": formatted_periods, "topRiders": top_riders}

    except Exception as e:
        logger.error(f"Error fetching aggregated rider earnings: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch rider earnings")

    finally:
        await cursor.close()
        await conn.close()


@router.get("/orders/{order_id}/reference-number")
async def get_reference_number(order_id: int, token: str = Depends(oauth2_scheme)):
    await validate_token_and_roles(token, ["user", "admin", "rider"])

    conn = await get_db_connection()
    cursor = await conn.cursor()

    try:
        await cursor.execute(
            "SELECT ReferenceNumber FROM Orders WHERE OrderID = ?", (order_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=404, detail="Order not found"
            )

        return {"ReferenceNumber": row[0]}

    except Exception as e:
        logger.error(f"Error retrieving reference number: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve reference number"
        )

    finally:
        await cursor.close()
        await conn.close()
