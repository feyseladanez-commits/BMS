"""Organization powers. Pure Python (no web or database code) so the rules are easy to read and to test.

Every party (woreda office, tax authority, trade office, ...) logs in with its own account and may only use the
powers its organization has. The starting powers per organization type below are SUGGESTIONS: the registry owner
decides what each office is really mandated to do, and the administrator can change the powers of any organization."""

POWERS = {  # key -> plain-language description (the web page translates these labels)
    "registry.read": "View buildings and rental contracts in its area",
    "building.review": "Approve, reject or return building registrations (issues the BRN)",
    "contract.approve": "Approve rental contracts",
    "contract.terminate": "Terminate rental contracts",
    "dispute.freeze": "Freeze a contract by opening a dispute",
    "dispute.resolve": "Resolve disputes",
    "inspection.record": "Record inspections",
    "tenant.docs": "View the documents tenants uploaded (ID, licence, lease)",
    "verify.etrade": "Record eTrade verifications",
    "verify.dars": "Record DARS verifications",
    "verify.fayda": "Record Fayda verifications",
    "tax.rules": "Set tax rule versions",
    "tax.reports": "View rental-income declarations",
    "audit.view": "View the full audit trail",
}

# Powers that act on a building or contract. An organization that holds one must also be able to see the record,
# so clean_powers() adds registry.read automatically.
ACTIONS = {"building.review", "contract.approve", "contract.terminate", "dispute.freeze", "dispute.resolve", "inspection.record"}

VERIFY = ("etrade", "dars", "fayda")

KINDS = {  # organization type -> (label, starting powers)
    "woreda": ("Woreda office", ["registry.read", "building.review", "contract.approve", "contract.terminate",
                                 "dispute.freeze", "dispute.resolve", "inspection.record",
                                 "verify.etrade", "verify.dars", "verify.fayda", "audit.view"]),
    "kebele": ("Kebele office", ["registry.read", "inspection.record"]),
    "city": ("City or sub-city office", ["registry.read", "inspection.record", "audit.view"]),
    "revenue": ("Revenue authority (tax)", ["registry.read", "tax.rules", "tax.reports"]),
    "trade": ("Trade office (eTrade)", ["verify.etrade"]),
    "dars": ("Documents authentication (DARS)", ["verify.dars"]),
    "national_id": ("National ID (Fayda)", ["verify.fayda"]),
    "other": ("Other organization", []),
}

# The administrator account (no organization) keeps what it could do before organizations existed.
ADMIN_POWERS = {"audit.view", "tax.rules", "tax.reports", "verify.etrade", "verify.dars", "verify.fayda"}

# Accounts from versions 0.1-0.3 (roles "officer" and "tax") move into an organization with exactly the powers they had.
LEGACY = {
    "officer": KINDS["woreda"][1],
    "tax": ["registry.read", "tax.rules", "tax.reports", "verify.etrade", "verify.dars", "verify.fayda"],
}


def plist(s):
    """Powers are stored as one comma-separated string."""
    return [p for p in (s or "").split(",") if p]


def pjoin(ps):
    return ",".join(sorted(set(ps)))


def clean_powers(ps):
    """Validate a list of powers and add what they imply. Raises ValueError on an unknown power."""
    ps = set(ps)
    bad = ps - set(POWERS)
    if bad:
        raise ValueError("Unknown power: " + ", ".join(sorted(bad)))
    if ps & ACTIONS:
        ps.add("registry.read")
    return ps


def norm(s):
    """Names typed by people: ignore capital letters and spaces at the ends."""
    return (s or "").strip().lower()


def in_scope(org_woreda, org_kebele, b_woreda, b_kebele):
    """Is a building inside an organization's area? An empty woreda means every woreda; an empty kebele, the whole woreda."""
    return (not org_woreda or norm(org_woreda) == norm(b_woreda)) and (not org_kebele or norm(org_kebele) == norm(b_kebele))


def scope_error(kind, woreda, kebele):
    """Woreda and kebele offices must have an area, otherwise they would reach every record."""
    if kind == "woreda" and not woreda:
        return "A woreda office needs a woreda"
    if kind == "kebele" and not (woreda and kebele):
        return "A kebele office needs a woreda and a kebele"
    if kebele and not woreda:
        return "A kebele needs its woreda"
    return None


def meta():
    """What the web page needs to draw the organization form."""
    return {"powers": POWERS, "kinds": {k: {"label": lab, "powers": sorted(ps)} for k, (lab, ps) in KINDS.items()}}
