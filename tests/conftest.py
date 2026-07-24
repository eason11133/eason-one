import pytest
from eason_one import create_app
from eason_one.extensions import db
from eason_one.seed import seed

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
