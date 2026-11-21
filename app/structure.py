"""Building structure: floors, the shops (units) on each floor with their photos, and the documents of each tenant.
Shop photos and tenant documents are stored like building files (table building_files, bytes in building_file_data).
Tenant documents are sensitive: only the building's owner and offices holding the tenant.docs power can open them."""
import hashlib, uuid, datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
import re
from sqlalchemy import select, func, String, Numeric, Date, DateTime, ForeignKey
from sqlalchemy.orm import Session, Mapped, mapped_column
from .main import (Base, now, A, User, Building, BFile, BFileData, Floor, Unit, Contract, me, need, get_b, get_db, log, row, my_powers, floor_room)
r = APIRouter(prefix=A)

EDITABLE = ("draft", "correction_requested", "approved")  # building states in which the structure may change
PHOTO_EXT = (".jpg", ".jpeg", ".png")
DOC_EXT = (".jpg", ".jpeg", ".png", ".pdf")
DOC_KINDS = ("tenant_id", "trade_license", "tin_certificate", "lease_agreement", "authorization", "other")
MAX_BYTES, MAX_PHOTOS, MAX_DOCS = 8_000_000, 10, 15

class RentPayment(Base):  # rent paid by a tenant for one month, entered by the building owner together with the receipt
    __tablename__ = "rent_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("rental_contracts.id"))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM the rent is for
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    paid_on: Mapped[dt.date] = mapped_column(Date)
    method: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    reference: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)  # receipt or transaction number
    file_id: Mapped[int] = mapped_column(ForeignKey("building_files.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id")); ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
METHODS = ("telebirr", "cbe_birr", "bank_transfer", "cash", "other")

class NewFloor(BaseModel):
    label: str = Field(min_length=1, max_length=30); name: Optional[str] = Field(None, max_length=100)
    m2: Optional[Decimal] = Field(None, gt=0, le=1000000); notes: Optional[str] = Field(None, max_length=1000)
class EditFloor(BaseModel):
    name: Optional[str] = Field(None, max_length=100); m2: Optional[Decimal] = Field(None, gt=0, le=1000000); notes: Optional[str] = Field(None, max_length=1000)

def editable(b):
    if b.status not in EDITABLE: raise HTTPException(400, "Floors and shops cannot be changed while the application is under review or after it was rejected")
def floor_get(db, u, fid):
    fl = db.get(Floor, fid)
    if not fl: raise HTTPException(404, "Not found")
    return fl, get_b(db, u, fl.building_id, write=True)
async def store(db, u, b, file, exts, **link):  # validate and save one upload; returns the new BFile
    ext = Path(file.filename or "").suffix.lower()
    if ext not in exts: raise HTTPException(400, "Only " + ", ".join(e[1:].upper() for e in exts) + " files are allowed")
    data = await file.read()
    if not data: raise HTTPException(400, "The file is empty")
    if len(data) > MAX_BYTES: raise HTTPException(400, "File is larger than 8 MB")
    f = BFile(building_id=b.id, filename=uuid.uuid4().hex + ext, sha256=hashlib.sha256(data).hexdigest(), size=len(data), uploader=u.id,
              orig_name=(file.filename or "")[:200] or None, **link)
    db.add(f); db.flush(); db.add(BFileData(file_id=f.id, data=data)); return f

@r.post("/buildings/{bid}/floors")
def add_floor(bid: int, d: NewFloor, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); b = get_b(db, u, bid, write=True); editable(b)
    label = d.label.strip()
    if not label: raise HTTPException(400, "Enter the floor label (G, 1, 2 ...)")
    if db.scalars(select(Floor).where(Floor.building_id == b.id, Floor.label == label)).first(): raise HTTPException(409, "This floor is already entered")
    n = db.scalar(select(func.count()).select_from(Floor).where(Floor.building_id == b.id))
    if n >= b.floors: raise HTTPException(400, f"This building has {b.floors} floor(s) and all are already described")
    fl = Floor(building_id=b.id, label=label, name=(d.name or "").strip() or None, m2=d.m2, notes=(d.notes or "").strip() or None)
    db.add(fl); db.flush(); log(db, u, "floor_created", f"floor:{fl.id}", f"building:{b.id} {label}"); db.commit(); return row(fl)
