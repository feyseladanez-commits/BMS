"""Organizations and their accounts. Each party (woreda office, tax authority, trade office, ...) gets an organization
with its own accounts; the administrator decides which powers the organization has (see policy.py)."""
import time, secrets
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .main import A, FAILS, User, Organization, me, need, get_db, log, row, hpw
from .policy import KINDS, meta, plist, pjoin, clean_powers, scope_error, norm
r = APIRouter(prefix=A)

class Member(BaseModel): email: str; name: str = Field(min_length=1); password: str = Field(min_length=8)
class NewOrg(BaseModel):
    name: str = Field(min_length=2, max_length=200); kind: str
    woreda: Optional[str] = None; kebele: Optional[str] = None
    powers: Optional[list[str]] = None  # leave out to start from the suggestion for this type
    account: Optional[Member] = None  # the first account of the organization, optional
class EditOrg(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=200); woreda: Optional[str] = None; kebele: Optional[str] = None
    powers: Optional[list[str]] = None; active: Optional[bool] = None
class Active(BaseModel): active: bool
class NewPw(BaseModel): old: str; new: str = Field(min_length=8)

def org_out(db, o):
    ms = db.scalars(select(User).where(User.org_id == o.id).order_by(User.id)).all()
    return {**row(o), "powers": plist(o.powers),
            "members": [{"id": m.id, "name": m.name, "email": m.email, "active": m.active, "mfa": bool(m.mfa_secret)} for m in ms]}
def member_email(db, d):  # checked before anything is written
    e = d.email.strip().lower()
    if "@" not in e: raise HTTPException(400, "Enter a valid email")
    if db.scalars(select(User).where(User.email == e)).first(): raise HTTPException(409, "Email already registered")
    return e
def add_member(db, actor, o, d, e):
    n = User(email=e, name=d.name.strip(), pw=hpw(d.password), role="staff", org_id=o.id); db.add(n); db.flush()
    log(db, actor, "create_user", f"user:{n.id}", f"staff of org:{o.id}"); return n
def area_text(w, k): return f"{w or 'all'}/{k or 'all'}"

@r.get("/org-meta")
def org_meta(u: User = Depends(me), db: Session = Depends(get_db)):  # powers, organization types and the woredas that have an office (owners pick from these)
    ws = {}
    for o in db.scalars(select(Organization).where(Organization.active)):
        if o.woreda: ws.setdefault(norm(o.woreda), o.woreda)
    return {**meta(), "woredas": sorted(ws.values())}

@r.get("/admin/orgs")
def list_orgs(u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "admin"); return [org_out(db, o) for o in db.scalars(select(Organization).order_by(Organization.name))]

@r.post("/admin/orgs")
def new_org(d: NewOrg, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "admin")
    if d.kind not in KINDS: raise HTTPException(400, "Unknown organization type")
    w, k = (d.woreda or "").strip() or None, (d.kebele or "").strip() or None
    err = scope_error(d.kind, w, k)
    if err: raise HTTPException(400, err)
    name = d.name.strip()
    if len(name) < 2: raise HTTPException(400, "Enter the organization name")
    if db.scalars(select(Organization).where(func.lower(Organization.name) == name.lower())).first(): raise HTTPException(409, "An organization with this name already exists")
    try: ps = clean_powers(KINDS[d.kind][1] if d.powers is None else d.powers)
    except ValueError as e: raise HTTPException(400, str(e))
    first = member_email(db, d.account) if d.account else None
    o = Organization(name=name, kind=d.kind, woreda=w, kebele=k, powers=pjoin(ps)); db.add(o); db.flush()
    log(db, u, "org_created", f"org:{o.id}", f"{d.kind}; area {area_text(w, k)}; powers {o.powers}")
    if d.account: add_member(db, u, o, d.account, first)
    db.commit(); return org_out(db, o)

@r.patch("/admin/orgs/{oid}")
def edit_org(oid: int, d: EditOrg, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "admin"); o = db.get(Organization, oid)
    if not o: raise HTTPException(404, "Organization not found")
    before = f"area {area_text(o.woreda, o.kebele)}; active {o.active}; powers {o.powers}"
    if d.name is not None:
        nm = d.name.strip()
        if len(nm) < 2: raise HTTPException(400, "Enter the organization name")
        if db.scalars(select(Organization).where(func.lower(Organization.name) == nm.lower(), Organization.id != o.id)).first():
            raise HTTPException(409, "An organization with this name already exists")
        o.name = nm
    if d.woreda is not None: o.woreda = d.woreda.strip() or None
    if d.kebele is not None: o.kebele = d.kebele.strip() or None
    err = scope_error(o.kind, o.woreda, o.kebele)
    if err: raise HTTPException(400, err)
    if d.powers is not None:
        try: o.powers = pjoin(clean_powers(d.powers))
        except ValueError as e: raise HTTPException(400, str(e))
    if d.active is not None: o.active = d.active
    log(db, u, "org_updated", f"org:{o.id}", f"{before} -> area {area_text(o.woreda, o.kebele)}; active {o.active}; powers {o.powers}")
    db.commit(); return org_out(db, o)

@r.post("/admin/orgs/{oid}/members")
def new_member(oid: int, d: Member, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "admin"); o = db.get(Organization, oid)
    if not o: raise HTTPException(404, "Organization not found")
    n = add_member(db, u, o, d, member_email(db, d)); db.commit(); return row(n)

@r.post("/admin/users/{uid}/active")
def set_user_active(uid: int, d: Active, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "admin"); t = db.get(User, uid)
    if not t: raise HTTPException(404, "Not found")
    if t.id == u.id and not d.active: raise HTTPException(400, "You cannot deactivate your own account")
    t.active = d.active; log(db, u, "user_active", f"user:{t.id}", str(d.active)); db.commit(); return row(t)

@r.post("/me/password")
def change_password(d: NewPw, u: User = Depends(me), db: Session = Depends(get_db)):
    key = ("pw", u.email); t = time.time(); FAILS[key] = [x for x in FAILS.get(key, []) if t - x < 600]
    if len(FAILS[key]) >= 5: raise HTTPException(429, "Too many attempts. Try again in 10 minutes")
    if not secrets.compare_digest(hpw(d.old, u.pw.split("$")[0]), u.pw):
        FAILS[key].append(t); raise HTTPException(401, "Current password is wrong")
    u.pw = hpw(d.new); db.add(u); log(db, u, "password_changed", f"user:{u.id}"); db.commit(); return {"ok": True}
