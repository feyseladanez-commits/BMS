"""Ethiopia Building and Rental Registry - backend (FastAPI + SQLAlchemy).
SQLite by default; set DATABASE_URL=postgresql+psycopg://... for PostgreSQL."""
import os, time, base64, hmac, struct, html, hashlib, secrets, threading, uuid, mimetypes, datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Optional
import jwt
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request, Depends, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (create_engine, String, Float, Numeric, Date, DateTime, ForeignKey, Text, LargeBinary,
                        UniqueConstraint, select, func, text, inspect as sa_inspect)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session
from .policy import ADMIN_POWERS, LEGACY, plist, pjoin, clean_powers, in_scope, norm

HERE = Path(__file__).parent
DB = os.getenv("DATABASE_URL", "sqlite:///./registry.db")
if DB.startswith(("postgres://", "postgresql://")): DB = "postgresql+psycopg://" + DB.split("://", 1)[1]  # hosts show the plain URL; the driver in requirements.txt is psycopg 3
SECRET = os.getenv("SECRET_KEY") or secrets.token_hex(32)  # set SECRET_KEY in production
UPLOADS = Path(os.getenv("UPLOAD_DIR", "./uploads")); UPLOADS.mkdir(parents=True, exist_ok=True)
engine = create_engine(DB, connect_args={"check_same_thread": False} if DB.startswith("sqlite") else {})
SessionLocal = sessionmaker(engine, expire_on_commit=False)
LOCK = threading.Lock()  # serialises BRN issue and contract conflict check + insert
now = lambda: dt.datetime.now(dt.timezone.utc)

class Base(DeclarativeBase): pass
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    pw: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))  # owner | staff (member of an organization) | admin
    woreda: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # only filled for accounts that date from before v0.4
    active: Mapped[bool] = mapped_column(default=True)
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"), nullable=True)
class Organization(Base):  # a party with its own accounts and its own powers: woreda office, tax authority, trade office, ...
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    woreda: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # area of responsibility; empty = every woreda
    kebele: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # empty = the whole woreda
    powers: Mapped[str] = mapped_column(Text, default="")  # comma-separated keys from policy.POWERS
    active: Mapped[bool] = mapped_column(default=True)
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
class Building(Base):
    __tablename__ = "buildings"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200)); region: Mapped[str] = mapped_column(String(100))
    city: Mapped[str] = mapped_column(String(100)); woreda: Mapped[str] = mapped_column(String(100))
    subcity: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # sub-city (kifle ketema); older buildings have none
    kebele: Mapped[str] = mapped_column(String(100)); lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float); floors: Mapped[int]; btype: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="draft")
    brn: Mapped[Optional[str]] = mapped_column(String(30), unique=True, nullable=True)
    tok: Mapped[str] = mapped_column(String(40), unique=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    approved: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    issuer: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # name of the organization that approved it
class BFile(Base):
    __tablename__ = "building_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    category: Mapped[str] = mapped_column(String(40)); filename: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64)); size: Mapped[int]
    uploader: Mapped[int] = mapped_column(ForeignKey("users.id"))
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id"), nullable=True)  # set: a photo of that shop
    contract_id: Mapped[Optional[int]] = mapped_column(ForeignKey("rental_contracts.id"), nullable=True)  # set: a tenant document
    doc_no: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)  # number printed on the document (licence no., ID no.)
    expires: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    orig_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
class BFileData(Base):  # the file bytes live in the database: on hosts with an ephemeral disk, files on disk vanish at every restart
    __tablename__ = "building_file_data"
    file_id: Mapped[int] = mapped_column(ForeignKey("building_files.id"), primary_key=True, autoincrement=False)
    data: Mapped[bytes] = mapped_column(LargeBinary)
