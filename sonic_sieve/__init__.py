"""AudioMuse plugin entry point; no production modifications at import time."""


def register(ctx):
    from .web import create_blueprint

    ctx.add_blueprint(create_blueprint())
