"""POST /api/token — Discord OAuth code 交換エンドポイント。

Activity フロントは sdk.commands.authorize で受け取った code を
このエンドポイントに POST し、返ってきた access_token を
sdk.commands.authenticate に渡す。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .auth import AuthError, exchange_code

router = APIRouter()


class TokenRequest(BaseModel):
    code: str


class TokenResponse(BaseModel):
    access_token: str


@router.post("/api/token", response_model=TokenResponse)
async def token(req: TokenRequest) -> TokenResponse:
    try:
        access_token = await exchange_code(req.code)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return TokenResponse(access_token=access_token)
