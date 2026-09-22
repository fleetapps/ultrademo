def bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


INTERNAL = bearer("test-internal")
