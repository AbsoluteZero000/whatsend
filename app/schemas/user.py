from pydantic import BaseModel, EmailStr


class SignUpRequest(BaseModel):
    username: str
    email: str
    password: str


class SignInRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool

    class Config:
        from_attributes = True
