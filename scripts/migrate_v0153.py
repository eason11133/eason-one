from eason_one import create_app

app = create_app({"AUTO_START_OPERATION_RUNTIME": False})
with app.app_context():
    service = __import__(
        "eason_one.services.project_company",
        fromlist=["run_founder_surface_maintenance"],
    )
    result = service.run_founder_surface_maintenance(start_runtime=False)
    print(
        "v0.15.3 maintenance complete: "
        f"completed_attention={len(result['completed_attention'])}, "
        f"budget_gates={len(result['budget_gates'])}, "
        f"project_proposals={len(result['project_proposals'])}"
    )
