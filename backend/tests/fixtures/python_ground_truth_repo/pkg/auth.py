def verify_token(token: str) -> bool:
    return bool(token)


def dynamic_lookup(target: object, name: str):
    return getattr(target, name)()


if True:
    conditional_value = "enabled"
else:
    conditional_value = "disabled"