class Floor(Base):  # one floor of a building; shops (units) sit under a floor
    __tablename__ = "floors"
    __table_args__ = (UniqueConstraint("building_id", "label"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    label: Mapped[str] = mapped_column(String(30))  # G, 1, 2, Basement ...
    name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    m2: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)  # total floor area; when set, the shops on it may not add up to more
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
class Unit(Base):
    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("building_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    floor: Mapped[str] = mapped_column(String(30)); number: Mapped[str] = mapped_column(String(30))
    m2: Mapped[Decimal] = mapped_column(Numeric(12, 2)); use: Mapped[str] = mapped_column(String(50))
    floor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("floors.id"), nullable=True)
    shop_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
class Measure(Base):
    __tablename__ = "unit_measurements"
    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    m2: Mapped[Decimal] = mapped_column(Numeric(12, 2)); by_id: Mapped[int]; ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
class Contract(Base):
    __tablename__ = "rental_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[Optional[str]] = mapped_column(String(30), unique=True, nullable=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    tenant_name: Mapped[str] = mapped_column(String(200))
    fayda_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    fayda_status: Mapped[str] = mapped_column(String(30), default="not_verified_no_provider")
    start_date: Mapped[dt.date] = mapped_column(Date); end_date: Mapped[dt.date] = mapped_column(Date)
    orig_end: Mapped[Optional[dt.date]] = mapped_column(Date, nullable=True)
    rent: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id")); tok: Mapped[str] = mapped_column(String(40), unique=True)
    issuer: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # name of the organization that approved it
    tenant_phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    tenant_tin: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    trade_license_no: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    business_activity: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
class Audit(Base):  # append-only: the API has no update or delete for this table
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    actor: Mapped[str] = mapped_column(String(200)); action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(60)); detail: Mapped[str] = mapped_column(Text, default="")

