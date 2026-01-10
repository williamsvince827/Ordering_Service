from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordBearer
from services.promo_service import fetch_active_promos

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost:4000/auth/token")
router = APIRouter(prefix="/debug", tags=["Debug"])

@router.get("/promos")
async def debug_promos(token: str = Depends(oauth2_scheme)):
    promos = await fetch_active_promos(token)
    return {
        "count": len(promos),
        "promos": promos
    }
