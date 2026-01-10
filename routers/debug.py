from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordBearer
from services.promo_service import fetch_active_promos

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="https://authservices-npr8.onrender.com/auth/token")
router = APIRouter(prefix="/debug", tags=["Debug"])

@router.get("/promos")
async def debug_promos(token: str = Depends(oauth2_scheme)):
    promos = await fetch_active_promos(token)
    return {
        "count": len(promos),
        "promos": promos
    }
