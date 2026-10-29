from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="서버 상태 확인")
def health() -> dict:
    return {"status": "ok"}
