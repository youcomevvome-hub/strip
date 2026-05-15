from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Header, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import User

settings = get_settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
ALGO = "HS256"


def hash_password(p: str) -> str:
    return pwd.hash(p)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd.verify(plain, hashed)


def create_token(sub: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": sub, "exp": exp}, settings.SECRET_KEY, algorithm=ALGO)


def _user_from_jwt(token: str, db: Session) -> User | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGO])
        email = payload.get("sub")
    except JWTError:
        return None
    if not email:
        return None
    return db.query(User).filter_by(email=email).first()


def get_current_principal(
    db: Annotated[Session, Depends(get_db)],
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> User | str:
    """Accept either a JWT (UI users) or static API key (machine clients)."""
    if x_api_key and x_api_key == settings.API_KEY:
        return "api-key"
    if token:
        user = _user_from_jwt(token, db)
        if user:
            return user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
