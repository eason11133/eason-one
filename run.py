import os

from eason_one import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("EASON_ONE_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
    # The Company Runtime is process-owned. Never let Werkzeug's development
    # reloader spawn a second runtime owner against the same local database.
    app.run(debug=debug, use_reloader=False)
