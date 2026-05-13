from pydantic import BaseModel, EmailStr


class SignUpRequest(BaseModel):
    username: str
    password: str


class SignInRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    is_active: bool

    class Config:
        from_attributes = True
