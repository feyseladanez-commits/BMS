import os, tempfile
d = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{d}/t.db", UPLOAD_DIR=f"{d}/up", ADMIN_EMAIL="a@x.org", ADMIN_PASSWORD="adminpass1")
from fastapi.testclient import TestClient
from app.main import app
def tok(c, e, p): return {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"email": e, "password": p}).json()["token"]}
def test_flow():
    with TestClient(app) as c:
        a = tok(c, "a@x.org", "adminpass1")
        for e, w in (("o1@x.org", "W3"), ("o2@x.org", "W9")):
            assert c.post("/api/v1/admin/users", headers=a, json={"email": e, "name": "Off", "password": "officerpw1", "role": "officer", "woreda": w}).status_code == 200
        assert c.post("/api/v1/auth/register", json={"email": "own@x.org", "name": "Own", "password": "ownerpass1"}).status_code == 200
        assert c.post("/api/v1/auth/login", json={"email": "own@x.org", "password": "bad"}).status_code == 401
        o = tok(c, "own@x.org", "ownerpass1"); P = "/api/v1/"
        b = c.post(P + "buildings", headers=o, json={"subcity": "Sub", "name": "Plaza", "woreda": "W3", "kebele": "K1", "lat": 9.0, "lng": 38.7, "floors": 3}).json()
        assert c.post(P + "buildings", headers=o, json={"subcity": "Sub", "name": "X", "woreda": "W3", "kebele": "K1", "lat": 90, "lng": 38.7, "floors": 3}).status_code == 422
        assert c.post(P + f"buildings/{b['id']}/submit", headers=o).status_code == 400  # no photo yet
        assert c.post(P + f"buildings/{b['id']}/files", headers=o, files={"file": ("a.png", b"x" * 10)}, data={"category": "front"}).status_code == 200
        assert c.post(P + f"buildings/{b['id']}/submit", headers=o).status_code == 400  # floors not described yet
        for lab in ("G", "1", "2"): assert c.post(P + f"buildings/{b['id']}/floors", headers=o, json={"label": lab}).status_code == 200
        assert c.post(P + f"buildings/{b['id']}/submit", headers=o).status_code == 200
        assert c.post(P + f"buildings/{b['id']}/review", headers=tok(c, "o2@x.org", "officerpw1"), json={"decision": "approve"}).status_code == 404  # other woreda
        r = c.post(P + f"buildings/{b['id']}/review", headers=tok(c, "o1@x.org", "officerpw1"), json={"decision": "approve"}).json()
        assert r["brn"].startswith("ET-") and r["issuer"] == "W3 woreda office"
        u = c.post(P + f"buildings/{b['id']}/units", headers=o, json={"floor": "G", "number": "G1", "m2": "125.50"}).json()
        assert c.post(P + f"buildings/{b['id']}/units", headers=o, json={"floor": "G", "number": "G1", "m2": "10"}).status_code == 409
        assert c.post(P + f"buildings/{b['id']}/units", headers=o, json={"floor": "G", "number": "G2", "m2": "0"}).status_code == 422
        k = lambda s, e: c.post(P + "contracts", headers=o, json={"unit_id": u["id"], "tenant_name": "T", "start_date": s, "end_date": e, "rent": 100})
        first = k("2027-01-01", "2027-12-31"); assert first.status_code == 200
        assert k("2027-06-01", "2028-06-01").status_code == 409
        assert k("2028-01-01", "2028-12-31").status_code == 200
        assert c.get(f"/verify/{r['tok']}").status_code == 200 and r["brn"] in c.get(f"/verify/{r['tok']}").text
        ho = tok(c, "o1@x.org", "officerpw1"); cid = first.json()["id"]
        assert c.post(P + "inspections", headers=ho, json={"building_id": b["id"], "notes": "ok", "lat": 9.0, "lng": 38.7}).status_code == 200
        assert c.post(P + "tax/rules", headers=a, json={"version": "T2", "rate": "0.1"}).status_code == 200
        dd = c.post(P + "tax/declarations", headers=o, json={"contract_id": cid, "period": "2027-01", "declared": "100"}); assert dd.status_code == 200 and dd.json()["estimate_only"]
        assert c.post(P + f"contracts/{cid}/approve", headers=ho).status_code == 200
        assert c.post(P + "disputes", headers=ho, json={"contract_id": cid, "reason": "double claim"}).status_code == 200
        assert k("2027-03-01", "2027-04-01").status_code == 409  # frozen contract still blocks the unit
        assert c.get(P + "reports/contracts.xlsx", headers=o).content[:2] == b"PK"
