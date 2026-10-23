import json
from typing import Literal
from pydantic import BaseModel, EmailStr
from fastapi import HTTPException, Form


class SignupForm(BaseModel):
    name: str
    email: EmailStr
    password: str


class LoginForm(BaseModel):
    email: EmailStr
    password: str


class VerifyOTP(BaseModel):
    email: EmailStr
    otp: str


class UserMeResponse(BaseModel):
    name: str | None
    tokens: int

    class Config:
        from_attributes = True


class ProjectInfo(BaseModel):
    name: str
    design_code: str = "IBC 2024"
    design_method: str = "ASD"
    risk_category: str
    site_class: str
    exposure_category: Literal["B", "C", "D"] = "B"
    latitude_scaled: float
    longitude_scaled: float


# 2. Create a helper dependency to safely parse the JSON string from the form
def get_project_info(agent_config: str = Form(...)) -> ProjectInfo:
    try:
        data = json.loads(agent_config)
        return ProjectInfo(**data)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid Project Information.")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    image: str | None = None  # data URL (data:image/...;base64,...)


class RequestPasswordReset(BaseModel):
    email: EmailStr