@r.patch("/floors/{fid}")
def edit_floor(fid: int, d: EditFloor, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); fl, b = floor_get(db, u, fid); editable(b)
    if d.m2 is not None:  # a smaller floor area must still hold the shops already on it
        used = Decimal(str(db.scalar(select(func.coalesce(func.sum(Unit.m2), 0)).where(Unit.floor_id == fl.id))))
        if used > d.m2: raise HTTPException(400, f"Shops on this floor already total {used} m²")
        fl.m2 = d.m2
    if d.name is not None: fl.name = d.name.strip() or None
    if d.notes is not None: fl.notes = d.notes.strip() or None
    log(db, u, "floor_updated", f"floor:{fl.id}"); db.commit(); return row(fl)
@r.delete("/floors/{fid}")
def del_floor(fid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); fl, b = floor_get(db, u, fid); editable(b)
    if db.scalars(select(Unit).where(Unit.floor_id == fl.id)).first(): raise HTTPException(400, "This floor still has shops")
    db.delete(fl); log(db, u, "floor_deleted", f"floor:{fid}", fl.label); db.commit(); return {"deleted": True}

@r.get("/buildings/{bid}/structure")
def structure(bid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    """Floors, the shops on each floor with photos, and for every shop its tenants (contracts) with their documents."""
    b = get_b(db, u, bid); docs_ok = u.role == "owner" or "tenant.docs" in my_powers(db, u)
    files = {}
    for f in db.scalars(select(BFile).where(BFile.building_id == b.id, (BFile.unit_id.is_not(None)) | (BFile.contract_id.is_not(None))).order_by(BFile.id)):
        files.setdefault(("u", f.unit_id) if f.unit_id else ("c", f.contract_id), []).append(f)
    fmt = lambda f: {k: v for k, v in row(f).items() if k not in ("filename", "uploader")}
    floors = []
    for fl in db.scalars(select(Floor).where(Floor.building_id == b.id).order_by(Floor.id)):
        shops = []
        for un in db.scalars(select(Unit).where(Unit.floor_id == fl.id).order_by(Unit.id)):
            tenants = []
            for c in db.scalars(select(Contract).where(Contract.unit_id == un.id).order_by(Contract.id.desc())):
                cf = files.get(("c", c.id), [])
                pays = [{**row(x), "receipt_id": x.file_id if docs_ok else None} for x in db.scalars(select(RentPayment).where(RentPayment.contract_id == c.id).order_by(RentPayment.period.desc(), RentPayment.id.desc()))]
                tenants.append({**{k: v for k, v in row(c).items() if k != "tok"}, "payments": pays, "paid_total": sum((Decimal(str(x["amount"])) for x in pays), Decimal(0)), "doc_count": len(cf), "docs": [fmt(f) for f in cf] if docs_ok else [], "docs_hidden": bool(cf) and not docs_ok})
            shops.append({**row(un), "photos": [fmt(f) for f in files.get(("u", un.id), [])], "tenants": tenants})
        floors.append({**row(fl), "used_m2": sum((Decimal(str(s["m2"])) for s in shops), Decimal(0)), "shops": shops})
    return {"building": {"id": b.id, "name": b.name, "subcity": b.subcity, "floors": b.floors, "status": b.status}, "floors": floors, "can_view_tenant_docs": docs_ok}

@r.post("/units/{uid}/photos")
async def shop_photo(uid: int, file: UploadFile = File(...), u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); un = db.get(Unit, uid)
    if not un: raise HTTPException(404, "Not found")
    b = get_b(db, u, un.building_id, write=True); editable(b)
    if db.scalar(select(func.count()).select_from(BFile).where(BFile.unit_id == un.id)) >= MAX_PHOTOS: raise HTTPException(400, f"A shop can have at most {MAX_PHOTOS} photos")
    f = await store(db, u, b, file, PHOTO_EXT, category="shop", unit_id=un.id)
    log(db, u, "shop_photo_uploaded", f"unit:{un.id}", f.sha256[:12]); db.commit(); return {k: v for k, v in row(f).items() if k != "filename"}

@r.post("/contracts/{cid}/files")
async def tenant_doc(cid: int, category: str = Form(...), doc_no: Optional[str] = Form(None), expires: Optional[str] = Form(None),
                     file: UploadFile = File(...), u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); c = db.get(Contract, cid)
    if not c: raise HTTPException(404, "Not found")
    b = get_b(db, u, db.get(Unit, c.unit_id).building_id, write=True)
    if category not in DOC_KINDS: raise HTTPException(400, "Unknown document type")
    try: exp = dt.date.fromisoformat(expires) if expires else None
    except ValueError: raise HTTPException(400, "Expiry date must look like 2027-12-31")
    if db.scalar(select(func.count()).select_from(BFile).where(BFile.contract_id == c.id)) >= MAX_DOCS: raise HTTPException(400, f"A tenant can have at most {MAX_DOCS} documents")
    f = await store(db, u, b, file, DOC_EXT, category=category, contract_id=c.id, doc_no=(doc_no or "").strip()[:60] or None, expires=exp)
    log(db, u, "tenant_document_uploaded", f"contract:{c.id}", f"{category} {f.sha256[:12]}"); db.commit()
    return {k: v for k, v in row(f).items() if k != "filename"}

@r.delete("/files/{fid}")
def delete_file(fid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    """The owner may remove a wrong upload: building files before submission, shop photos while the structure is editable,
    tenant documents only while their contract is still waiting for approval. The audit trail keeps the file's fingerprint."""
    need(u, "owner"); f = db.get(BFile, fid)
    if not f: raise HTTPException(404, "Not found")
    b = get_b(db, u, f.building_id, write=True)
    if f.category == "rent_receipt": raise HTTPException(400, "Remove the payment instead")
    if f.contract_id:
        if db.get(Contract, f.contract_id).status != "submitted": raise HTTPException(400, "Documents of an approved contract cannot be removed")
    elif f.unit_id: editable(b)
    elif b.status not in ("draft", "correction_requested"): raise HTTPException(400, "Building files cannot be removed after submission")
    blob = db.get(BFileData, f.id)
    if blob: db.delete(blob)
    log(db, u, "file_deleted", f"building:{b.id}", f"{f.category} {f.sha256[:12]}"); db.delete(f); db.commit(); return {"deleted": True}

@r.post("/contracts/{cid}/payments")
async def add_payment(cid: int, period: str = Form(...), amount: Decimal = Form(...), paid_on: str = Form(...), method: str = Form("other"),
                      reference: Optional[str] = Form(None), file: UploadFile = File(...), u: User = Depends(me), db: Session = Depends(get_db)):
    """The building owner records one rent payment: month, amount, date and the receipt (JPG, PNG or PDF). The receipt is required."""
    need(u, "owner"); c = db.get(Contract, cid)
    if not c: raise HTTPException(404, "Not found")
    b = get_b(db, u, db.get(Unit, c.unit_id).building_id, write=True)
    if c.status not in ("active", "terminated", "disputed"): raise HTTPException(400, "Rent can be recorded after the contract is approved")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period): raise HTTPException(400, "Month must look like 2027-03")
    if not (0 < amount <= 100000000): raise HTTPException(400, "Enter the rent amount paid")
    try: pd = dt.date.fromisoformat(paid_on)
    except ValueError: raise HTTPException(400, "Payment date must look like 2027-03-05")
    if pd > dt.date.today(): raise HTTPException(400, "The payment date cannot be in the future")
    if method not in METHODS: raise HTTPException(400, "Unknown payment method")
    f = await store(db, u, b, file, DOC_EXT, category="rent_receipt", contract_id=c.id, doc_no=(reference or "").strip()[:60] or None)
    x = RentPayment(contract_id=c.id, period=period, amount=amount, paid_on=pd, method=method, reference=(reference or "").strip()[:80] or None, file_id=f.id, created_by=u.id)
    db.add(x); db.flush(); log(db, u, "rent_payment_recorded", f"contract:{c.id}", f"{period} {amount} {f.sha256[:12]}"); db.commit(); return row(x)

@r.delete("/payments/{pid}")
def del_payment(pid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    """A wrong entry can be removed by the owner within 48 hours; it stays in the audit trail."""
    need(u, "owner"); x = db.get(RentPayment, pid)
    if not x: raise HTTPException(404, "Not found")
    get_b(db, u, db.get(Unit, db.get(Contract, x.contract_id).unit_id).building_id, write=True)
    if (now().replace(tzinfo=None) - x.ts.replace(tzinfo=None)) > dt.timedelta(hours=48): raise HTTPException(400, "Payments can only be removed within 48 hours")
    f = db.get(BFile, x.file_id); blob = db.get(BFileData, x.file_id)
    log(db, u, "rent_payment_deleted", f"contract:{x.contract_id}", f"{x.period} {x.amount}"); db.delete(x); db.flush()
    if blob: db.delete(blob)
    if f: db.delete(f)
    db.commit(); return {"deleted": True}
