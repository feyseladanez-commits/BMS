import os, tempfile
d = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{d}/t.db", UPLOAD_DIR=f"{d}/up", ADMIN_EMAIL="a@x.org", ADMIN_PASSWORD="adminpass1")
from fastapi.testclient import TestClient
from app.main import app
P = "/api/v1/"
def tok(c, e, p): return {"Authorization": "Bearer " + c.post(P + "auth/login", json={"email": e, "password": p}).json()["token"]}

def test_structure_photos_and_tenant_documents():
    with TestClient(app) as c:
        a = tok(c, "a@x.org", "adminpass1")
        org = c.post(P + "admin/orgs", headers=a, json={"name": "Struct woreda office", "kind": "woreda", "woreda": "StructW", "account": {"name": "Off", "email": "st-off@x.org", "password": "officerpw1"}}).json()
        assert c.post(P + "admin/orgs", headers=a, json={"name": "Struct docs office", "kind": "woreda", "woreda": "StructW", "powers": ["registry.read", "tenant.docs"],
                      "account": {"name": "Docs", "email": "st-docs@x.org", "password": "officerpw1"}}).status_code == 200
        c.post(P + "auth/register", json={"email": "st-own@x.org", "name": "Own", "password": "ownerpass1"}); o = tok(c, "st-own@x.org", "ownerpass1")
        mk = {"name": "Struct Tower", "woreda": "StructW", "kebele": "K1", "lat": 9.0, "lng": 38.7, "floors": 2}
        assert c.post(P + "buildings", headers=o, json=mk).status_code == 422  # sub-city is required
        b = c.post(P + "buildings", headers=o, json={**mk, "subcity": "Bole"}).json(); B = P + f"buildings/{b['id']}/"
        assert b["subcity"] == "Bole"
        f1 = c.post(B + "floors", headers=o, json={"label": "G", "name": "Ground", "m2": "100"}).json()
        assert c.post(B + "floors", headers=o, json={"label": "G"}).status_code == 409
        f2 = c.post(B + "floors", headers=o, json={"label": "1"}).json()
        assert c.post(B + "floors", headers=o, json={"label": "2"}).status_code == 400  # only 2 floors declared
        s1 = c.post(B + "units", headers=o, json={"floor_id": f1["id"], "number": "G1", "m2": "60", "shop_name": "Abel Barber"}); assert s1.status_code == 200, s1.text
        assert c.post(B + "units", headers=o, json={"floor_id": f1["id"], "number": "G2", "m2": "50"}).status_code == 400  # 60 + 50 > 100 m2 floor
        assert c.post(B + "units", headers=o, json={"floor_id": f1["id"], "number": "G2", "m2": "40"}).status_code == 200
        un = s1.json()["id"]
        ph = lambda name, data: c.post(P + f"units/{un}/photos", headers=o, files={"file": (name, data)})
        assert ph("shop.pdf", b"x").status_code == 400 and ph("shop.jpg", b"jpgdata").status_code == 200
        assert c.post(B + "submit", headers=o).status_code == 400  # front photo missing
        c.post(B + "files", headers=o, files={"file": ("f.png", b"x" * 5)}, data={"category": "front"})
        assert c.post(B + "submit", headers=o).status_code == 200
        # a contract needs an approved building
        k = {"unit_id": un, "tenant_name": "Abel", "start_date": "2027-01-01", "end_date": "2027-12-31", "rent": 100, "tenant_tin": "0012345678", "trade_license_no": "TL-77"}
        assert c.post(P + "contracts", headers=o, json=k).status_code == 400
        off = tok(c, "st-off@x.org", "officerpw1")
        assert c.post(B + "review", headers=off, json={"decision": "approve"}).status_code == 200
        ct = c.post(P + "contracts", headers=o, json=k).json(); assert ct["tenant_tin"] == "0012345678"
        doc = lambda cat, name="id.pdf": c.post(P + f"contracts/{ct['id']}/files", headers=o, files={"file": (name, b"pdfdata")}, data={"category": cat, "doc_no": "ID-1", "expires": "2030-01-01"})
        assert doc("passport_selfie").status_code == 400 and doc("tenant_id", "id.exe").status_code == 400
        d1 = doc("tenant_id"); assert d1.status_code == 200 and d1.json()["doc_no"] == "ID-1"
        assert c.post(P + f"contracts/{ct['id']}/files", headers=o, files={"file": ("x.pdf", b"x")}, data={"category": "other", "expires": "soon"}).status_code == 400
        c.post(P + f"contracts/{ct['id']}/approve", headers=off)
        pay = lambda **k: c.post(P + f"contracts/{ct['id']}/payments", headers=o, files={"file": ("r.pdf", b"receipt")}, data={"period": "2027-01", "amount": "100", "paid_on": "2025-01-05", "method": "telebirr", **k})
        assert pay(paid_on="2999-01-01").status_code == 400 and pay(period="2027-13").status_code == 400 and pay(amount="0").status_code == 400
        assert c.post(P + f"contracts/{ct['id']}/payments", headers=o, data={"period": "2027-01", "amount": "100", "paid_on": "2025-01-05"}).status_code == 422  # receipt required
        pr = pay(reference="TX123"); assert pr.status_code == 200, pr.text
        t0 = c.get(B + "structure", headers=o).json()["floors"][0]["shops"][0]["tenants"][0]
        assert t0["payments"][0]["receipt_id"] and float(t0["paid_total"]) == 100
        assert c.get(B + "structure", headers=off).json()["floors"][0]["shops"][0]["tenants"][0]["payments"][0]["receipt_id"] is None  # receipt hidden without tenant.docs
        assert c.delete(P + f"files/{t0['payments'][0]['receipt_id']}", headers=o).status_code == 400
        assert c.delete(P + f"payments/{pr.json()['id']}", headers=o).status_code == 200
        st = c.get(B + "structure", headers=o).json()
        shop = st["floors"][0]["shops"][0]
        assert st["floors"][0]["label"] == "G" and len(shop["photos"]) == 1 and shop["tenants"][0]["docs"][0]["category"] == "tenant_id"
        assert str(st["floors"][0]["used_m2"]) == "100.00" or float(st["floors"][0]["used_m2"]) == 100
        # a woreda office without tenant.docs sees the structure but not the documents; with the power it can open them
        sv = c.get(B + "structure", headers=off).json()["floors"][0]["shops"][0]["tenants"][0]
        assert sv["docs"] == [] and sv["docs_hidden"] is True and c.get(P + f"files/{d1.json()['id']}", headers=off).status_code == 404
        dk = tok(c, "st-docs@x.org", "officerpw1")
        assert c.get(P + f"files/{d1.json()['id']}", headers=dk).content == b"pdfdata"
        assert any(x["action"] == "tenant_document_viewed" for x in c.get(P + "admin/audit", headers=a).json())
        assert c.get(P + f"files/{d1.json()['id']}", headers=a).status_code == 404  # the administrator has no access to tenant documents
        # another owner cannot reach any of it
        c.post(P + "auth/register", json={"email": "st-o2@x.org", "name": "O2", "password": "ownerpass1"}); o2 = tok(c, "st-o2@x.org", "ownerpass1")
        assert c.get(B + "structure", headers=o2).status_code == 404 and c.get(P + f"files/{d1.json()['id']}", headers=o2).status_code == 404
        # removing: shop photo yes; a document of an approved contract no
        assert c.delete(P + f"files/{shop['photos'][0]['id']}", headers=o).status_code == 200
        assert c.delete(P + f"files/{d1.json()['id']}", headers=o).status_code == 400
        assert c.delete(P + f"floors/{f1['id']}", headers=o).status_code == 400  # still has shops
