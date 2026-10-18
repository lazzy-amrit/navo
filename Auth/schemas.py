from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


class SignupSchema(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginSchema(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordSchema(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class RefreshSchema(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)
    type: Literal["access", "both"]


class LogoutSchema(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class ForgotPasswordSchema(BaseModel):
    email: EmailStr


class ResetPasswordSchema(BaseModel):
    email: EmailStr
    type: Literal["verify", "change"]
    otp: Optional[str] = Field(default=None, pattern=r"^\d{6}$")
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def check_fields(self):
        if self.type == "verify" and not self.otp:
            raise ValueError("otp is required")
        if self.type == "change" and not self.password:
            raise ValueError("password is required")
        return self


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str
