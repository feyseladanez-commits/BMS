import os, tempfile
d = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{d}/t.db"); os.environ.setdefault("UPLOAD_DIR", f"{d}/up")
os.environ.setdefault("ADMIN_EMAIL", "a@x.org"); os.environ.setdefault("ADMIN_PASSWORD", "adminpass1")
from fastapi.testclient import TestClient
from app.main import app, totp
def test_mfa_tax_export_ratelimit():
    with TestClient(app) as c:
        L = lambda **k: c.post("/api/v1/auth/login", json=k)
        a = {"Authorization": "Bearer " + L(email="a@x.org", password="adminpass1").json()["token"]}
        c.post("/api/v1/admin/users", headers=a, json={"email": "b@x.org", "name": "B", "password": "adminpass2", "role": "admin"})
        h = {"Authorization": "Bearer " + L(email="b@x.org", password="adminpass2").json()["token"]}
        s = c.post("/api/v1/me/mfa/setup", headers=h).json()["secret"]
        assert c.post("/api/v1/me/mfa/enable", headers=h, json={"secret": s, "code": "000000"}).status_code == 400
        assert c.post("/api/v1/me/mfa/enable", headers=h, json={"secret": s, "code": totp(s)}).status_code == 200
        assert L(email="b@x.org", password="adminpass2").status_code == 401  # code now required
        assert L(email="b@x.org", password="adminpass2", code=totp(s)).status_code == 200
        assert c.post("/api/v1/tax/rules", headers=h, json={"version": "TEST-1", "rate": "0.10"}).status_code == 200
        assert c.get("/api/v1/reports/buildings.csv", headers=h).status_code == 200
        assert c.get("/api/v1/admin/integrations", headers=h).json()["fayda"].startswith("not_connected")
        codes = [L(email="zz@x.org", password="nope").status_code for _ in range(6)]
        assert codes[-1] == 429
