def test_healthz_returns_service_identity(client):
    response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {"ok": True, "service": "eason-one"}


def test_build_info_returns_service_and_application_version(client):
    response = client.get("/api/build-info")

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {"service": "eason-one", "version": "0.20.0"}
