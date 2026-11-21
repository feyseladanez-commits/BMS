import os, tempfile
d = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{d}/t.db"); os.environ.setdefault("UPLOAD_DIR", f"{d}/up")
os.environ.setdefault("ADMIN_EMAIL", "a@x.org"); os.environ.setdefault("ADMIN_PASSWORD", "adminpass1")
from fastapi.testclient import TestClient
from sqlalchemy import select, create_engine, text, inspect
from app.main import app, startup, hpw, totp, SessionLocal, User, Organization
import app.main as main
P = "/api/v1/"
PW = "officerpw1"


def tok(c, e, p): return {"Authorization": "Bearer " + c.post(P + "auth/login", json={"email": e, "password": p}).json()["token"]}


def admin(c): return tok(c, "a@x.org", "adminpass1")


def make_org(c, a, **k):
    r = c.post(P + "admin/orgs", headers=a, json=k)
    assert r.status_code == 200, r.text
    return r.json()


def acct(email, name="Staff"): return {"email": email, "name": name, "password": PW}


def owner(c, email):
    assert c.post(P + "auth/register", json={"email": email, "name": "Own", "password": "ownerpass1"}).status_code == 200
    return tok(c, email, "ownerpass1")


def submitted(c, o, name, woreda, kebele):
    b = c.post(P + "buildings", headers=o, json={"subcity": "Sub", "name": name, "woreda": woreda, "kebele": kebele, "lat": 9.0, "lng": 38.7, "floors": 1}).json()
    assert c.post(P + f"buildings/{b['id']}/files", headers=o, files={"file": ("a.png", b"x" * 10)}, data={"category": "front"}).status_code == 200
    c.post(P + f"buildings/{b['id']}/files", headers=o, files={"file": ("a.png", b"x" * 10)}, data={"category": "owner_fayda"}).status_code
    c.post(P + f"buildings/{b['id']}/files", headers=o, files={"file": ("a.png", b"x" * 10)}, data={"category": "owner_photo"}).status_code
    c.post(P + f"buildings/{b['id']}/files", headers=o, files={"file": ("a.png", b"x" * 10)}, data={"category": "title_deed"}).status_code
    assert c.post(P + f"buildings/{b['id']}/floors", headers=o, json={"label": "G"}).status_code == 200
    r = c.post(P + f"buildings/{b['id']}/submit", headers=o)
    assert r.status_code == 200, r.text
    return b, r.json()


def review(c, h, bid, decision="approve"): return c.post(P + f"buildings/{bid}/review", headers=h, json={"decision": decision})


