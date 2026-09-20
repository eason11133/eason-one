from eason_one import create_app
from eason_one.extensions import db
from eason_one.services.vnext_backbone import bootstrap_vnext

app = create_app()
with app.app_context():
    db.create_all()
    bootstrap_vnext()
    print("v0.15 migration/backfill complete")
