"""Create the production Argon2id value without echoing or persisting a password."""

from __future__ import annotations

import base64
import getpass
import sys

from argon2 import PasswordHasher


def create_hash() -> str:
    password = getpass.getpass("管理员密码（不会显示）：")
    confirmation = getpass.getpass("再次输入管理员密码：")
    if not password:
        raise ValueError("密码不能为空")
    if password != confirmation:
        raise ValueError("两次密码不一致")
    hasher = PasswordHasher(
        time_cost=3,
        memory_cost=65536,
        parallelism=2,
        hash_len=32,
        salt_len=16,
    )
    return base64.b64encode(hasher.hash(password).encode("utf-8")).decode("ascii")


def main() -> int:
    try:
        value = create_hash()
    except (EOFError, KeyboardInterrupt):
        print("已取消。", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"ADMIN_PASSWORD_HASH_B64={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