def test_woreda_office_approves_only_in_its_own_woreda():
    with TestClient(app) as c:
        a = admin(c)
        org = make_org(c, a, name="Bole woreda office", kind="woreda", woreda="Bole", account=acct("bole@x.org", "Bole officer"))
        assert "building.review" in org["powers"] and "registry.read" in org["powers"] and org["members"][0]["email"] == "bole@x.org"
        make_org(c, a, name="Kirkos woreda office", kind="woreda", woreda="Kirkos", account=acct("kirkos@x.org"))
        bo, ko = tok(c, "bole@x.org", PW), tok(c, "kirkos@x.org", PW)
        me = c.get(P + "me", headers=bo).json()
        assert me["role"] == "staff" and me["org"]["name"] == "Bole woreda office" and "building.review" in me["powers"]
        assert "mfa_secret" not in me and "pw" not in me  # secrets never leave the server

        o = owner(c, "owner-o1@x.org")
        meta = c.get(P + "org-meta", headers=o).json()  # owners are offered the woredas that have an office
        assert "Bole" in meta["woredas"] and "building.review" in meta["powers"] and meta["kinds"]["woreda"]["label"] == "Woreda office"
        b = c.post(P + "buildings", headers=o, json={"subcity": "Sub", "name": "Orgs Plaza", "woreda": "Bole", "kebele": "K1", "lat": 9.0, "lng": 38.7, "floors": 1}).json()
        up = lambda cat: c.post(P + f"buildings/{b['id']}/files", headers=o, files={"file": ("a.png", b"x" * 10)}, data={"category": cat}).status_code
        assert up("<img src=x onerror=alert(1)>") == 400  # the category is shown to officers, so only the known ones are accepted
        assert up("front") == 200
        assert up("owner_fayda") == 200
        assert up("owner_photo") == 200
        assert up("title_deed") == 200
        assert c.post(P + f"buildings/{b['id']}/floors", headers=o, json={"label": "G"}).status_code == 200
        sub = c.post(P + f"buildings/{b['id']}/submit", headers=o).json()
        assert sub["reviewers"] == 1  # one account can approve it, and it was told
        assert any("New application waiting" in n["text"] for n in c.get(P + "notifications", headers=bo).json())
        assert not any("New application waiting" in n["text"] for n in c.get(P + "notifications", headers=ko).json())

        assert all(x["id"] != b["id"] for x in c.get(P + "buildings", headers=ko).json())  # other woreda: not even listed
        assert review(c, ko, b["id"]).status_code == 404
        assert "Orgs Plaza" in c.get(P + "reports/buildings.csv", headers=bo).text and "Orgs Plaza" not in c.get(P + "reports/buildings.csv", headers=ko).text
        assert review(c, a, b["id"]).status_code == 403  # the administrator does not approve buildings

        done = review(c, bo, b["id"]).json()
        assert done["issuer"] == "Bole woreda office" and done["brn"].startswith("ET-")
        assert "Bole woreda office" in c.get(f"/verify/{done['tok']}").text  # the public page names the office that approved it
        assert c.get(P + "qr/verify/" + done["tok"]).json()["issued by"] == "Bole woreda office"

        u = c.post(P + f"buildings/{b['id']}/units", headers=o, json={"floor": "G", "number": "G1", "m2": "50"}).json()
        k = c.post(P + "contracts", headers=o, json={"unit_id": u["id"], "tenant_name": "T", "start_date": "2027-01-01", "end_date": "2027-12-31", "rent": 100}).json()
        assert any("New rental contract waiting" in n["text"] for n in c.get(P + "notifications", headers=bo).json())
        assert c.post(P + f"contracts/{k['id']}/approve", headers=ko).status_code == 404
        r = c.post(P + f"contracts/{k['id']}/approve", headers=bo)
        assert r.status_code == 200 and r.json()["issuer"] == "Bole woreda office"
        audit = c.get(P + "admin/audit", headers=a).json()
        assert any("Bole woreda office" in x["actor"] and x["action"] == "building_approve" for x in audit)  # the audit trail names the organization


def test_submitting_where_no_office_exists_is_told_so():
    with TestClient(app) as c:
        o = owner(c, "owner-o1b@x.org")
        b, sub = submitted(c, o, "Nobody Plaza", "Nowhere-W", "K1")
        assert sub["reviewers"] == 0


