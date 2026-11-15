"""Apply schema revisions as one deployment job before starting API replicas."""

from app.database import check_database_ready, get_engine, init_database


def main() -> None:
    engine = get_engine()
    try:
        init_database()
        check_database_ready()
        print("Database migrations complete; schema is ready.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
