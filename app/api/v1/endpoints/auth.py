from fastapi import APIRouter, Depends, status

from app.api.deps import get_current_user
from app.schemas.auth import (
    EmailOnlyRequest,
    LoginRequest,
    MessageResponse,
    ResetPasswordRequest,
    SignupRequest,
    TokenRequest,
    TokenResponse,
    UserOut,
)
from app.services.auth_service import auth_service, to_user_out

router = APIRouter()


@router.post("/signup", response_model=MessageResponse, status_code=status.HTTP_201_CREATED, summary="Create an account")
def signup(data: SignupRequest):
    user = auth_service.signup(data)
    return {"message": f"Account created. We sent a verification link to {user.email}."}


@router.post("/login", response_model=TokenResponse, summary="Log in and receive a JWT")
def login(data: LoginRequest):
    return auth_service.login(data.email, data.password)


@router.post("/verify-email", response_model=MessageResponse, summary="Confirm an email address")
def verify_email(data: TokenRequest):
    auth_service.verify_email(data.token)
    return {"message": "Your email is verified. You can now log in."}


@router.post("/resend-verification", response_model=MessageResponse, summary="Send a new verification link")
def resend_verification(data: EmailOnlyRequest):
    auth_service.resend_verification(data.email)
    return {"message": "If that account exists and isn't verified yet, a new verification link is on its way."}


@router.post("/forgot-password", response_model=MessageResponse, summary="Email a password reset link")
def forgot_password(data: EmailOnlyRequest):
    auth_service.forgot_password(data.email)
    return {"message": "If an account exists for that email, a password reset link is on its way."}


@router.post("/reset-password", response_model=MessageResponse, summary="Set a new password with a reset token")
def reset_password(data: ResetPasswordRequest):
    auth_service.reset_password(data.token, data.new_password)
    return {"message": "Your password has been reset. You can now log in."}


@router.get("/me", response_model=UserOut, summary="Current logged-in user")
def me(user: dict = Depends(get_current_user)):
    return to_user_out(user)
