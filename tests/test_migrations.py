"""The migrations build exactly the schema the code expects, so a production database never drifts from the models."""


def test_migrations_match_the_models(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    from conftest import ROOT
    from workforce import db, models, settings  # noqa: F401  (models registers the tables)
    monkeypatch.setenv("WORKFORCE_DATABASE_URL", f"sqlite:///{tmp_path / 'migrated.db'}")
    settings.get_settings.cache_clear()
    db.configure()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    with db.engine().connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), db.Base.metadata) == []
    db.engine().dispose()
    settings.get_settings.cache_clear()
