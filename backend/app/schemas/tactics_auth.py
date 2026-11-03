"""Strict ordinary-account request bodies; never trim passwords."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

Username = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_]{3,32}$")]
Password = Annotated[str, StringConstraints(strict=True, min_length=12, max_length=128)]
DisplayName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32)]


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterBody(EmptyBody):
    username: Username
    display_name: DisplayName
    password: Password


class LoginBody(EmptyBody):
    username: Username
    password: Password


class ProfileBody(EmptyBody):
    display_name: DisplayName


class PasswordBody(EmptyBody):
    current_password: Password
    new_password: Password


class RecoveryCodeBody(EmptyBody):
    current_password: Password


class RecoverBody(EmptyBody):
    username: Username
    recovery_code: Annotated[
        str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$")
    ]
    new_password: Password
