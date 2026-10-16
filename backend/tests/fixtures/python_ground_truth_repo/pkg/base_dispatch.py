class Grandparent:
    def run(self) -> str:
        return "grandparent"


class Left(Grandparent):
    def run(self) -> str:
        return "left"


class Right(Grandparent):
    def run(self) -> str:
        return "right"


class Child(Left, Right):
    pass


class ExternalChild:
    """Concrete builtin-base example alongside the unresolved external base case."""


def dispatch(child: Child) -> str:
    return child.run()
