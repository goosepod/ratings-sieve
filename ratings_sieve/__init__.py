"""AudioMuse plugin entry point; no production modifications at import time."""


def register(ctx):
    from .web import create_blueprint
    from .cache import audiomuse_cache, migrate_audiomuse

    ctx.on_install(migrate_audiomuse)
    ctx.add_blueprint(create_blueprint(cache_factory=audiomuse_cache))