class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True); user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text); ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
def notify(db, uid, msg): db.add(Notification(user_id=uid, text=msg))
_F = Fernet(base64.urlsafe_b64encode(hashlib.sha256((os.getenv("MFA_KEY") or SECRET).encode()).digest()))
def seal(s): return _F.encrypt(s.encode()).decode()  # MFA secrets are encrypted at rest; keep SECRET_KEY or MFA_KEY stable
def unseal(s): return _F.decrypt(s.encode()).decode()
def totp(secret, t=None):
    h = hmac.new(base64.b32decode(secret), struct.pack(">Q", int((t or time.time()) // 30)), "sha1").digest(); o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7fffffff) % 10**6:06d}"
def totp_ok(secret, code): return any(secrets.compare_digest(totp(secret, time.time() + d * 30), code) for d in (-1, 0, 1))
FAILS = {}  # (ip, email) -> failure times; use Redis when running several workers
def hpw(p, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", p.encode(), salt.encode(), 200_000).hex()
def get_db():
    with SessionLocal() as db: yield db
def org_of(db, u):  # the organization a staff account belongs to
    return db.get(Organization, u.org_id) if u and u.role == "staff" and u.org_id else None
def usable(db, u):  # an account works while it is active and, for staff, while its organization is active too
    if not u or not u.active: return False
    if u.role == "staff":
        o = org_of(db, u); return bool(o and o.active)
    return True
def log(db, u, action, entity, detail=""):
    o = org_of(db, u)
    db.add(Audit(actor=(f"{u.email} ({o.name if o else u.role})" if u else "system")[:200], action=action, entity=entity, detail=detail))
def my_powers(db, u):  # the administrator keeps the old admin powers; staff get exactly their organization's powers
    if u.role == "admin": return set(ADMIN_POWERS)
    o = org_of(db, u)
    return set(plist(o.powers)) if o and o.active else set()
def me(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> User:
    try: uid = jwt.decode((authorization or "").removeprefix("Bearer "), SECRET, algorithms=["HS256"])["sub"]
    except Exception: raise HTTPException(401, "Sign in required")
    u = db.get(User, int(uid))
    if not usable(db, u): raise HTTPException(401, "Account unavailable")
    return u
def mfa_gate(u):
    if os.getenv("REQUIRE_MFA") == "1" and u.role != "owner" and not u.mfa_secret: raise HTTPException(403, "Set up MFA before using this role")
def need(u, *roles):
    mfa_gate(u)
    if u.role not in roles: raise HTTPException(403, "Not allowed for your role")
def allow(db, u, *powers):  # the account must hold at least one of these powers
    mfa_gate(u)
    if not set(powers) & my_powers(db, u): raise HTTPException(403, "Your account does not have this power")
def row(o): return {c.name: getattr(o, c.name) for c in o.__table__.columns if c.name not in ("pw", "mfa_secret")}
def me_out(db, u):
    o = org_of(db, u)
    return {**row(u), "has_mfa": bool(u.mfa_secret), "powers": sorted(my_powers(db, u)),
            "org": {"id": o.id, "name": o.name, "kind": o.kind, "woreda": o.woreda, "kebele": o.kebele} if o else None}

def vis(db, u):  # which buildings this account may read: owners their own, the administrator all, staff what their organization may see in its area
    if u.role == "owner": return lambda b: b.owner_id == u.id
    if u.role == "admin": return lambda b: True
    o = org_of(db, u); ok = bool(o and o.active and "registry.read" in plist(o.powers))
    return lambda b: ok and b.status != "draft" and in_scope(o.woreda, o.kebele, b.woreda, b.kebele)
def get_b(db, u, bid, write=False) -> Building:
    b = db.get(Building, bid)
    if not b or not vis(db, u)(b) or (write and u.role == "admin"): raise HTTPException(404, "Not found")
    return b
def holders(db, power, b):  # active staff accounts whose organization holds this power for this building (who to notify)
    ids = []
    for o in db.scalars(select(Organization).where(Organization.active)):
        if power in plist(o.powers) and in_scope(o.woreda, o.kebele, b.woreda, b.kebele):
            ids += db.scalars(select(User.id).where(User.org_id == o.id, User.role == "staff", User.active)).all()
    return ids
def legacy_org(db, role, woreda=None):  # officers and tax staff from before v0.4 join an organization that has the powers they had
    w = (woreda or "").strip()
    name, kind, wd = (f"{w} woreda office", "woreda", w) if role == "officer" else ("Tax authority", "revenue", None)
    o = db.scalars(select(Organization).where(func.lower(Organization.name) == name.lower())).first()
    if not o:
        o = Organization(name=name, kind=kind, woreda=wd, powers=pjoin(clean_powers(LEGACY[role]))); db.add(o); db.flush()
    return o
def ensure_columns():  # small upgrade step: add the columns v0.4 needs to tables that an older version created
    for t, col, typ in (("users", "org_id", "INTEGER"), ("buildings", "issuer", "VARCHAR(200)"), ("rental_contracts", "issuer", "VARCHAR(200)"),
                        ("buildings", "subcity", "VARCHAR(100)"),
                        ("building_files", "unit_id", "INTEGER"), ("building_files", "contract_id", "INTEGER"), ("building_files", "doc_no", "VARCHAR(60)"),
                        ("building_files", "expires", "DATE"), ("building_files", "orig_name", "VARCHAR(200)"),
                        ("units", "floor_id", "INTEGER"), ("units", "shop_name", "VARCHAR(200)"), ("units", "notes", "TEXT"),
                        ("rental_contracts", "tenant_phone", "VARCHAR(30)"), ("rental_contracts", "tenant_tin", "VARCHAR(30)"),
                        ("rental_contracts", "trade_license_no", "VARCHAR(60)"), ("rental_contracts", "business_activity", "VARCHAR(200)")):
        insp = sa_inspect(engine)
        if t in insp.get_table_names() and col not in {x["name"] for x in insp.get_columns(t)}:
            with engine.begin() as c: c.execute(text(f"ALTER TABLE {t} ADD COLUMN {col} {typ}"))
def migrate_legacy():  # accounts with the old roles "officer" and "tax" become staff of an organization (runs on every start, does nothing once done)
    with SessionLocal() as db:
        for u in db.scalars(select(User).where(User.role.in_(("officer", "tax")))).all():
            if u.role == "officer" and not (u.woreda or "").strip(): continue  # an officer without a woreda cannot be placed safely
            o = legacy_org(db, u.role, u.woreda); log(db, None, "account_moved_to_organization", f"user:{u.id}", f"{u.role} -> org:{o.id}")
            u.role, u.org_id = "staff", o.id
        db.commit()
def link_floors():  # units from before v0.5 only have a floor text: give each one a floor record (runs on every start, does nothing once done)
    with SessionLocal() as db:
        for un in db.scalars(select(Unit).where(Unit.floor_id.is_(None))).all():
            label = (un.floor or "").strip() or "?"
            fl = db.scalars(select(Floor).where(Floor.building_id == un.building_id, Floor.label == label)).first()
            if not fl: fl = Floor(building_id=un.building_id, label=label); db.add(fl); db.flush()
            un.floor_id = fl.id
        db.commit()
def floor_for(db, b, floor_id, label, create=True):  # the floor a new shop goes on; a floor given only by its label is created on the way
    if floor_id:
        fl = db.get(Floor, floor_id)
        if not fl or fl.building_id != b.id: raise HTTPException(404, "Floor not found")
        return fl
    label = (label or "").strip()
    if not label: raise HTTPException(400, "Choose the floor this shop is on")
    fl = db.scalars(select(Floor).where(Floor.building_id == b.id, Floor.label == label)).first()
    if fl or not create: return fl
    n = db.scalar(select(func.count()).select_from(Floor).where(Floor.building_id == b.id))
    if n >= b.floors: raise HTTPException(400, f"This building has {b.floors} floor(s) and all are already described")
    fl = Floor(building_id=b.id, label=label); db.add(fl); db.flush(); return fl
def floor_room(db, fl, more, ignore_unit=None):  # the shops on a floor may not add up to more than the floor area (when the floor area is known)
    if fl.m2 is None: return
    used = Decimal(str(db.scalar(select(func.coalesce(func.sum(Unit.m2), 0)).where(Unit.floor_id == fl.id, Unit.id != (ignore_unit or 0)))))
    if used + more > fl.m2: raise HTTPException(400, f"Shops on floor {fl.label} would total {used + more} m², more than the floor area of {fl.m2} m²")
def clash(db, unit_id, s, e, ignore=None):
    q = select(Contract).where(Contract.unit_id == unit_id, Contract.status.in_(("submitted", "active", "terminated", "disputed")),
                               Contract.start_date <= e, Contract.end_date >= s)  # new_start<=old_end and old_start<=new_end
    if ignore: q = q.where(Contract.id != ignore)
    return db.scalars(q).first()
def cdict(db, c):
    un = db.get(Unit, c.unit_id); b = db.get(Building, un.building_id)
    return {**row(c), "unit": un.number, "m2": un.m2, "building": b.name, "brn": b.brn, "woreda": b.woreda}

class Reg(BaseModel): email: str; name: str; password: str = Field(min_length=8)
class Login(BaseModel): email: str; password: str; code: Optional[str] = None
class NewB(BaseModel):
    name: str; subcity: str = Field(min_length=1, max_length=100); woreda: str; kebele: str; lat: float = Field(ge=3, le=15); lng: float = Field(ge=33, le=48)
    floors: int = Field(ge=1); region: str = "Addis Ababa"; city: str = "Addis Ababa"; btype: str = "commercial"
class NewU(BaseModel):
    floor_id: Optional[int] = None; floor: Optional[str] = None  # give the floor record, or just its label
    number: str = Field(min_length=1, max_length=30); m2: Decimal = Field(gt=0, le=100000); use: str = "shop"
    shop_name: Optional[str] = Field(None, max_length=200); notes: Optional[str] = Field(None, max_length=1000)
class Area(BaseModel): m2: Decimal = Field(gt=0, le=100000)
class NewC(BaseModel):
    unit_id: int; tenant_name: str; fayda: Optional[str] = None
    tenant_phone: Optional[str] = Field(None, max_length=30); tenant_tin: Optional[str] = Field(None, max_length=30)
    trade_license_no: Optional[str] = Field(None, max_length=60); business_activity: Optional[str] = Field(None, max_length=200)
    start_date: dt.date; end_date: dt.date; rent: Decimal = Field(gt=0)
class Review(BaseModel): decision: str; reason: Optional[str] = None
class NewUser(BaseModel): email: str; name: str; password: str = Field(min_length=8); role: str; woreda: Optional[str] = None
class Term(BaseModel): effective: dt.date

app = FastAPI(title="Ethiopia Building and Rental Registry", version="0.5")
@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine); ensure_columns(); migrate_legacy(); link_floors()
    with SessionLocal() as db:  # first admin comes from environment, never hardcoded
        e, p = os.getenv("ADMIN_EMAIL"), os.getenv("ADMIN_PASSWORD")
        if e and p and not db.scalars(select(User).where(User.role == "admin")).first():
            db.add(User(email=e.lower(), name="Administrator", pw=hpw(p), role="admin")); db.commit()
    if engine.dialect.name == "postgresql":  # database-level double-rent guard
        try:
            with engine.begin() as c:
                c.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
                c.execute(text("ALTER TABLE rental_contracts ADD CONSTRAINT no_double_rent EXCLUDE USING gist "
                    "(unit_id WITH =, daterange(start_date, end_date, '[]') WITH &&) "
                    "WHERE (status IN ('submitted','active','terminated','disputed'))"))
        except Exception: pass  # already present

A = "/api/v1"
@app.post(A + "/auth/register")
def register(d: Reg, db: Session = Depends(get_db)):
    if db.scalars(select(User).where(User.email == d.email.lower())).first(): raise HTTPException(409, "Email already registered")
    u = User(email=d.email.lower(), name=d.name, pw=hpw(d.password), role="owner"); db.add(u); db.flush()
    log(db, u, "register", f"user:{u.id}"); db.commit(); return {"ok": True}
@app.post(A + "/auth/login")
def login(d: Login, request: Request, db: Session = Depends(get_db)):
    key = (request.client.host if request.client else "?", d.email.lower()); t = time.time()
    FAILS[key] = [x for x in FAILS.get(key, []) if t - x < 600]
    if len(FAILS[key]) >= 5: raise HTTPException(429, "Too many attempts. Try again in 10 minutes")
    u = db.scalars(select(User).where(User.email == d.email.lower())).first()
    if not usable(db, u) or not secrets.compare_digest(hpw(d.password, u.pw.split("$")[0]), u.pw):
        FAILS[key].append(t); raise HTTPException(401, "Wrong email or password")
    if u.mfa_secret and not totp_ok(unseal(u.mfa_secret), (d.code or "").strip()):
        FAILS[key].append(t); raise HTTPException(401, "Enter the 6-digit code from your authenticator app")
    tk = jwt.encode({"sub": str(u.id), "exp": now() + dt.timedelta(hours=8)}, SECRET, algorithm="HS256")
    return {"token": tk, "user": me_out(db, u)}
@app.get(A + "/me")
def whoami(u: User = Depends(me), db: Session = Depends(get_db)): return me_out(db, u)
@app.post(A + "/admin/users")
def mk_user(d: NewUser, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "admin")  # accounts of offices are made inside their organization (see orgs.py); "officer" and "tax" remain as old names
    if d.role not in ("officer", "admin", "owner", "tax"): raise HTTPException(400, "Unknown role")
    if d.role == "officer" and not d.woreda: raise HTTPException(400, "Officers need a woreda")
    if db.scalars(select(User).where(User.email == d.email.lower())).first(): raise HTTPException(409, "Email already registered")
    role, oid, w = d.role, None, d.woreda
    if d.role in ("officer", "tax"):  # the account joins an organization that has the powers this old role had
        role, oid, w = "staff", legacy_org(db, d.role, d.woreda).id, None
    n = User(email=d.email.lower(), name=d.name, pw=hpw(d.password), role=role, woreda=w, org_id=oid); db.add(n); db.flush()
    log(db, u, "create_user", f"user:{n.id}", d.role); db.commit(); return row(n)
@app.get(A + "/admin/audit")
def audit(u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, "audit.view"); return [row(a) for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))]

@app.post(A + "/buildings")
def new_b(d: NewB, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); d.subcity = d.subcity.strip()
    if not d.subcity: raise HTTPException(400, "Enter the sub-city")
    if db.scalars(select(Building).where(func.lower(Building.name) == d.name.lower(), Building.kebele == d.kebele, Building.woreda == d.woreda)).first():
        raise HTTPException(409, "Possible duplicate: a building with this name already exists in this kebele")
    b = Building(owner_id=u.id, tok=secrets.token_hex(12), **d.model_dump()); db.add(b); db.flush()
    log(db, u, "building_created", f"building:{b.id}"); db.commit(); return row(b)
@app.get(A + "/buildings")
def list_b(u: User = Depends(me), db: Session = Depends(get_db)):
    q = select(Building).order_by(Building.id.desc()); see = vis(db, u)
    if u.role == "owner": q = q.where(Building.owner_id == u.id)
    elif u.role == "staff":  # narrow it in the database first; vis() stays the final check
        o = org_of(db, u); q = q.where(Building.status != "draft")
        if o and o.woreda: q = q.where(func.lower(func.trim(Building.woreda)) == norm(o.woreda))
        if o and o.kebele: q = q.where(func.lower(func.trim(Building.kebele)) == norm(o.kebele))
    out = []
    for b in db.scalars(q):
        if not see(b): continue
        fs = db.scalars(select(BFile).where(BFile.building_id == b.id, BFile.unit_id.is_(None), BFile.contract_id.is_(None))).all()  # shop photos and tenant documents are listed with their shop and tenant
        nf = db.scalar(select(func.count()).select_from(Floor).where(Floor.building_id == b.id))
        tot = db.scalar(select(func.coalesce(func.sum(Unit.m2), 0)).where(Unit.building_id == b.id))
        out.append({**row(b), "files": [row(f) for f in fs], "floors_entered": nf, "total_m2": tot, "tok": b.tok if b.status == "approved" else None})
    return out
@app.post(A + "/buildings/{bid}/files")
async def upload(bid: int, category: str = Form("front"), file: UploadFile = File(...), u: User = Depends(me), db: Session = Depends(get_db)):
    b = get_b(db, u, bid, write=True); need(u, "owner")
    if category not in ("front", "side", "entrance", "ownership"): raise HTTPException(400, "Unknown file category")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".pdf"): raise HTTPException(400, "Only JPG, PNG or PDF files are allowed")
    data = await file.read()
    if len(data) > 8_000_000: raise HTTPException(400, "File is larger than 8 MB")
    name = uuid.uuid4().hex + ext  # only the name and type are kept here; the bytes go to BFileData
    f = BFile(building_id=b.id, category=category, filename=name, sha256=hashlib.sha256(data).hexdigest(), size=len(data), uploader=u.id, orig_name=(file.filename or "")[:200] or None)
    db.add(f); db.flush(); db.add(BFileData(file_id=f.id, data=data)); log(db, u, "file_uploaded", f"building:{b.id}", category); db.commit(); return row(f)
@app.get(A + "/files/{fid}")
def get_file(fid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    f = db.get(BFile, fid)
    if not f: raise HTTPException(404, "Not found")
    get_b(db, u, f.building_id)
    if f.contract_id:  # tenant documents (ID copies, licences, leases): the building's owner and offices that hold tenant.docs; every look by an office is logged
        if u.role != "owner" and "tenant.docs" not in my_powers(db, u): raise HTTPException(404, "Not found")
        if u.role != "owner": log(db, u, "tenant_document_viewed", f"contract:{f.contract_id}", f"file:{f.id} {f.category}"); db.commit()
    blob = db.get(BFileData, f.id)
    if blob: return Response(blob.data, media_type=mimetypes.guess_type(f.filename)[0] or "application/octet-stream")
    return FileResponse(UPLOADS / f.filename)  # a file that an older version saved on disk
@app.post(A + "/buildings/{bid}/submit")
def submit(bid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); b = get_b(db, u, bid, write=True)
    if b.status not in ("draft", "correction_requested"): raise HTTPException(400, "Already submitted")
    if not db.scalars(select(BFile).where(BFile.building_id == b.id, BFile.category == "front")).first():
        raise HTTPException(400, "Upload a front photo of the building first")
    nf = db.scalar(select(func.count()).select_from(Floor).where(Floor.building_id == b.id))
    if nf < b.floors: raise HTTPException(400, f"Describe every floor first: {nf} of {b.floors} floors entered")
    b.status = "submitted"; ids = holders(db, "building.review", b)
    for i in ids: notify(db, i, f"New application waiting: {b.name}")
    log(db, u, "building_submitted", f"building:{b.id}", f"{len(ids)} reviewer account(s) notified"); db.commit()
    return {**row(b), "reviewers": len(ids)}
@app.post(A + "/buildings/{bid}/review")
def review(bid: int, d: Review, u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, "building.review"); b = get_b(db, u, bid, write=True)
    if b.status != "submitted": raise HTTPException(400, "Only submitted applications can be reviewed")
    if d.decision not in ("approve", "reject", "correction"): raise HTTPException(400, "Unknown decision")
    if d.decision != "approve" and not d.reason: raise HTTPException(400, "A reason is required")
    with LOCK:
        if d.decision == "approve":
            n = db.scalar(select(func.count()).select_from(Building).where(Building.brn.is_not(None))) + 1
            o = org_of(db, u)
            b.brn = f"ET-{n:07d}"; b.status = "approved"; b.approved = now(); b.reason = None; b.issuer = o.name if o else None
        else:
            b.status = "rejected" if d.decision == "reject" else "correction_requested"; b.reason = d.reason
        notify(db, b.owner_id, f"Your application for {b.name} is {b.status.replace('_', ' ')}" + (f". BRN {b.brn}" if b.brn else f": {b.reason}")); log(db, u, f"building_{d.decision}", f"building:{b.id}", d.reason or b.brn or ""); db.commit()
    return row(b)

@app.post(A + "/buildings/{bid}/units")
def add_unit(bid: int, d: NewU, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); b = get_b(db, u, bid, write=True)
    if b.status not in ("draft", "correction_requested", "approved"): raise HTTPException(400, "Shops cannot be changed while the application is under review or after it was rejected")
    d.number = d.number.strip()
    if not d.number: raise HTTPException(400, "Enter the shop number")
    if db.scalars(select(Unit).where(Unit.building_id == b.id, Unit.number == d.number)).first(): raise HTTPException(409, "Unit number already exists in this building")
    fl = floor_for(db, b, d.floor_id, d.floor); floor_room(db, fl, d.m2)
    n = Unit(building_id=b.id, floor=fl.label, floor_id=fl.id, number=d.number, m2=d.m2, use=d.use, shop_name=(d.shop_name or "").strip() or None, notes=(d.notes or "").strip() or None)
    db.add(n); db.flush(); db.add(Measure(unit_id=n.id, m2=d.m2, by_id=u.id))
    log(db, u, "unit_created", f"unit:{n.id}", str(d.m2)); db.commit(); return row(n)
@app.get(A + "/buildings/{bid}/units")
def units(bid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    b = get_b(db, u, bid); return [row(x) for x in db.scalars(select(Unit).where(Unit.building_id == b.id))]
@app.patch(A + "/units/{uid}")
def set_area(uid: int, d: Area, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); un = db.get(Unit, uid)
    if not un: raise HTTPException(404, "Not found")
    get_b(db, u, un.building_id, write=True)
    if un.floor_id: floor_room(db, db.get(Floor, un.floor_id), d.m2, ignore_unit=un.id)
    un.m2 = d.m2; db.add(Measure(unit_id=un.id, m2=d.m2, by_id=u.id))
    log(db, u, "area_updated", f"unit:{un.id}", str(d.m2)); db.commit(); return row(un)

@app.post(A + "/contracts")
def new_c(d: NewC, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); un = db.get(Unit, d.unit_id)
    if not un: raise HTTPException(404, "Unit not found")
    b = get_b(db, u, un.building_id, write=True)
    if b.status != "approved": raise HTTPException(400, "Rental contracts can be added after the building is approved")
    if d.end_date < d.start_date: raise HTTPException(400, "End date must not be before the start date")
    f = (d.fayda or "").replace(" ", "")
    if f and not (f.isdigit() and 6 <= len(f) <= 20): raise HTTPException(400, "Fayda number must be digits only")
    with LOCK:
        x = clash(db, un.id, d.start_date, d.end_date)
        if x: raise HTTPException(409, f"Unit is already rented {x.start_date} to {x.end_date} ({x.ref}). Overlapping contracts are not allowed")
        c = Contract(unit_id=un.id, tenant_name=d.tenant_name.strip(), fayda_last4=f[-4:] or None,
                     tenant_phone=(d.tenant_phone or "").strip() or None, tenant_tin=(d.tenant_tin or "").strip() or None,
                     trade_license_no=(d.trade_license_no or "").strip() or None, business_activity=(d.business_activity or "").strip() or None, start_date=d.start_date, end_date=d.end_date,
                     rent=d.rent, created_by=u.id, tok=secrets.token_hex(12)); db.add(c); db.flush()
        c.ref = f"RC-{now().year}-{c.id:05d}"; log(db, u, "contract_submitted", f"contract:{c.id}")
        for i in holders(db, "contract.approve", b): notify(db, i, f"New rental contract waiting: {c.ref}")
        db.commit()
    return cdict(db, c)
@app.get(A + "/contracts")
def list_c(u: User = Depends(me), db: Session = Depends(get_db)):
    out = []; see = vis(db, u)
    for c in db.scalars(select(Contract).order_by(Contract.id.desc())):
        un = db.get(Unit, c.unit_id); b = db.get(Building, un.building_id)
        if not see(b): continue
        out.append(cdict(db, c))
    return out
def get_c(db, u, cid):
    c = db.get(Contract, cid)
    if not c: raise HTTPException(404, "Not found")
    get_b(db, u, db.get(Unit, c.unit_id).building_id, write=True); return c
@app.post(A + "/contracts/{cid}/approve")
def approve_c(cid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, "contract.approve"); c = get_c(db, u, cid)
    if c.status != "submitted": raise HTTPException(400, "Only submitted contracts can be approved")
    with LOCK:
        if clash(db, c.unit_id, c.start_date, c.end_date, ignore=c.id): raise HTTPException(409, "Overlaps another contract")
        o = org_of(db, u); c.status = "active"; c.issuer = o.name if o else None
        notify(db, c.created_by, f"Contract {c.ref} approved"); log(db, u, "contract_approved", f"contract:{c.id}"); db.commit()
    return cdict(db, c)
@app.post(A + "/contracts/{cid}/terminate")
def terminate(cid: int, d: Term, u: User = Depends(me), db: Session = Depends(get_db)):
    if u.role != "owner": allow(db, u, "contract.terminate")
    c = get_c(db, u, cid)
    if c.status not in ("active", "submitted") or not (c.start_date <= d.effective <= c.end_date): raise HTTPException(400, "Choose a date inside the contract period")
    c.orig_end, c.end_date, c.status = c.end_date, d.effective, "terminated"  # history kept; unit frees after this date
    log(db, u, "contract_terminated", f"contract:{c.id}", str(d.effective)); db.commit(); return cdict(db, c)

def verify_data(db, tok):
    b = db.scalars(select(Building).where(Building.tok == tok, Building.status == "approved")).first()
    if b: return {"type": "Building", "reference": b.brn, "status": b.status, "woreda": b.woreda, "date": str(b.approved.date()),
                         **({"issued by": b.issuer} if b.issuer else {})}
    c = db.scalars(select(Contract).where(Contract.tok == tok, Contract.status.in_(("active", "terminated")))).first()
    if c:
        un = db.get(Unit, c.unit_id)
        return {"type": "Rental contract", "reference": c.ref, "status": c.status, "building": db.get(Building, un.building_id).brn,
                "unit": un.number, "period": f"{c.start_date} to {c.end_date}", **({"issued by": c.issuer} if c.issuer else {})}
@app.get(A + "/qr/verify/{tok}")
def qr_json(tok: str, db: Session = Depends(get_db)):
    r = verify_data(db, tok)
    if not r: raise HTTPException(404, "No matching record")
    return r
@app.get("/verify/{tok}", response_class=HTMLResponse)
def qr_page(tok: str, db: Session = Depends(get_db)):
    r = verify_data(db, tok); col = "#0f6e46" if r else "#b3261e"
    body = "".join(f"<p><b>{html.escape(k)}</b>: {html.escape(str(v))}</p>" for k, v in r.items()) if r else "<p>No matching record.</p>"
    return f"<meta name=viewport content='width=device-width,initial-scale=1'><body style='font:16px system-ui;max-width:520px;margin:24px auto;padding:0 14px'><h2 style='color:{col}'>{'Record verified' if r else 'Not found'}</h2>{body}<p style='color:#666;font-size:13px'>Public view. Names, ID numbers and rent are not shown.</p>"

from . import extra, orgs, structure  # noqa: E402  (register the extra tables and routes)
app.include_router(extra.r); app.include_router(orgs.r); app.include_router(structure.r)
app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")