def test_powers_limit_what_an_office_can_do():
    with TestClient(app) as c:
        a = admin(c)
        make_org(c, a, name="Arada woreda office", kind="woreda", woreda="Arada", account=acct("arada@x.org"))
        make_org(c, a, name="Arada K1 kebele", kind="kebele", woreda="Arada", kebele="K1", account=acct("k1@x.org"))
        make_org(c, a, name="City revenue", kind="revenue", account=acct("rev@x.org"))
        make_org(c, a, name="Trade office", kind="trade", account=acct("trade@x.org"))
        o = owner(c, "owner-o2@x.org")
        b1, _ = submitted(c, o, "Arada K1 Tower", "Arada", "K1")
        b2, _ = submitted(c, o, "Arada K2 Tower", "Arada", "K2")
        k1, rev, trade = tok(c, "k1@x.org", PW), tok(c, "rev@x.org", PW), tok(c, "trade@x.org", PW)

        ids = {x["id"] for x in c.get(P + "buildings", headers=k1).json()}  # a kebele office sees its own kebele only
        assert b1["id"] in ids and b2["id"] not in ids
        assert review(c, k1, b1["id"]).status_code == 403  # it may look and inspect, not approve
        ins = {"building_id": b1["id"], "notes": "checked on site"}
        assert c.post(P + "inspections", headers=k1, json=ins).status_code == 200
        assert c.post(P + "inspections", headers=k1, json={**ins, "building_id": b2["id"]}).status_code == 404

        ids = {x["id"] for x in c.get(P + "buildings", headers=rev).json()}  # the revenue authority reads city-wide but cannot act on records
        assert {b1["id"], b2["id"]} <= ids
        assert review(c, rev, b1["id"]).status_code == 403
        assert c.post(P + "inspections", headers=rev, json=ins).status_code == 403
        assert c.post(P + "tax/rules", headers=rev, json={"version": "ORG-T1", "rate": "0.10", "note": "test"}).status_code == 200
        assert c.get(P + "tax/reports", headers=rev).status_code == 200
        assert c.post(P + "verifications", headers=rev, json={"kind": "etrade", "entity": "e", "reference": "r", "source": "s"}).status_code == 403

        assert c.get(P + "buildings", headers=trade).json() == [] and c.get(P + "contracts", headers=trade).json() == []  # no registry access at all
        assert c.get(P + f"buildings/{b1['id']}/units", headers=trade).status_code == 404
        assert review(c, trade, b1["id"]).status_code == 403
        assert c.post(P + "verifications", headers=trade, json={"kind": "etrade", "entity": "e", "reference": "r", "source": "s"}).status_code == 200
        assert c.post(P + "verifications", headers=trade, json={"kind": "fayda", "entity": "e", "reference": "r", "source": "s"}).status_code == 403
        assert {v["kind"] for v in c.get(P + "verifications", headers=trade).json()} == {"etrade"}  # and sees only its own kind
        assert c.get(P + "tax/reports", headers=trade).status_code == 403 and c.get(P + "admin/audit", headers=trade).status_code == 403

        assert c.get(P + "admin/orgs", headers=o).status_code == 403  # owners and offices cannot manage organizations
        assert c.post(P + "admin/orgs", headers=k1, json={"subcity": "Sub", "name": "Sneaky", "kind": "other"}).status_code == 403


def test_admin_can_change_powers_and_switch_an_organization_off():
    with TestClient(app) as c:
        a = admin(c)
        org = make_org(c, a, name="Lideta woreda office", kind="woreda", woreda="Lideta", account=acct("lid@x.org"))
        oid, uid = org["id"], org["members"][0]["id"]
        o = owner(c, "owner-o3@x.org")
        b, _ = submitted(c, o, "Lideta Plaza", "Lideta", "K1")
        lo = tok(c, "lid@x.org", PW)

        r = c.patch(P + f"admin/orgs/{oid}", headers=a, json={"powers": ["inspection.record"]})  # approval taken away
        assert r.status_code == 200 and sorted(r.json()["powers"]) == ["inspection.record", "registry.read"]  # seeing the record is implied
        assert review(c, lo, b["id"]).status_code == 403
        r = c.patch(P + f"admin/orgs/{oid}", headers=a, json={"powers": ["building.review"]})
        assert "registry.read" in r.json()["powers"] and review(c, lo, b["id"]).status_code == 200
        assert any(x["action"] == "org_updated" for x in c.get(P + "admin/audit", headers=a).json())

        assert c.patch(P + f"admin/orgs/{oid}", headers=a, json={"powers": ["everything"]}).status_code == 400
        assert c.patch(P + f"admin/orgs/{oid}", headers=a, json={"woreda": ""}).status_code == 400  # a woreda office cannot lose its woreda
        assert c.patch(P + "admin/orgs/999999", headers=a, json={"active": False}).status_code == 404

        assert c.patch(P + f"admin/orgs/{oid}", headers=a, json={"active": False}).status_code == 200  # switched off: works at once, even for an open session
        assert c.get(P + "me", headers=lo).status_code == 401
        assert c.post(P + "auth/login", json={"email": "lid@x.org", "password": PW}).status_code == 401
        assert c.patch(P + f"admin/orgs/{oid}", headers=a, json={"active": True}).status_code == 200
        assert c.get(P + "me", headers=lo).status_code == 200

        assert c.post(P + f"admin/users/{uid}/active", headers=a, json={"active": False}).status_code == 200  # one account on its own
        assert c.get(P + "me", headers=lo).status_code == 401
        my_id = c.get(P + "me", headers=a).json()["id"]
        assert c.post(P + f"admin/users/{my_id}/active", headers=a, json={"active": False}).status_code == 400  # no self lock-out


