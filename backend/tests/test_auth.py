def test_login_success_returns_jwt(client, seeded_user):
    response = client.post(
        "/auth/login",
        json={"email": seeded_user.email, "password": "TestPassword123!"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]


def test_login_wrong_password_rejected(client, seeded_user):
    response = client.post(
        "/auth/login",
        json={"email": seeded_user.email, "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_me_requires_token(client):
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_me_returns_current_user(client, seeded_user):
    login_response = client.post(
        "/auth/login",
        json={"email": seeded_user.email, "password": "TestPassword123!"},
    )
    token = login_response.json()["access_token"]

    me_response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_response.status_code == 200
    body = me_response.json()
    assert body["email"] == seeded_user.email
    assert body["role"] == "reviewer_l2"
    assert body["company_id"] == str(seeded_user.company_id)
    assert body["company_name"] == "Test Company"
    assert body["is_platform_admin"] is False
