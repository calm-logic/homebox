from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..auth import clear_session, issue_session, require_session_api, verify_credentials

router = APIRouter(prefix="/api/auth")


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody, response: Response):
    if not verify_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    issue_session(response, body.username)
    return {"ok": True, "username": body.username}


@router.post("/logout")
async def logout(response: Response):
    clear_session(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = require_session_api(request)
    return {"username": user}
