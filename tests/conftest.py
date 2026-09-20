import pytest
from pathlib import Path
from eason_one import create_app
from eason_one.extensions import db
from eason_one.seed import seed
from tests.release_contract import CURRENT_REGRESSION, classification


def pytest_ignore_collect(collection_path, config):
    path = Path(str(collection_path))
    try:
        relative = path.resolve().relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        return False
    if not relative.startswith("test_") and not relative.startswith("acceptance/test_"):
        return False
    return classification(relative) != "CURRENT_REGRESSION"

@pytest.fixture()
def app(tmp_path):
    app=create_app({"TESTING":True,"SQLALCHEMY_DATABASE_URI":f"sqlite:///{tmp_path/'test.db'}"})
    with app.app_context():
        db.drop_all(); db.create_all(); seed()
        yield app
        db.session.remove()

@pytest.fixture()
def ctx(app):
    with app.app_context(): yield

@pytest.fixture()
def client(app): return app.test_client()
