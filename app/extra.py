"""Extra modules: MFA setup, inspections, disputes, tax (versioned, estimates only), manual verifications, CSV exports."""
import csv, io, re, base64, secrets, datetime as dt
from decimal import Decimal
from openpyxl import Workbook
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import String, Float, Numeric, DateTime, ForeignKey, Text, UniqueConstraint, select, update
from sqlalchemy.orm import Mapped, mapped_column, Session
from .main import (Base, User, Unit, Contract, Notification, me, need, allow, my_powers, get_b, get_db, log, row, now, A, cdict,
                   totp_ok, notify, list_b, list_c, Building, seal)
from .policy import VERIFY
r = APIRouter(prefix=A)

class Inspection(Base):
    __tablename__ = "inspections"
    id: Mapped[int] = mapped_column(primary_key=True); building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    officer_id: Mapped[int] = mapped_column(ForeignKey("users.id")); notes: Mapped[str] = mapped_column(Text)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True); lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[int] = mapped_column(primary_key=True); contract_id: Mapped[int] = mapped_column(ForeignKey("rental_contracts.id"))
    opened_by: Mapped[int] = mapped_column(ForeignKey("users.id")); reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open"); prev_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
class TaxRule(Base):
    __tablename__ = "tax_rule_versions"
    id: Mapped[int] = mapped_column(primary_key=True); version: Mapped[str] = mapped_column(String(40), unique=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4)); note: Mapped[str] = mapped_column(Text, default=""); active: Mapped[bool] = mapped_column(default=True)