def test_organization_and_account_rules():
    with TestClient(app) as c:
        a = admin(c)
        post = lambda **k: c.post(P + "admin/orgs", headers=a, json=k)
        assert post(name="Dup office", kind="other").status_code == 200
        assert post(name="dup OFFICE", kind="other").status_code == 409
        assert post(name="X1", kind="nonsense").status_code == 400
        assert post(name="Woreda without area", kind="woreda").status_code == 400
        assert post(name="Kebele without woreda", kind="kebele", kebele="K1").status_code == 400
        assert post(name="Bad power", kind="other", powers=["everything"]).status_code == 400
        org = post(name="Has account", kind="other", account=acct("dupmail@x.org"))
        assert org.status_code == 200 and org.json()["powers"] == []  # "other" starts with nothing
        oid = org.json()["id"]
        mem = lambda **k: c.post(P + f"admin/orgs/{oid}/members", headers=a, json={**acct("fresh@x.org"), **k})
        assert mem(email="DupMail@x.org").status_code == 409  # emails are unique whatever the capitals
        assert mem(email="notanemail").status_code == 400
        assert mem(password="short").status_code == 422
        assert c.post(P + "admin/orgs/999999/members", headers=a, json=acct("fresh2@x.org")).status_code == 404
        assert mem().status_code == 200
        assert post(name="Half made", kind="other", account=acct("dupmail@x.org")).status_code == 409  # a refused request leaves nothing behind
        assert "Half made" not in [x["name"] for x in c.get(P + "admin/orgs", headers=a).json()]
        mine = [x for x in c.get(P + "admin/orgs", headers=a).json() if x["id"] == oid][0]
        assert {m["email"] for m in mine["members"]} == {"dupmail@x.org", "fresh@x.org"} and all("pw" not in m for m in mine["members"])


def test_change_password():
    with TestClient(app) as c:
        a = admin(c)
        make_org(c, a, name="Password office", kind="other", account=acct("pw@x.org"))
        h = tok(c, "pw@x.org", PW)
        assert c.post(P + "me/password", headers=h, json={"old": "not-the-password", "new": "brandnewpw1"}).status_code == 401
        assert c.post(P + "me/password", headers=h, json={"old": PW, "new": "short"}).status_code == 422
        assert c.post(P + "me/password", headers=h, json={"old": PW, "new": "brandnewpw1"}).status_code == 200
        assert c.post(P + "auth/login", json={"email": "pw@x.org", "password": PW}).status_code == 401
        assert c.post(P + "auth/login", json={"email": "pw@x.org", "password": "brandnewpw1"}).status_code == 200


