from sqlalchemy import create_engine, text

from apps.worker.core.config import settings


def check_database(database_url: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def main() -> None:
    check_database(settings.database_url)


if __name__ == "__main__":
    main()