class Declaration(Base):
    __tablename__ = "rental_income_declarations"
    __table_args__ = (UniqueConstraint("contract_id", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True); contract_id: Mapped[int] = mapped_column(ForeignKey("rental_contracts.id"))
    period: Mapped[str] = mapped_column(String(7)); declared: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    rule_version: Mapped[str] = mapped_column(String(40)); estimate: Mapped[Decimal] = mapped_column(Numeric(14, 2))
class Verification(Base):  # manual eTrade / DARS / Fayda records until an official API is connected
    __tablename__ = "manual_verifications"
    id: Mapped[int] = mapped_column(primary_key=True); kind: Mapped[str] = mapped_column(String(20)); entity: Mapped[str] = mapped_column(String(80))
    reference: Mapped[str] = mapped_column(String(120)); source: Mapped[str] = mapped_column(String(200)); evidence: Mapped[str] = mapped_column(Text, default="")
    officer_id: Mapped[int] = mapped_column(ForeignKey("users.id")); ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

def contract_for(db, u, cid):
    c = db.get(Contract, cid)
    if not c: raise HTTPException(404, "Not found")
    get_b(db, u, db.get(Unit, c.unit_id).building_id, write=True); return c

class MfaOn(BaseModel): secret: str; code: str
@r.post("/me/mfa/setup")
def mfa_setup(u: User = Depends(me)): return {"secret": base64.b32encode(secrets.token_bytes(10)).decode()}
@r.post("/me/mfa/enable")
def mfa_enable(d: MfaOn, u: User = Depends(me), db: Session = Depends(get_db)):
    if not totp_ok(d.secret, d.code.strip()): raise HTTPException(400, "Code does not match. Check the clock on your phone")
    u.mfa_secret = seal(d.secret); db.add(u); log(db, u, "mfa_enabled", f"user:{u.id}"); db.commit(); return {"ok": True}

class NewI(BaseModel): building_id: int; notes: str; lat: Optional[float] = Field(None, ge=3, le=15); lng: Optional[float] = Field(None, ge=33, le=48)
@r.post("/inspections")
def inspect(d: NewI, u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, "inspection.record"); b = get_b(db, u, d.building_id, write=True)
    i = Inspection(officer_id=u.id, **d.model_dump()); db.add(i); db.flush(); log(db, u, "inspection", f"building:{b.id}", d.notes[:80])
    notify(db, b.owner_id, f"Inspection recorded for {b.name}"); db.commit(); return row(i)
@r.get("/buildings/{bid}/inspections")
def inspections(bid: int, u: User = Depends(me), db: Session = Depends(get_db)):
    get_b(db, u, bid); return [row(i) for i in db.scalars(select(Inspection).where(Inspection.building_id == bid))]

class NewD(BaseModel): contract_id: int; reason: str = Field(min_length=3)
@r.post("/disputes")
def dispute(d: NewD, u: User = Depends(me), db: Session = Depends(get_db)):
    if u.role != "owner": allow(db, u, "dispute.freeze")
    c = contract_for(db, u, d.contract_id)
    x = Dispute(contract_id=c.id, opened_by=u.id, reason=d.reason); db.add(x)
    if u.role != "owner":  # offices freeze the record; a frozen contract still blocks the unit
        if c.status not in ("active", "submitted"): raise HTTPException(400, "Only active or submitted contracts can be frozen")
        x.prev_status, c.status = c.status, "disputed"
    db.flush(); log(db, u, "dispute_opened", f"contract:{c.id}", d.reason[:80]); db.commit(); return row(x)
@r.post("/disputes/{did}/resolve")
def resolve(did: int, u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, "dispute.resolve"); x = db.get(Dispute, did)
    if not x or x.status != "open": raise HTTPException(404, "Open dispute not found")
    c = contract_for(db, u, x.contract_id); x.status = "resolved"
    if c.status == "disputed": c.status = x.prev_status or "active"
    log(db, u, "dispute_resolved", f"contract:{c.id}"); db.commit(); return row(x)
@r.get("/disputes")
def disputes(u: User = Depends(me), db: Session = Depends(get_db)):
    ok = {c["id"] for c in list_c(u=u, db=db)}; return [row(x) for x in db.scalars(select(Dispute)) if x.contract_id in ok]

class NewRule(BaseModel): version: str; rate: Decimal = Field(ge=0, le=1); note: str = ""
@r.post("/tax/rules")
def rule(d: NewRule, u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, "tax.rules")  # rates come from the tax authority; none is hardcoded
    db.execute(update(TaxRule).values(active=False)); t = TaxRule(**d.model_dump()); db.add(t); log(db, u, "tax_rule", d.version, str(d.rate)); db.commit(); return row(t)
@r.get("/tax/rules")
def rules(u: User = Depends(me), db: Session = Depends(get_db)): return [row(t) for t in db.scalars(select(TaxRule))]
class NewDecl(BaseModel): contract_id: int; period: str; declared: Decimal = Field(ge=0)
@r.post("/tax/declarations")
def declare(d: NewDecl, u: User = Depends(me), db: Session = Depends(get_db)):
    need(u, "owner"); c = contract_for(db, u, d.contract_id)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", d.period): raise HTTPException(400, "Period must look like 2027-03")
    t = db.scalars(select(TaxRule).where(TaxRule.active)).first()
    if not t: raise HTTPException(400, "No tax rule has been configured by the tax authority yet")
    if db.scalars(select(Declaration).where(Declaration.contract_id == c.id, Declaration.period == d.period)).first(): raise HTTPException(409, "Already declared for this period")
    x = Declaration(contract_id=c.id, period=d.period, declared=d.declared, rule_version=t.version, estimate=(d.declared * t.rate).quantize(Decimal("0.01")))
    db.add(x); db.flush(); log(db, u, "declaration", f"contract:{c.id}", d.period); db.commit()
    return {**row(x), "estimate_only": True, "notice": "Estimate under rule " + t.version + ". Not an official assessment."}
@r.get("/tax/reports")
def tax_report(u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, "tax.reports"); out = []
    for x in db.scalars(select(Declaration)):
        c = db.get(Contract, x.contract_id)
        out.append({"contract": c.ref, "period": x.period, "declared": x.declared, "rent": c.rent, "estimate": x.estimate, "rule_version": x.rule_version, "mismatch": x.declared != c.rent})
    log(db, u, "tax_report_viewed", "tax"); db.commit(); return out

class NewV(BaseModel): kind: str; entity: str; reference: str; source: str; evidence: str = ""
@r.post("/verifications")
def verification(d: NewV, u: User = Depends(me), db: Session = Depends(get_db)):
    if d.kind not in VERIFY: raise HTTPException(400, "Kind must be etrade, dars or fayda")
    allow(db, u, "verify." + d.kind)  # each organization records only the kinds of verification it holds the power for
    v = Verification(officer_id=u.id, **d.model_dump()); db.add(v); db.flush(); log(db, u, "manual_verification", d.kind, d.entity); db.commit(); return row(v)
@r.get("/verifications")
def verifications(u: User = Depends(me), db: Session = Depends(get_db)):
    allow(db, u, *["verify." + k for k in VERIFY]); ks = [k for k in VERIFY if "verify." + k in my_powers(db, u)]
    return [row(v) for v in db.scalars(select(Verification)) if v.kind in ks]
@r.get("/admin/integrations")
def integrations(u: User = Depends(me)):
    need(u, "admin"); return {k: "not_connected (manual workflow only)" for k in ("fayda", "etrade", "mor", "dars", "sms")}

@r.get("/notifications")
def notes(u: User = Depends(me), db: Session = Depends(get_db)):
    return [row(n) for n in db.scalars(select(Notification).where(Notification.user_id == u.id).order_by(Notification.id.desc()).limit(30))]

def safe(v):  # stop spreadsheet formula injection
    s = str(v if v is not None else ""); return "'" + s if s[:1] in "=+-@" else s
def tb(u, db):
    return ["BRN", "name", "sub-city", "woreda", "kebele", "floors", "status", "total_m2"], [[b["brn"], b["name"], b.get("subcity"), b["woreda"], b["kebele"], b["floors"], b["status"], b["total_m2"]] for b in list_b(u=u, db=db)]
def tc(u, db):  # no Fayda data in exports
    return ["ref", "BRN", "unit", "m2", "tenant", "start", "end", "rent", "status"], [[c["ref"], c["brn"], c["unit"], c["m2"], c["tenant_name"], c["start_date"], c["end_date"], c["rent"], c["status"]] for c in list_c(u=u, db=db)]
def send(name, head, rows, u, db):
    log(db, u, "export", name); db.commit(); b = io.StringIO(); w = csv.writer(b); w.writerow(head); w.writerows([[safe(c) for c in x] for x in rows])
    return Response(b.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}.csv"})
def sendx(name, head, rows, u, db):
    log(db, u, "export", name + ".xlsx"); db.commit(); wb = Workbook(); ws = wb.active; ws.append(head)
    for x in rows: ws.append([safe(c) if isinstance(c, str) else (float(c) if isinstance(c, Decimal) else c) for c in x])
    b = io.BytesIO(); wb.save(b)
    return Response(b.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={name}.xlsx"})
@r.get("/reports/buildings.csv")
def rep_b(u: User = Depends(me), db: Session = Depends(get_db)): return send("buildings", *tb(u, db), u, db)
@r.get("/reports/contracts.csv")
def rep_c(u: User = Depends(me), db: Session = Depends(get_db)): return send("contracts", *tc(u, db), u, db)
@r.get("/reports/buildings.xlsx")
def rep_bx(u: User = Depends(me), db: Session = Depends(get_db)): return sendx("buildings", *tb(u, db), u, db)
@r.get("/reports/contracts.xlsx")
def rep_cx(u: User = Depends(me), db: Session = Depends(get_db)): return sendx("contracts", *tc(u, db), u, db)
