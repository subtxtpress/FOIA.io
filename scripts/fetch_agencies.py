"""
fetch_agencies.py — Pull all federal agency FOIA contacts from api.foia.gov
Run: python3 fetch_agencies.py YOUR_API_KEY
"""
import sys, json, urllib.request, time

API_KEY = sys.argv[1] if len(sys.argv) > 1 else ""
if not API_KEY:
    print("Usage: python3 fetch_agencies.py YOUR_API_KEY"); sys.exit(1)

BASE = "https://api.foia.gov/api/agency_components"

def fetch(url):
    req = urllib.request.Request(url, headers={"X-API-Key": API_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def get_str(v):
    """Safely extract a string from various field formats."""
    if not v: return ""
    if isinstance(v, str): return v.strip()
    if isinstance(v, list) and v: return str(v[0]).strip()
    if isinstance(v, dict):
        return (v.get("value") or v.get("uri") or v.get("url") or "").strip()
    return str(v).strip()

def get_all_components():
    components = []
    url = f"{BASE}?include=agency&fields[agency]=name,abbreviation&page[limit]=50"
    page = 0
    while url:
        page += 1
        print(f"  Fetching page {page}...", end=" ", flush=True)
        try:
            data = fetch(url)
        except Exception as e:
            print(f"ERROR: {e}"); break

        items = data.get("data", [])
        included = {i["id"]: i for i in data.get("included", []) if i.get("type") == "agency"}

        for item in items:
            attrs = item.get("attributes", {})
            rels  = item.get("relationships", {})
            agency_rel = rels.get("agency", {}).get("data", {})
            parent = included.get(agency_rel.get("id"), {})
            parent_attrs = parent.get("attributes", {})

            # ── Email ─────────────────────────────────────────────
            # API returns email as a list: ['foia@abmc.gov']
            email_raw = attrs.get("email", "")
            if isinstance(email_raw, list):
                email = email_raw[0].strip() if email_raw else ""
            else:
                email = get_str(email_raw)

            # ── Phone ─────────────────────────────────────────────
            # API field is 'telephone' not 'foia_phone'
            phone = get_str(attrs.get("telephone","") or attrs.get("foia_phone",""))

            # ── Fax ───────────────────────────────────────────────
            fax = get_str(attrs.get("fax","") or attrs.get("foia_fax",""))

            # ── Portal URL ────────────────────────────────────────
            # submission_web has the FOIA.gov portal link
            portal = ""
            sw = attrs.get("submission_web")
            if isinstance(sw, dict):
                portal = sw.get("uri","") or sw.get("url","") or ""
            elif isinstance(sw, str):
                portal = sw
            # fallback: reading_rooms first entry
            if not portal:
                rr = attrs.get("reading_rooms")
                if isinstance(rr, list) and rr:
                    portal = rr[0].get("uri","") if isinstance(rr[0], dict) else ""

            # ── Mailing Address ───────────────────────────────────
            addr = attrs.get("submission_address") or {}
            if isinstance(addr, dict):
                parts = [
                    addr.get("address_line1",""),
                    addr.get("address_line2",""),
                    addr.get("locality",""),
                    addr.get("administrative_area",""),
                    addr.get("postal_code",""),
                ]
                address = "\n".join(p for p in parts if p).strip()
            else:
                address = get_str(addr)

            # ── Officer title ─────────────────────────────────────
            officer_title = "FOIA Officer"

            components.append({
                "name":               attrs.get("title","").strip(),
                "abbreviation":       (get_str(attrs.get("abbreviation","")) or
                                       get_str(parent_attrs.get("abbreviation",""))),
                "foia_officer_title": officer_title,
                "foia_email":         email,
                "foia_phone":         phone,
                "foia_fax":           fax,
                "foia_address":       address,
                "response_days":      20,
                "portal_url":         portal,
            })

        print(f"{len(items)} components")

        # Pagination
        links = data.get("links", {})
        nxt = links.get("next")
        if isinstance(nxt, dict): nxt = nxt.get("href","")
        if nxt:
            url = nxt if nxt.startswith("http") else f"https://api.foia.gov{nxt}"
        else:
            url = None
        time.sleep(0.3)

    return components

def generate_output(components):
    def clean(s):
        return str(s or "").replace("\\","\\\\").replace('"','\\"').replace("\n","\\n")
    lines = [f"# Auto-generated from api.foia.gov — {len(components)} components\n\nAGENCIES = [\n"]
    for c in components:
        lines.append(
            f'    {{"name": "{clean(c["name"])}", "abbreviation": "{clean(c["abbreviation"])}", '
            f'"foia_officer_title": "{clean(c["foia_officer_title"])}", '
            f'"foia_email": "{clean(c["foia_email"])}", '
            f'"foia_address": "{clean(c["foia_address"])}", '
            f'"foia_phone": "{clean(c["foia_phone"])}", '
            f'"foia_fax": "{clean(c["foia_fax"])}", '
            f'"response_days": 20, '
            f'"portal_url": "{clean(c["portal_url"])}"}},\n'
        )
    lines.append("]\n")
    return "".join(lines)

if __name__ == "__main__":
    print(f"Fetching from api.foia.gov | key: {API_KEY[:8]}...\n")
    components = get_all_components()
    components = [c for c in components if c["name"]]
    print(f"\nTotal: {len(components)} components")

    with open("agencies_output.py","w") as f:
        f.write(generate_output(components))
    print(f"Written to agencies_output.py\n")
    print(f"Email:   {sum(1 for c in components if c['foia_email'])}/{len(components)}")
    print(f"Phone:   {sum(1 for c in components if c['foia_phone'])}/{len(components)}")
    print(f"Portal:  {sum(1 for c in components if c['portal_url'])}/{len(components)}")
    print(f"Address: {sum(1 for c in components if c['foia_address'])}/{len(components)}")

    # Show 3 sample entries
    print("\n--- Sample entries ---")
    for c in components[:3]:
        print(f"  {c['name']}: email={c['foia_email']} phone={c['foia_phone']} portal={c['portal_url'][:60] if c['portal_url'] else ''}")