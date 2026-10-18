from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from Auth.schemas import (
    ChangePasswordSchema,
    ForgotPasswordSchema,
    LoginSchema,
    LogoutSchema,
    MessageResponse,
    RefreshSchema,
    ResetPasswordSchema,
    SignupSchema,
    TokenResponse,
)
from Auth.services.jwt import get_current_user
from Auth.services.limiter import check_email_limit, check_user_limit, rate_limit
from Auth.services.mailer import send_otp_email
from Auth.services.otp import clear_otp, generate_otp, is_otp_verified, verify_otp
from Auth.services.password import hash_password, verify_password
from Auth.services.refresh import (
    issue_tokens,
    refresh_session,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
)
from Database.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=201,
    dependencies=[Depends(rate_limit("signup", 5, 3600))],
)
def signup(data: SignupSchema):
    email = data.email.lower()
    if User.get_or_none(User.email == email):
        raise HTTPException(400, "Email already registered")
    user = User.create(
        name=data.name.strip(),
        email=email,
        password_hash=hash_password(data.password),
    )
    return issue_tokens(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit("login", 10, 60))],
)
def login(data: LoginSchema):
    check_email_limit("login", data.email, 5, 900)
    user = User.get_or_none(User.email == data.email.lower())
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return issue_tokens(user)


@router.post(
    "/session/refresh",
    response_model=TokenResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(rate_limit("refresh", 30, 60))],
)
def session_refresh(data: RefreshSchema):
    return refresh_session(data.refresh_token, data.type)


@router.post(
    "/logout",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("logout", 30, 60))],
)
def logout(data: LogoutSchema):
    revoke_refresh_token(data.refresh_token)
    return {"message": "Logged out"}


@router.post(
    "/change-password",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit("change-password", 20, 60))],
)
def change_password(data: ChangePasswordSchema, user: User = Depends(get_current_user)):
    check_user_limit("change-password", user.id, 5, 900)
    if user.password_hash and not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "Old password is incorrect")
    user.password_hash = hash_password(data.new_password)
    user.save()
    revoke_all_refresh_tokens(user)
    return issue_tokens(user)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("forgot", 5, 3600))],
)
def forgot_password(data: ForgotPasswordSchema, background: BackgroundTasks):
    check_email_limit("forgot", data.email, 3, 3600)
    user = User.get_or_none(User.email == data.email.lower())
    if user:
        code = generate_otp(user)
        background.add_task(send_otp_email, user.email, code)
    return {"message": "If the email exists, a code has been sent"}


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("reset", 10, 900))],
)
def reset_password(data: ResetPasswordSchema):
    check_email_limit("reset", data.email, 10, 900)
    user = User.get_or_none(User.email == data.email.lower())
    if not user:
        raise HTTPException(400, "Invalid or expired code")
    if data.type == "verify":
        verify_otp(user, data.otp)
        return {"message": "Code verified"}
    if not is_otp_verified(user):
        raise HTTPException(400, "Code not verified or expired")
    user.password_hash = hash_password(data.password)
    user.save()
    clear_otp(user)
    revoke_all_refresh_tokens(user)
    return {"message": "Password changed"}