def test_office_accounts_follow_the_mfa_rule():
    with TestClient(app) as c:
        a = admin(c)
        make_org(c, a, name="Mfa woreda office", kind="woreda", woreda="Yeka", account=acct("mfa@x.org"))
        h = tok(c, "mfa@x.org", PW)
        os.environ["REQUIRE_MFA"] = "1"
        try:
            assert c.get(P + "admin/audit", headers=h).status_code == 403  # needs MFA first
            s = c.post(P + "me/mfa/setup", headers=h).json()["secret"]
            assert c.post(P + "me/mfa/enable", headers=h, json={"secret": s, "code": totp(s)}).status_code == 200
            assert c.get(P + "admin/audit", headers=h).status_code == 200  # a woreda office holds audit.view
            assert c.get(P + "me", headers=h).json()["has_mfa"] is True
        finally:
            os.environ.pop("REQUIRE_MFA", None)


def test_old_accounts_move_into_organizations():
    with TestClient(app) as c:
        a = admin(c)
        mk = lambda e, **k: c.post(P + "admin/users", headers=a, json={"email": e, "name": "Old", "password": PW, **k})
        r = mk("old1@x.org", role="officer", woreda="Gulele")  # the old API still works: the officer lands in a woreda office
        assert r.status_code == 200 and r.json()["role"] == "staff"
        me = c.get(P + "me", headers=tok(c, "old1@x.org", PW)).json()
        assert me["org"]["name"] == "Gulele woreda office" and "building.review" in me["powers"] and "audit.view" in me["powers"]
        assert mk("old2@x.org", role="officer", woreda="gulele").status_code == 200  # same woreda, other capitals: same office
        offices = [x for x in c.get(P + "admin/orgs", headers=a).json() if x["name"] == "Gulele woreda office"]
        assert len(offices) == 1 and len(offices[0]["members"]) == 2
        assert mk("old3@x.org", role="officer").status_code == 400  # an officer needs a woreda
        assert mk("old1@x.org", role="officer", woreda="Gulele").status_code == 409  # no longer a server error
        assert mk("oldtax@x.org", role="tax").status_code == 200  # tax staff land in the tax authority
        h = tok(c, "oldtax@x.org", PW)
        assert c.post(P + "tax/rules", headers=h, json={"version": "ORG-T2", "rate": "0.10", "note": "t"}).status_code == 200
        assert c.post(P + "verifications", headers=h, json={"kind": "fayda", "entity": "e", "reference": "r", "source": "s"}).status_code == 200  # as before

        with SessionLocal() as db:  # a database from v0.1-0.3 still holds accounts with the old roles: they move when the app starts
            db.add(User(email="legacy@x.org", name="Legacy", pw=hpw(PW), role="officer", woreda="Legacy-W"))
            db.add(User(email="legacy-tax@x.org", name="Legacy tax", pw=hpw(PW), role="tax"))
            db.commit()
        startup()
        startup()  # running it again changes nothing
        with SessionLocal() as db:
            u = db.scalars(select(User).where(User.email == "legacy@x.org")).first()
            assert u.role == "staff" and u.org_id
            o = db.get(Organization, u.org_id)
            assert o.name == "Legacy-W woreda office" and o.woreda == "Legacy-W" and "building.review" in o.powers.split(",")
            t = db.scalars(select(User).where(User.email == "legacy-tax@x.org")).first()
            assert t.role == "staff" and "tax.rules" in db.get(Organization, t.org_id).powers.split(",")
        assert c.get(P + "me", headers=tok(c, "legacy@x.org", PW)).json()["org"]["woreda"] == "Legacy-W"


def test_upgrade_adds_the_new_columns_to_an_old_database():
    old = create_engine(f"sqlite:///{d}/old.db")
    with old.begin() as cx:
        for t in ("users", "buildings", "rental_contracts"): cx.execute(text(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)"))
    keep, main.engine = main.engine, old
    try:
        main.ensure_columns(); main.ensure_columns()  # twice: nothing breaks
    finally:
        main.engine = keep
    cols = lambda t: {x["name"] for x in inspect(old).get_columns(t)}
    assert "org_id" in cols("users") and "issuer" in cols("buildings") and "issuer" in cols("rental_contracts")
