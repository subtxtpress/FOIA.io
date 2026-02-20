"""
FOIA.io - Complete FOIA Request Management SaaS
Flask backend with authentication, Stripe, FOIA numbering, agency lookup,
deadline calculation, Word doc generation, and action logging.
"""

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, send_file, abort
)
from db import get_db
import hashlib
import hmac
import os
import json
import re
import secrets
from datetime import datetime, date, timedelta
from functools import wraps

# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
app.permanent_session_lifetime = timedelta(days=30)
STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")  # your monthly price ID
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
MAX_UPLOAD_MB = 20
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ─────────────────────────────────────────────
# Database helpers (get_db imported from db.py)
# ─────────────────────────────────────────────


def init_db():
    db = get_db()

    # Determine primary key syntax based on database type
    pk = "SERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    # ── Users ──────────────────────────────────────────────────────────────
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id          {pk},
            username    TEXT    NOT NULL UNIQUE,
            email       TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            stripe_customer_id  TEXT,
            stripe_sub_id       TEXT,
            subscription_status TEXT DEFAULT 'inactive',
            lifetime_free       INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── Invite codes ───────────────────────────────────────────────────────
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS invite_codes (
            id          {pk},
            code        TEXT NOT NULL UNIQUE,
            used        INTEGER DEFAULT 0,
            used_by     INTEGER REFERENCES users(id),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── FOIA sequence (one row per year) ───────────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS foia_sequence (
            year    INTEGER PRIMARY KEY,
            last_seq INTEGER DEFAULT 0
        )
    """)

    # ── Federal agencies ───────────────────────────────────────────────────
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS federal_agencies (
            id              {pk},
            name            TEXT NOT NULL,
            abbreviation    TEXT,
            foia_officer_title  TEXT,
            foia_email      TEXT,
            foia_address    TEXT,
            foia_phone      TEXT,
            foia_fax        TEXT,
            response_days   INTEGER DEFAULT 20,
            portal_url      TEXT
        )
    """)

    # ── Requests ───────────────────────────────────────────────────────────
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS requests (
            id              {pk},
            user_id         INTEGER NOT NULL,
            foia_number     TEXT    NOT NULL UNIQUE,
            created_date    TEXT    NOT NULL,
            agency_type     TEXT    NOT NULL DEFAULT 'Federal',
            agency_id       INTEGER,
            agency_name     TEXT,
            state_code      TEXT,
            foia_officer_title TEXT,
            subject         TEXT,
            notes           TEXT,
            status          TEXT    NOT NULL DEFAULT 'new',
            -- populated when letter is saved
            letter_text     TEXT,
            letter_saved_at TEXT,
            -- deadline fields
            response_days   INTEGER,
            deadline_date   TEXT,
            -- active request extras
            turn            TEXT    DEFAULT 'My Turn',
            secondary_contact_name  TEXT,
            secondary_contact_email TEXT,
            -- closed
            closed_date     TEXT,
            -- appeal
            appeal_type     TEXT,
            appeal_text     TEXT,
            appeal_saved_at TEXT,
            appeal_exemption TEXT,
            -- last action
            last_action_date TEXT,
            last_action_note TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (agency_id) REFERENCES federal_agencies(id)
        )
    """)

    # ── Action log ─────────────────────────────────────────────────────────
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS action_log (
            id          {pk},
            request_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            action      TEXT    NOT NULL,
            note        TEXT,
            logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (request_id) REFERENCES requests(id)
        )
    """)

    # ── State laws reference ───────────────────────────────────────────────
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS state_laws (
            id              {pk},
            state_name      TEXT NOT NULL,
            state_code      TEXT NOT NULL UNIQUE,
            law_name        TEXT NOT NULL,
            citation        TEXT NOT NULL,
            response_days   INTEGER,
            appeal_days     INTEGER,
            notes           TEXT
        )
    """)

    # ── Attachments ────────────────────────────────────────────────────────
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS request_attachments (
            id              {pk},
            request_id      INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
            user_id         INTEGER NOT NULL REFERENCES users(id),
            filename        TEXT NOT NULL,
            original_name   TEXT NOT NULL,
            file_size       INTEGER NOT NULL,
            mime_type       TEXT,
            label           TEXT,
            uploaded_at     TEXT NOT NULL
        )
    """)

    # Commit all CREATE TABLE statements before seeding
    db.commit()

    # ── Migrations (safe to run on existing DBs) ───────────────────────────
    try:
        db.execute("ALTER TABLE users ADD COLUMN lifetime_free INTEGER DEFAULT 0")
        db.commit()
    except Exception:
        pass  # column already exists

    db.commit()
    db.close()
    _seed_agencies()
    _seed_states()


# ─────────────────────────────────────────────
# Federal agency seed data
# ─────────────────────────────────────────────
AGENCIES = [
    {"name": "American Battle Monuments Commission", "abbreviation": "ABMC", "foia_officer_title": "FOIA Officer", "foia_email": "foia@abmc.gov", "foia_address": "2300 Clarendon Boulevard, Suite 500\nAttn: FOIA\nArlington\nVA\n22201", "foia_phone": "571-842-0020", "foia_fax": "", "response_days": 20, "portal_url": "https://www.abmc.gov/foia/foia-reading-room"},
    {"name": "Administrative Conference of the United States", "abbreviation": "ACUS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Shawne McGibbon, Chief FOIA Officer\n1120 20th Street, NW, Suite 706 South\nWashington\nDC\n20036", "foia_phone": "202-480-2080", "foia_fax": "", "response_days": 20, "portal_url": "https://www.foia.gov/agency-search.html?id=9843e5a0-05b2-42a1-a5a1-61e6262b9f8b&type=component"},
    {"name": "Armed Forces Retirement Home", "abbreviation": "AFRH", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "3700 North Capitol Street NW\nP.O. Box 2875\nWashington\nDC\n20011", "foia_phone": "202-541-7535", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Appraisal Subcommittee", "abbreviation": "ASC", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@asc.gov", "foia_address": "1325 G Street NW, Suite 500\nWashington\nDC\n20005", "foia_phone": "202-289-2735", "foia_fax": "", "response_days": 20, "portal_url": "https://www.asc.gov/resources/public-records"},
    {"name": "U.S. Agency for Global Media", "abbreviation": "USAGM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA/Privacy Act Officer, USAGM/FOIA/PA Unit, Room 3349\n330 Independence Avenue, SW\nWashington\nDC\n20237", "foia_phone": "202-920-2435", "foia_fax": "", "response_days": 20, "portal_url": "https://www.usagm.gov/foia/electronic-reading-room/"},
    {"name": "Council on Environmental Quality", "abbreviation": "CEQ", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Howard Sun, FOIA Public Liaison\n730 Jackson Place, NW\nWashington\nDC\n20503", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.whitehouse.gov/ceq/information-resources/"},
    {"name": "Commission of Fine Arts", "abbreviation": "CFA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Chief FOIA Officer\n401 F St., NW, Suite 312\nWashington\nDC\n20001", "foia_phone": "202-504-2200", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Consumer Financial Protection Bureau", "abbreviation": "CFPB", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@cfpb.gov", "foia_address": "CFPB, Attn: Chief FOIA Officer\n1700 G Street, NW\nWashington\nDC\n20552", "foia_phone": "855-444-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://www.consumerfinance.gov/foia-requests/foia-electronic-reading-room/"},
    {"name": "Commodity Futures Trading Commission", "abbreviation": "CFTC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "CFTC FOIA Office, Three Lafayette Centre\n1155 21st Street, N.W.\nWashington\nDC\n20581", "foia_phone": "202-418-5912", "foia_fax": "", "response_days": 20, "portal_url": "https://www.cftc.gov/FOI/request/index.htm"},
    {"name": "Central Intelligence Agency", "abbreviation": "CIA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Information and Privacy Coordinator, Central Intelligence Agency\nWashington\nDC\n20505", "foia_phone": "703-613-1287", "foia_fax": "", "response_days": 20, "portal_url": "https://www.cia.gov/readingroom/node/256459"},
    {"name": "Council of the Inspectors General on Integrity and Efficiency", "abbreviation": "CIGIE", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA Contact, Suite 825\n1717 H Street, NW\nWashington\nDC\n20006", "foia_phone": "202-292-2600", "foia_fax": "", "response_days": 20, "portal_url": "https://www.ignet.gov/content/foia-reading-room-1"},
    {"name": "Corporation for National and Community Service", "abbreviation": "CNCS", "foia_officer_title": "FOIA Officer", "foia_email": "foia@americorps.gov", "foia_address": "Stephanie A. Soper, Freedom of Information Act Officer, Office of the General Counsel\n250 E Street, SW\nWashington\nDC\n20525", "foia_phone": "202-606-6747", "foia_fax": "", "response_days": 20, "portal_url": "https://americorps.gov/about/agency-overview/foia-privacy-act"},
    {"name": "United States Copyright Office", "abbreviation": "CO", "foia_officer_title": "FOIA Officer", "foia_email": "copfoia@loc.gov", "foia_address": "FOIA Requester Service Center\nP.O. Box 70400\nWashington\nDC\n20024-0421", "foia_phone": "202-707-3000", "foia_fax": "", "response_days": 20, "portal_url": "https://www.copyright.gov"},
    {"name": "Committee for Purchase from People Who Are Blind or Severely Disabled", "abbreviation": "CPPBSD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kimberly Zeich, Executive Director\n355 E Street SW Suite 325\nWashington\nDC\n20024", "foia_phone": "703-603-2100", "foia_fax": "", "response_days": 20, "portal_url": "http://www.abilityone.gov/laws,_regulations_and_policy/foia_reading_room.html"},
    {"name": "U.S. Chemical Safety and Hazard Investigation Board (CSB)", "abbreviation": "CSB", "foia_officer_title": "FOIA Officer", "foia_email": "foia@csb.gov", "foia_address": "1750 Pennsylvania Avenue NW\nSuite 910\nWashington\nLA\n20006", "foia_phone": "202-261-7600", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Court Services and Offender Supervision Agency", "abbreviation": "CSOSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Jeanean West, Government Information Specialist, Office of the General Counsel\n800 North Capitol Street, N.W., Suite 702\nWashington\nDC\n20002", "foia_phone": "202-220-5748", "foia_fax": "", "response_days": 20, "portal_url": "https://www.csosa.gov/foia-forms/"},
    {"name": "Denali Commission", "abbreviation": "DC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "John Whittington, FOIA Officer\n510 L Street Ste 410\nAnchorage\nAK\n99501", "foia_phone": "907-271-1640", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Headquarters & Privacy Office", "abbreviation": "PRIV", "foia_officer_title": "FOIA Officer", "foia_email": "foia@hq.dhs.gov", "foia_address": "Privacy Office, Mail Stop 0655\n2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528-0655", "foia_phone": "202-343-1743", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "U.S. Citizenship & Immigration Services", "abbreviation": "USCIS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIAPAQuestions@uscis.dhs.gov", "foia_address": "U.S. Citizenship & Immigration Services, National Records Center, FOIA/PA Office\nP.O. Box 648010\nLee's Summit\nMO\n64064-8010", "foia_phone": "800-350-5283", "foia_fax": "", "response_days": 20, "portal_url": "https://myaccount.uscis.gov/"},
    {"name": "Office for Civil Rights and Civil Liberties", "abbreviation": "CRCL", "foia_officer_title": "FOIA Officer", "foia_email": "crclfoia@hq.dhs.gov", "foia_address": "Mail Stop 0190, 2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528-0655", "foia_phone": "202-343-1743", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dhs.gov/dhs-foia-request-submission-form"},
    {"name": "U.S. Coast Guard", "abbreviation": "USCG", "foia_officer_title": "FOIA Officer", "foia_email": "efoia@uscg.mil", "foia_address": "United States Coast Guard, ATTN: FOIA Coordinator\nCommandant (CG-611), 2703 Martin Luther King Jr Ave, SE, Stop 7710\nWashington\nDC\n20593-7710", "foia_phone": "202-475-3522", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "U.S. Customs & Border Protection", "abbreviation": "CBP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Freedom of Information Act Office, U.S. Customs and Border Protection\n1300 Pennsylvania Avenue NW, Mail Stop 1181\nWashington\nDC\n20229-1181", "foia_phone": "202-325-0150", "foia_fax": "", "response_days": 20, "portal_url": "https://www.securerelease.us/account-page/sign-in/"},
    {"name": "Federal Emergency Management Agency", "abbreviation": "FEMA", "foia_officer_title": "FOIA Officer", "foia_email": "fema-foia@fema.dhs.gov", "foia_address": "Federal Emergency Management Agency, FOIA Officer,\n500 C Street, SW, Room 840\nWashington\nDC\n20472", "foia_phone": "202-646-3323", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "Federal Law Enforcement Training Center", "abbreviation": "FLETC", "foia_officer_title": "FOIA Officer", "foia_email": "fletc-foia@dhs.gov", "foia_address": "Federal Law Enforcement Training Center, FOIA Officer\n1131 Chapel Crossing Road, Building #681\nGlynco\nGA\n31524", "foia_phone": "912-267-3103", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "U.S. Immigration & Customs Enforcement", "abbreviation": "ICE", "foia_officer_title": "FOIA Officer", "foia_email": "ice-foia@ice.dhs.gov", "foia_address": "United States Immigration & Customs Enforcement, FOIA Office\n500 12th Street, SW, Stop 5009\nWashington\nDC\n20536-5009", "foia_phone": "866-633-1182", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA.OIG@oig.dhs.gov", "foia_address": "Office of the Inspector General, FOIA Public Liaison, DHS-OIG Counsel\n245 Murray Lane, SW, Stop 0305\nWashington\nDC\n20528-0305", "foia_phone": "202-981-6100", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dhs.gov/dhs-foia-request-submission-form"},
    {"name": "Office of Intelligence & Analysis", "abbreviation": "I&A", "foia_officer_title": "FOIA Officer", "foia_email": "iafoia@hq.dhs.gov", "foia_address": "Privacy Office, Mail Stop 0655 \n2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528", "foia_phone": "202-447-4561", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dhs.gov/dhs-foia-request-submission-form"},
    {"name": "Cybersecurity and Information Security Agency", "abbreviation": "CISA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@hq.dhs.gov", "foia_address": "DHS Privacy Office, Mail Stop 0655\n2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528", "foia_phone": "703-235-2211", "foia_fax": "", "response_days": 20, "portal_url": "https://www.securerelease.us/create-request/agency/dd766a62-27ba-4cd1-ba1f-0fe7708077e2/component/039aa896-2c55-450e-be50-7d2e87f877fd"},
    {"name": "Science & Technology Directorate", "abbreviation": "S&T", "foia_officer_title": "FOIA Officer", "foia_email": "foia@hq.dhs.gov", "foia_address": ", 2707 Martin Luther King Jr. AVE SE\n245 Murray Ln SW\nWashington\nDC\n20528", "foia_phone": "202-343-1743 or 866-431-0486", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dhs.gov/dhs-foia-request-submission-form"},
    {"name": "United States Secret Service", "abbreviation": "USSS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@usss.dhs.gov", "foia_address": "Communications Center (FOIA/PA)\n245 Murray Drive, Building T-5\nWashington\nDC\n20223", "foia_phone": "202-406-6370", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "Transportation Security Administration", "abbreviation": "TSA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@tsa.dhs.gov", "foia_address": "TSA FOIA \n6595 Springfield Center Drive \nSpringfield\nVA\n20598-6020", "foia_phone": "866-364-2872", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "Office of Biometric Identity Management", "abbreviation": "OBIM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "DHS Privacy Office, Mail Stop 0655\n2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.securerelease.us/create-request/agency/dd766a62-27ba-4cd1-ba1f-0fe7708077e2/component/33dd590b-eb58-4551-8fc2-2fe404a87248"},
    {"name": "Defense Nuclear Facilities Safety Board", "abbreviation": "DNFSB", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@dnfsb.gov", "foia_address": "FOIA Office\n625 Indiana Avenue, NW, Suite 700\nWashington\nDC\n20004", "foia_phone": "(202) 694-7000; (800) 788-4016", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dnfsb.gov/foia-reading-room"},
    {"name": "Departmental Offices", "abbreviation": "DOC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Bobbie Parsons, Deputy Chief FOIA Officer, Office of Privacy and Open Government, Room 61013\n14th Street and Constitution Avenue, NW\nWashington\nDC\n20230", "foia_phone": "202-482-3257", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Bureau of Economic Analysis", "abbreviation": "BEA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@bea.gov", "foia_address": "Communication Division Mail Stop BE-64\n4600 Silver Hill Road, Room 8K120C\nWashington\nDC\n20233", "foia_phone": "301-278-9798", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Census Bureau", "abbreviation": "CEN", "foia_officer_title": "FOIA Officer", "foia_email": "census.efoia@census.gov", "foia_address": "ATTN: FOIA/PA Office\n4600 Silver Hill Road\nWashington\nDC\n20233-3700", "foia_phone": "301-763-2127", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Office of the Under Secretary for Economic Affairs", "abbreviation": "OUSEA", "foia_officer_title": "FOIA Officer", "foia_email": "OUSEA-FOIA@bea.gov", "foia_address": "FOIA Officer, Office of the Under Secretary for Economic Affairs\n1401 Constitution Ave, N.W., Mail Stop H4843\nWashington\nDC\n20230", "foia_phone": "301-278-9798", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Economic Development Administration", "abbreviation": "EDA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA-EDA@eda.gov", "foia_address": "1401 Constitution Avenue NW\nRoom 72023\nWashington\nDC\n20230", "foia_phone": "202-482-1315", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Bureau of Industry and Security", "abbreviation": "BIS", "foia_officer_title": "FOIA Officer", "foia_email": "efoiarequest@bis.doc.gov", "foia_address": "1401 Constitution Avenue NW\nRoom 6622\nWashington\nDC\n20230", "foia_phone": "202-482-7893", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "International Trade Administration", "abbreviation": "ITA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@trade.gov", "foia_address": "1401 Constitution Avenue NW\nRoom 40003\nWashington\nDC\n20230", "foia_phone": "202-482-5436", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Minority Business Development Agency", "abbreviation": "MBDA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@mbda.gov", "foia_address": "1401 Constitution Avenue NW\nRoom 5093\nWashington\nDC\n20230", "foia_phone": "202-482-2332", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "National Institute of Standards and Technology", "abbreviation": "NIST", "foia_officer_title": "FOIA Officer", "foia_email": "foia@nist.gov", "foia_address": "100 Bureau Drive\nMail Stop 1710\nGaithersburg\nMD\n20899-1710", "foia_phone": "301-975-4074", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "National Technical Information Service", "abbreviation": "NTIS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Office of Administrative Management\n5301 Shawnee Road\nAlexandria\nVA\n22312", "foia_phone": "703-605-6543", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "National Telecommunications and Information Administration", "abbreviation": "NTIA", "foia_officer_title": "FOIA Officer", "foia_email": "eFOIA@ntia.gov", "foia_address": "Office of the Chief Counsel\n1401 Constitution Avenue NW, Room 4713\nWashington\nDC\n20230", "foia_phone": "202-482-1816", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "National Oceanic and Atmospheric Administration", "abbreviation": "NOAA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@noaa.gov", "foia_address": "NOAA FOIA Office (SOU 10000)\n1315 East-West Highway, Room 9719\nSilver Spring\nMD\n20910", "foia_phone": "301-628-5658", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "U.S. Patent and Trademark Office", "abbreviation": "PTO", "foia_officer_title": "FOIA Officer", "foia_email": "FOIARequests@uspto.gov", "foia_address": "Louis Boston Jr., FOIA Contact\nP.O. Box 1450\nAlexandria\nVA\n22313-1450", "foia_phone": "571-272-3000", "foia_fax": "", "response_days": 20, "portal_url": "https://www.uspto.gov/learning-and-resources/ip-policy/electronic-freedom-information-act-e-foia"},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@oig.doc.gov", "foia_address": "Jennifer Piel, FOIA Officer\n1401 Constitution Avenue NW, Room 7896\nWashington\nDC\n20230", "foia_phone": "202-482-5992", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Immediate Office of the Secretary", "abbreviation": "IOS", "foia_officer_title": "FOIA Officer", "foia_email": "IOSFOIA@doc.gov", "foia_address": "Office of Privacy and Open Government\n1401 Constitution Avenue NW, Room 61025\nWashington\nDC\n20230", "foia_phone": "202-482-3257", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Headquarters", "abbreviation": "HQ", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Alexander Morris, FOIA Officer, Mail Stop MA-46\n1000 Independence Avenue, SW\nWashington\nDC\n20585", "foia_phone": "202-586-3159", "foia_fax": "", "response_days": 20, "portal_url": "http://energy.gov/doe-headquarters-foia-request-form"},
    {"name": "Chicago Office", "abbreviation": "CH", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michelle Galbert, FOIA Officer\n9800 South Cass Avenue\nLemont\nIL\n60423", "foia_phone": "630-252-2041", "foia_fax": "", "response_days": 20, "portal_url": "http://science.energy.gov/isc/foia/how-to-submit-a-foia-request/request-form/"},
    {"name": "Idaho Operations Office", "abbreviation": "ID", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Anna Christensen, FOIA Officer, MS 1203\n1955 Fremont Avenue\nIdaho Falls\nID\n83401", "foia_phone": "208-526-4223", "foia_fax": "", "response_days": 20, "portal_url": "http://www.id.energy.gov/foia/RequestForm.aspx"},
    {"name": "Oak Ridge Office", "abbreviation": "ORO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michelle Galbert, FOIA Officer\n9800 South Cass Avenue\nLemont\nIL\n60423", "foia_phone": "630-252-2041", "foia_fax": "", "response_days": 20, "portal_url": "http://science.energy.gov/isc/foia/how-to-submit-a-foia-request/request-form/"},
    {"name": "Office of Scientific and Technical Information", "abbreviation": "OSTI", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Erin Anderson, FOIA Officer\nP.O. Box 62\nOak Ridge\nTN\n37831", "foia_phone": "865-576-8408", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Richland Operations Office", "abbreviation": "ROO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "U.S. Department of Energy, Hanford Field Office\nP.O. Box 550, Mail Stop H5-20\nRichland\nWA\n99352", "foia_phone": "509-376-6288", "foia_fax": "", "response_days": 20, "portal_url": "http://www.hanford.gov/c.cfm/foia/"},
    {"name": "Savannah River Office", "abbreviation": "SRO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Savannah River Operations Office\nP.O. Box A\nAiken\nSC\n29801", "foia_phone": "803-952-7813", "foia_fax": "", "response_days": 20, "portal_url": "https://sro.srs.gov/docs/e_foiarequestform.pdf"},
    {"name": "National Nuclear Security Administration", "abbreviation": "NNSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Annie Maxfield, FOIA Officer\nP.O. Box 5400\nAlbuquerque\nNM\n87185-5400", "foia_phone": "866-747-5994", "foia_fax": "", "response_days": 20, "portal_url": "http://nnsa.energy.gov/aboutus/ouroperations/generalcounsel/foia/howtosubmitfoiareq"},
    {"name": "Office of Naval Reactors", "abbreviation": "NR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Clifford Nunn, FOIA Officer\nP.O. Box 109\nWest Mifflin\nPA\n15122-0109", "foia_phone": "412-476-7202", "foia_fax": "", "response_days": 20, "portal_url": "https://www.energy.gov/doe-headquarters-foia-request-form"},
    {"name": "Carlsbad Field Office", "abbreviation": "CBFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Myles Hall, FOIA Officer, P.O. Box 3090\n4021 National Parks Highway\nCarlsbad\nNM\n88221", "foia_phone": "575-234-7010", "foia_fax": "", "response_days": 20, "portal_url": "http://www.wipp.energy.gov/library/foia/Freedom_of_Information_Act_Request_Form.pdf"},
    {"name": "Golden Field Office", "abbreviation": "GFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Jason Hadnot, FOIA Officer, Mail Stop RSF DOE\n15013 Denver West Parkway\nGolden\nCO\n80401", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.eere-pmc.energy.gov/externalaffairs/FOIARequest.aspx"},
    {"name": "Environmental Management Consolidated Business Center", "abbreviation": "EMCBC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "DOE/EMCBC Office of Chief Counsel\n550 Main Street, Room 7-335\nCincinnati\nOH\n45202", "foia_phone": "513-246-0579", "foia_fax": "", "response_days": 20, "portal_url": "https://www.emcbc.doe.gov/foia/foia_form.php"},
    {"name": "Bonneville Power Administration", "abbreviation": "BPA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Candice Palen, FOIA Officer, CGI-7\nP.O. Box 3621\nPortland\nOR\n97208-3621", "foia_phone": "503-230-5602", "foia_fax": "", "response_days": 20, "portal_url": "http://www.bpa.gov/news/FOIA/Pages/RequestForm.aspx"},
    {"name": "Southeastern Power Administration", "abbreviation": "SEPA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Leon Jourolmon, FOIA Officer\n1166 Athens Tech Road\nElberton\nGA\n30635-6711", "foia_phone": "706-213-3810", "foia_fax": "", "response_days": 20, "portal_url": "https://energy.gov/doe-headquarters-foia-request-form"},
    {"name": "Southwestern Power Administration", "abbreviation": "SWPA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "6655 S Lewis\nAttn: S1500\nTulsa\nOK\n74136", "foia_phone": "918-595-6605", "foia_fax": "", "response_days": 20, "portal_url": "https://www.swpa.gov/PDFs/foia/foia_form.pdf"},
    {"name": "Western Area Power Administration", "abbreviation": "WAPA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "John Kral, FOIA Officer, P.O. Box 281213\n12155 West Alameda Parkway\nLakewood\nCO\n80228-8213", "foia_phone": "720-962-7139", "foia_fax": "", "response_days": 20, "portal_url": "https://www.wapa.gov/FOIA/Pages/make-request.aspx"},
    {"name": "Strategic Petroleum Reserve", "abbreviation": "SPR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kristin Frischhertz, FOIA Officer, MS FE-445\n900 Commerce Road East\nNew Orleans\nLA\n70123", "foia_phone": "504-734-4297", "foia_fax": "", "response_days": 20, "portal_url": "https://www.spr.doe.gov/FOIA/FOIA_Request_Form2.html"},
    {"name": "National Energy Technology Laboratory", "abbreviation": "NETL", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Erika Fincham, FOIA Officer, M/S 922/M214\nP.O. Box 10940\nPittsburgh\nPA\n15236-0940", "foia_phone": "412-386-6167", "foia_fax": "", "response_days": 20, "portal_url": "http://www.netl.doe.gov/home/foai-request-form"},
    {"name": "Bureau of Indian Affairs", "abbreviation": "BIA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@bia.gov", "foia_address": "Justin Davis, FOIA Officer\nMS-4660; 1849 C Street, NW\nWashington\nDC\n20240", "foia_phone": "(202) 513-7707", "foia_fax": "", "response_days": 20, "portal_url": "https://www.bia.gov/as-ia/foia/reading-room"},
    {"name": "Bureau of Land Management", "abbreviation": "BLM", "foia_officer_title": "FOIA Officer", "foia_email": "blm_wo_foia@blm.gov", "foia_address": "Ryan Witt, FOIA Officer, Attn: FOIA Office (WO-640)\n1849 C Street, N.W.\nWashington\nDC\n20240", "foia_phone": "970-256-4998", "foia_fax": "", "response_days": 20, "portal_url": "https://www.blm.gov/about/foia/foia-reading-room"},
    {"name": "Bureau of Ocean Energy Management", "abbreviation": "BOEM", "foia_officer_title": "FOIA Officer", "foia_email": "boemfoia@boem.gov", "foia_address": "Ashley Rychak, FOIA Officer\n45600 Woodland Road\nSterling\nVA\n20166", "foia_phone": "703-787-1204", "foia_fax": "", "response_days": 20, "portal_url": "https://www.boem.gov/about-boem/foia/foia-e-libraries"},
    {"name": "Bureau of Reclamation", "abbreviation": "BOR", "foia_officer_title": "FOIA Officer", "foia_email": "borfoia@usbr.gov", "foia_address": "P.O. Box 25007\nDenver\nCO\n80225-0007", "foia_phone": "303-445-3282", "foia_fax": "", "response_days": 20, "portal_url": "https://www.usbr.gov/foia/readroom.html"},
    {"name": "Bureau of Safety and Environmental Enforcement", "abbreviation": "BSEE", "foia_officer_title": "FOIA Officer", "foia_email": "bseefoia@bsee.gov", "foia_address": "Michelle L. Piland, FOIA Officer, VAE-BSEE-FOIA\n45600 Woodland Road\nSterling\nVA\n20166", "foia_phone": "715-791-4737", "foia_fax": "", "response_days": 20, "portal_url": "https://www.bsee.gov/newsroom/library"},
    {"name": "U.S. Fish and Wildlife Service", "abbreviation": "FWS", "foia_officer_title": "FOIA Officer", "foia_email": "fwhq_foia@fws.gov", "foia_address": "Erik Germain, FOIA Officer, MS: IRTM\n5275 Leesburg Pike\nFalls Church\nVA\n22041", "foia_phone": "303-275-2348", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fws.gov/program/fws-freedom-information-act-foia/foia-reading-room"},
    {"name": "U.S. Geological Survey", "abbreviation": "USGS", "foia_officer_title": "FOIA Officer", "foia_email": "foia@usgs.gov", "foia_address": "Melanie Ruiz, FOIA Officer\nP.O. Box 66783\nAlbuquerque\nNM\n87193", "foia_phone": "571-512-8459", "foia_fax": "", "response_days": 20, "portal_url": "https://www.usgs.gov/about/organization/science-support/foia/elibraries"},
    {"name": "National Park Service", "abbreviation": "NPS", "foia_officer_title": "FOIA Officer", "foia_email": "npsfoia@nps.gov", "foia_address": "Nicholas Banco FOIA Officer\n1849 C Street, Room 2214\nWashington\nDC\n20240", "foia_phone": "202-641-1621", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nps.gov/aboutus/foia/foia-reading-room.htm"},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "foia@doioig.gov", "foia_address": "Alexandra Ellis, FOIA Officer, DOI/OIG\n1849 C Street, NW, MS-4428  \nWashington\nDC\n20240", "foia_phone": "703-581-3425", "foia_fax": "", "response_days": 20, "portal_url": "https://www.doioig.gov/complaints-requests/freedom-information-act-foia"},
    {"name": "Office of the Secretary", "abbreviation": "OS", "foia_officer_title": "FOIA Officer", "foia_email": "osfoia@ios.doi.gov", "foia_address": "Leah Fairman, FOIA Officer\n1849 C Street, NW, MS-7328, MIB\nWashington\nDC\n20240", "foia_phone": "202-513-0765", "foia_fax": "", "response_days": 20, "portal_url": "https://www.doi.gov/foia/os"},
    {"name": "Office of the Solicitor", "abbreviation": "SOL", "foia_officer_title": "FOIA Officer", "foia_email": "sol.foia@sol.doi.gov", "foia_address": "Lance Purvis, FOIA Officer, MS-6540, MIB\n1849 C Street, NW\nWashington\nDC\n20240", "foia_phone": "202-208-5817", "foia_fax": "", "response_days": 20, "portal_url": "https://www.doi.gov/solicitor/opinions"},
    {"name": "Office of Surface Mining Reclamation and Enforcement", "abbreviation": "OSM", "foia_officer_title": "FOIA Officer", "foia_email": "foia@osmre.gov", "foia_address": "Dele Awoniyi, FOIA Officer, MS 1200W-MIB \n1849 C Street NW\nWashington\nDC\n20240", "foia_phone": "202-208-5840", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/request"},
    {"name": "Office of the Special Trustee", "abbreviation": "OST", "foia_officer_title": "FOIA Officer", "foia_email": "ost_foia@ost.doi.gov", "foia_address": "Keith Parsky, FOIA Officer\n1849 C Street, NW, Room 3214\nWashington\nDC\n20240", "foia_phone": "202-208-3521", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/request"},
    {"name": "Office of the Attorney General", "abbreviation": "Office of the AG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff, 6th Floor\n441 G St, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Office of the Deputy Attorney General", "abbreviation": "Office of the DAG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff, 6th Floor\n441 G St, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Office of the Associate Attorney General", "abbreviation": "Office of the Assoc. AG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff, 6th Floor\n441 G St, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Antitrust Division", "abbreviation": "Antitrust", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Laura Starling, Acting Chief, FOIA/PA Unit, Liberty Square Building\nSuite 1000, 450 5th Street, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-2692", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/atr/foia-library"},
    {"name": "Bureau of Alcohol, Tobacco, Firearms, and Explosives", "abbreviation": "ATF", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Hirsh Kravitz, Acting Division Chief, Room 4.E-301\n99 New York Avenue, NE\nWashington\nDC\n20226", "foia_phone": "202-648-8740", "foia_fax": "", "response_days": 20, "portal_url": "https://www.atf.gov/resource-center/foia-library"},
    {"name": "Civil Division", "abbreviation": "Civil", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Civil Division, Office of Records and Information\n1100 L Street, NW, Room 8400\nWashington\nDC\n20035", "foia_phone": "202-514-2319", "foia_fax": "", "response_days": 20, "portal_url": "http://www.justice.gov/civil/common/foia_request.html"},
    {"name": "Civil Rights Division", "abbreviation": "Civil Rights", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kilian Kagle, Chief, 4CON, Room 6.153\n950 Pennsylvania Avenue, NW\nWashington\nDC\n20530", "foia_phone": "202-514-4209", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/crt/foia-electronic-reading-room"},
    {"name": "Community Relations Service", "abbreviation": "CRS", "foia_officer_title": "FOIA Officer", "foia_email": "MRUFOIA.Requests@usdoj.gov", "foia_address": "Kevin Krebs, Assistant Director, FOIA/Privacy Unit\n175 N Street, NE, Suite 5.400\nWashington\nDC\n20530-0001", "foia_phone": "202-252-6020", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/crs/crs-electronic-reading-room"},
    {"name": "Criminal Division", "abbreviation": "Criminal", "foia_officer_title": "FOIA Officer", "foia_email": "crm.foia@usdoj.gov", "foia_address": "Criminal Division, Department of Justice\n950 Constitution Ave, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-616-0307", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/criminal/foia-library"},
    {"name": "Drug Enforcement Administration", "abbreviation": "DEA", "foia_officer_title": "FOIA Officer", "foia_email": "DEA.FOIA@usdoj.gov", "foia_address": "Angela C. Davis, Chief, Freedom of Information and Privacy Act Section\n8701 Morrissette Drive\nSpringfield\nVA\n22152", "foia_phone": "571-776-2300", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dea.gov/foia/foia-library"},
    {"name": "Environment and Natural Resources Division", "abbreviation": "ENRD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Charles Smiroldo, FOIA Coordinator, Law and Policy Section\nP.O. Box 7415, Ben Franklin Station\nWashington\nDC\n20044-7415", "foia_phone": "202-514-0424", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/enrd/selected-publications"},
    {"name": "Executive Office for Immigration Review", "abbreviation": "EOIR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Executive Office for Immigration Review, FOIA Service Center\n5107 Leesburg Pike, Suite 2150\nFalls Church\nVA\n22041", "foia_phone": "703-605-1297", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/eoir/foia-library-0"},
    {"name": "Executive Office for Organized Crime Drug Enforcement Task Forces", "abbreviation": "OCDETF", "foia_officer_title": "FOIA Officer", "foia_email": "JMDFOIA@usdoj.gov", "foia_address": "950 Pennsylvania Ave NW\nRoom 1111 RFK\nWashington\nDC\n20530", "foia_phone": "202-514-0296", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/oip/foia-library"},
    {"name": "Executive Office for United States Attorneys", "abbreviation": "EOUSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kevin Krebs, Assistant Director, FOIA/Privacy Unit\n175 N Street, NE, Suite 5.400\nWashington\nDC\n20530-0001", "foia_phone": "202-252-6020", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/usao/resources/foia-library"},
    {"name": "Executive Office for United States Trustees", "abbreviation": "EOUST", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Randi Meth, FOIA Counsel, Office of the General Counsel\n441 G Street, NW, Suite 6150\nWashington\nDC\n20530", "foia_phone": "202-307-1399", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/ust/foia-privacy-act/foia-library"},
    {"name": "Federal Bureau of Investigation", "abbreviation": "FBI", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Amie Napier, Section Chief, Record/Information Dissemination Section\n200 Constitution Drive\nWinchester\nVA\n22602", "foia_phone": "540-868-4500", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia.fbi.gov"},
    {"name": "Federal Bureau of Prisons", "abbreviation": "BOP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kara Christenson, Room 924, HOLC Building\n320 First Street, NW\nWashington\nDC\n20534", "foia_phone": "202-514-6655", "foia_fax": "", "response_days": 20, "portal_url": "https://www.bop.gov/foia/#tabs-5"},
    {"name": "Foreign Claims Settlement Commission", "abbreviation": "FCSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Jeremy LaFrancois, Chief Administrative Counsel, Room 6330\n441 G Street, NW \nWashington\nDC\n20579", "foia_phone": "202-616-6975", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/fcsc/foia-library"},
    {"name": "INTERPOL-United States National Central Bureau", "abbreviation": "USNCB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Jessica Younger, FOIA Public Liaison, Office of General Counsel\nCG-3 15th Floor\nWashington\nDC\n20530-0001", "foia_phone": "202-616-0201", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/interpol-washington/foia-library"},
    {"name": "Justice Management Division", "abbreviation": "JMD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Daniel Wagner, FOIA Contact, Room 1111 RFK\n950 Pennsylvania Avenue NW\nWashington\nDC\n20530-0001", "foia_phone": "(202) 616-0253", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/jmd/foia-library"},
    {"name": "National Security Division", "abbreviation": "NSD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Arnetta Mallory, Government Information Specialist, 3 Constitution Square\n175 N Street N.E., 12th Floor\nWashington\nDC\n20530", "foia_phone": "202-233-2639", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/nsd/nsd-foia-library"},
    {"name": "Office of Community Oriented Policing Services", "abbreviation": "COPS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Nicole Lee, FOIA Officer, Legal Division, Two Constitution Square\n145 N Street, NE\nWashington\nDC\n20530-0001", "foia_phone": "202-616-0825", "foia_fax": "", "response_days": 20, "portal_url": "https://cops.usdoj.gov/Default.asp?Item=40"},
    {"name": "Office of Information Policy", "abbreviation": "OIP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff\n441 G St, NW, 6th Floor \nWashington\nDC\n20530", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.regulations.gov/foia/action/public/home"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Deborah M. Waller, Government Information Specialist, Room 4726\n950 Pennsylvania Avenue, NW\nWashington\nDC\n20530", "foia_phone": "202-616-0646", "foia_fax": "", "response_days": 20, "portal_url": "https://oig.justice.gov/foia/eroom.htm"},
    {"name": "Office of Justice Programs", "abbreviation": "OJP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Monica Potter-Johnson, FOIA Officer, Room 5400\n810 7th Street, NW\nWashington\nDC\n20531", "foia_phone": "202-307-6235", "foia_fax": "", "response_days": 20, "portal_url": "https://ojp.gov/about/foia/ojp_foia_library.htm"},
    {"name": "Office of Legal Counsel", "abbreviation": "OLC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Melissa Golden, Lead Paralegal and FOIA Specialist, Room 5517\n950 Pennsylvania Avenue, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-2053", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/olc/olc-foia-electronic-reading-room"},
    {"name": "Office of Legal Policy", "abbreviation": "OLP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff, 6th Floor\n441 G St, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Office of Legislative Affairs", "abbreviation": "OLA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff, 6th Floor\n441 G St, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Office of the Pardon Attorney", "abbreviation": "PARDON", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Teresita Mutton, Attorney Advisor, Office of the Pardon Attorney, U.S. Department of Justice\n950 Pennsylvania Avenue, N.W.\nWashington\nDC\n20530", "foia_phone": "202-616-6070", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/pardon/pardon-attorney-foia/foia-library"},
    {"name": "Office of Professional Responsibility", "abbreviation": "OPR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Carmen Smith Carter, Assistant Counsel for the Freedom of Information and Privacy Acts, Suite 3241\n950 Pennsylvania Avenue, NW\nWashington\nDC\n20530", "foia_phone": "202-514-3365", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/opr/OPR-FOIA/opr-electronic-reading-room"},
    {"name": "Office of Public Affairs", "abbreviation": "Public Affairs", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff, 6th Floor\n441 G St, NW\nWashington\nDC\n20530-0001", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.regulations.gov/foia/action/public/home"},
    {"name": "Office of the Solicitor General", "abbreviation": "OSG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Shannon Munro, FOIA Officer and Executive Officer\n950 Pennsylvania Avenue, NW, Room 7115\nWashington\nDC\n20530-0001", "foia_phone": "202-616-9406", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/osg/osg-freedom-information-act-0"},
    {"name": "Office of Tribal Justice", "abbreviation": "OTJ", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kevin Krebs, Assistant Director, FOIA/Privacy Unit\n175 N Street, NE Suite 5.400\nWashington\nDC\n20530-0001", "foia_phone": "202-252-6020", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/usao/resources/foia-library"},
    {"name": "Office on Violence Against Women", "abbreviation": "OVW", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Diane Dauplaise, Suite 10W.121\n145 N Street, NE\nWashington\nDC\n20530", "foia_phone": "202-307-6026", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/ovw/ovw-freedom-information-act"},
    {"name": "Professional Responsibility Advisory Office", "abbreviation": "PRAO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Marguerite A. Driessen, Senior Legal Advisor, 6th Floor\n441 G Street, NW\nWashington\nDC\n20530", "foia_phone": "202-514-0458", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/prao/foia-library"},
    {"name": "Tax Division", "abbreviation": "TAX", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Civil Division, FOIA and Privacy Office\n1100 L Street, NW, Room 8400\nWashington\nDC\n20035", "foia_phone": "202-514-2319", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/tax/foia-library"},
    {"name": "United States Marshals Service", "abbreviation": "USMS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Charlotte Luckstone, FOIA Officer, Office of General Counsel\nCG-3 15th Floor\nWashington\nDC\n20530-1000", "foia_phone": "703-740-3943", "foia_fax": "", "response_days": 20, "portal_url": "https://www.usmarshals.gov/freedom-of-information-act"},
    {"name": "United States Parole Commission", "abbreviation": "USPC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Anissa Banks, FOIA/PA Specialist, 3rd Floor\n90 K Street, NE\nWashington\nDC\n20530", "foia_phone": "202-346-7030", "foia_fax": "", "response_days": 20, "portal_url": "https://www.justice.gov/uspc/freedom-information-act-foia/foia-library"},
    {"name": "Office of the Secretary", "abbreviation": "OST", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michael Bell, FOIA Officer, W94-122\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-4542", "foia_fax": "", "response_days": 20, "portal_url": "https://www.transportation.gov/foia/ost-electronic-reading-room"},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA Officer, 7th Floor\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-6131", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oig.dot.gov/freedom-information-act"},
    {"name": "Federal Aviation Administration", "abbreviation": "FAA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Freedom of Information Act Office, FAA, AFN-400\n800 Independence Avenue, SW\nWashington\nDC\n20591", "foia_phone": "202-267-7799", "foia_fax": "", "response_days": 20, "portal_url": "http://www.faa.gov/foia/foia_request/"},
    {"name": "Federal Highway Administration", "abbreviation": "FHWA", "foia_officer_title": "FOIA Officer", "foia_email": "foia.officer@dot.gov", "foia_address": "HQ: Federal Highway Administration  Attn: FOIA Officer\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-8408", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fhwa.dot.gov/foia/err.cfm"},
    {"name": "Federal Railroad Administration", "abbreviation": "FRA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Linda Martin, Assistant Chief Counsel, Acting FRA FOIA Officer\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-493-6039", "foia_fax": "", "response_days": 20, "portal_url": "https://railroads.dot.gov/freedom-information-act-foia"},
    {"name": "Federal Transit Administration", "abbreviation": "FTA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Nancy Sipes, FOIA/PA Contact, E42-315\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-2496", "foia_fax": "", "response_days": 20, "portal_url": "https://www.transit.dot.gov/foia/foia-electronic-reading-room"},
    {"name": "Federal Motor Carrier Safety Administration", "abbreviation": "FMCSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Jennifer Weatherly, FOIA/PA Contact (MC-MMI), W64-304\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-2960", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fmcsa.dot.gov/foia/foia-electronic-reading-room"},
    {"name": "National Highway Traffic Safety Administration", "abbreviation": "NHTSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA Contact, NHTSA Executive Secretariat, W41-304\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-1834", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nhtsa.gov/about-nhtsa/electronic-reading-room"},
    {"name": "Maritime Administration", "abbreviation": "MARAD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "T. Mitchell Hudson, Jr., FOIA Officer, Division of Legislation and Regulations, W24-220\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-5320", "foia_fax": "", "response_days": 20, "portal_url": "https://www.marad.dot.gov/about-us/foia/electronic-reading-room/"},
    {"name": "Pipeline and Hazardous Materials Safety Administration", "abbreviation": "PHMSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Dele Awoniyi, FOIA Officer, E23-306\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-4400", "foia_fax": "", "response_days": 20, "portal_url": "https://www.phmsa.dot.gov/foia/e-reading-room"},
    {"name": "Great Lakes St. Lawrence Seaway Development Corporation", "abbreviation": "GLS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michael Bell, FOIA Officer, W94-122\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "202-366-4542", "foia_fax": "", "response_days": 20, "portal_url": "https://www.seaway.dot.gov/publications/electronic-reading-room"},
    {"name": "Office of the Secretary and Joint Staff", "abbreviation": "OSD/JS", "foia_officer_title": "FOIA Officer", "foia_email": "whs.mc-alex.esd.mbx.osd-js-foia-requester-service-center@mail.mil", "foia_address": "OSD/JS FOIA Requester Service Center, Office of Freedom of Information\n1155 Defense Pentagon\nWashington\nDC\n20301-1155", "foia_phone": "866-574-4970", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.whs.mil/"},
    {"name": "Department of the Air Force", "abbreviation": "AF", "foia_officer_title": "FOIA Officer", "foia_email": "DAF.FOIA@us.af.mil", "foia_address": "SAF/AMGI (FOIA)\n1000 Air Force Pentagon\nWashington\nDC\n20330-1000", "foia_phone": "(703) 614-8500", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia.milcloud.mil/palMain.aspx"},
    {"name": "Department of the Army", "abbreviation": "ARMY", "foia_officer_title": "FOIA Officer", "foia_email": "usarmy.belvoir.hqda-esa.mbx.rmda-foia@army.mil", "foia_address": "U.S. Army Freedom of Information Act Office Army Records Management Directorate\n9301 Chapek Rd. Bldg 1458\nFort Belvoir\nVA\n22060-5605", "foia_phone": "571-515-0306", "foia_fax": "", "response_days": 20, "portal_url": "https://www.foia.army.mil/foiarequest/default.aspx"},
    {"name": "Department of the Navy", "abbreviation": "NAVY", "foia_officer_title": "FOIA Officer", "foia_email": "usn.ncr.dns.mbx.don-foia-pa@us.navy.mil", "foia_address": "Chief of Naval Operations, DNS-36\n2000 Navy Pentagon\nWashington\nDC\n20350-2000", "foia_phone": "703-697-0031", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "U.S. Marine Corps", "abbreviation": "USMC", "foia_officer_title": "FOIA Officer", "foia_email": "hqmcfoia@usmc.mil", "foia_address": "FOIA/PA, Headquarters US Marine Corps (ARSF), FOIA/PA Section (ARSF)\n3000 Marine Corps Pentagon\nWashington\nDC\n20350-3000", "foia_phone": "703-614-4008", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Armed Services Board of Contract Appeals", "abbreviation": "ASBCA", "foia_officer_title": "FOIA Officer", "foia_email": "asbca.foia@mail.mil", "foia_address": "Skyline 6, Room 703\n5109 Leesburg Pike\nFalls Church\nVA\n22041-3208", "foia_phone": "703-681-8501", "foia_fax": "", "response_days": 20, "portal_url": "http://www.asbca.mil/"},
    {"name": "Defense Commissary Agency", "abbreviation": "DeCA", "foia_officer_title": "FOIA Officer", "foia_email": "deca.gregg-adams.hq.mbx.foia@mail.mil", "foia_address": "Freedom of Information Officer\n1300 E Avenue\nFort Gregg-Adams\nVA\n23801-1800", "foia_phone": "804-734-8000 ext. 48982", "foia_fax": "", "response_days": 20, "portal_url": "https://corp.commissaries.com/our-agency/FOIA/FOIA-Electronic-Reading-Room"},
    {"name": "Defense Contract Audit Agency", "abbreviation": "DCAA", "foia_officer_title": "FOIA Officer", "foia_email": "dcaa.belvoir.hq.mbx.hq-foia@mail.mil", "foia_address": "FOIA Service Center, Suite 2135\n8725 John J. Kingman Road\nFort Belvoir\nVA\n22060-6219", "foia_phone": "571-448-3153", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dcaa.mil/Freedom-of-Information-Act/FOIA-Reading-Room/"},
    {"name": "Defense Contract Management Agency", "abbreviation": "DCMA", "foia_officer_title": "FOIA Officer", "foia_email": "dcma.gregg-adams.hq.mbx.dcma-foia@mail.mil", "foia_address": "DCMA-DCC, Attn:  FOIA Officer\n3901 A Avenue\nFort Gregg-Adams\nVA\n23801", "foia_phone": "804-609-4533", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dcma.mil/HQ/FOIA1/howto/"},
    {"name": "Defense Finance and Accounting Service", "abbreviation": "DFAS", "foia_officer_title": "FOIA Officer", "foia_email": "dfas.foia@mail.mil", "foia_address": "Indianapolis FOIA/PA Program Manager\n8899 East 56th Street\nIndianapolis\nIN\n46249-0150", "foia_phone": "317-212-4591", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dfas.mil/foia/foiareadingroom.html"},
    {"name": "Defense Information Systems Agency", "abbreviation": "DISA", "foia_officer_title": "FOIA Officer", "foia_email": "disa.meade.eiic.mbx.cio-foia@mail.mil", "foia_address": "Chief Information Office A3E23 \n6910 Cooper Ave. \nFt. Meade\nMD\n20755-0549", "foia_phone": "301-225-6104", "foia_fax": "", "response_days": 20, "portal_url": "http://www.disa.mil/About/Legal-and-Regulatory/Freedom-of-Info-Act-Program"},
    {"name": "Defense Intelligence Agency", "abbreviation": "DIA", "foia_officer_title": "FOIA Officer", "foia_email": "foia1@dodiis.mil", "foia_address": "ATTN: IMO-2 (FOIA)\n7400 Pentagon\nWashington\nDC\n20301-7400", "foia_phone": "301-394-6253", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/"},
    {"name": "Defense Logistics Agency", "abbreviation": "DLA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Information Governance & Compliance/Attn: FOIA/Privacy\n6090 Strathmore Road\nRichmond\nVA\n23237", "foia_phone": "445-737-7072", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dla.mil/FOIA/Electronic-Reading-Room/"},
    {"name": "Defense Counterintelligence and Security Agency", "abbreviation": "DCSA", "foia_officer_title": "FOIA Officer", "foia_email": "dcsa.quantico.dcsa-hq.mbx.foia@mail.mil", "foia_address": "Office of FOIA and Privacy\n27130 Telegraph Road\nQuantico\nVA\n22314", "foia_phone": "(878) 274-1185", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dcsa.mil/contact/foia/foip/"},
    {"name": "Defense Threat Reduction Agency", "abbreviation": "DTRA", "foia_officer_title": "FOIA Officer", "foia_email": "dtrafoiaprivacy@mail.mil", "foia_address": "ATTN: IT-FOIA/PA\n8725 John J. Kingman Road\nFort Belvoir\nVA\n22060-6201", "foia_phone": "703-767-1792", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dtra.mil/About/Mission/Freedom-of-Information-Act-and-Privacy-Act/FOIA-Reading-Room/"},
    {"name": "Defense Technical Information Center", "abbreviation": "DTIC", "foia_officer_title": "FOIA Officer", "foia_email": "dtic.belvoir.rm.mbx.foia@mail.mil", "foia_address": "DTIC-R  ATTN: FOIA Requester Service Center\n8725 John J. Kingman Road\nFort Belvoir\nVA\n22060-6218", "foia_phone": "571-448-9702", "foia_fax": "", "response_days": 20, "portal_url": "https://discover.dtic.mil/foia/"},
    {"name": "Department of Defense Education Activity", "abbreviation": "DoDEA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@dodea.edu", "foia_address": "FOIA Requester Service Center\n4800 Mark Center Drive\nAlexandria\nVA\n22350-1400", "foia_phone": "571-372-1890", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dodea.edu/offices/executive-services/freedom-information-act-foia"},
    {"name": "National Geospatial-Intelligence Agency", "abbreviation": "NGA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIANGA@nga.mil", "foia_address": "7500 GEOINT Drive\nMail Stop N22-SISM\nSpringfield\nVA\n22150", "foia_phone": "(703) 601 6799", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nga.mil/About/Pages/FOIA.aspx"},
    {"name": "National Guard Bureau", "abbreviation": "NGB", "foia_officer_title": "FOIA Officer", "foia_email": "ngb.foia@army.mil", "foia_address": "Office of Information and Privacy (NGB/JA-OIP)\n111 South George Mason Drive, AH2\nArlington\nVA\n22204-1373", "foia_phone": "844-573-2939", "foia_fax": "", "response_days": 20, "portal_url": "http://www.nationalguard.mil/Resources/FOIA/Reading-Room/"},
    {"name": "National Reconnaissance Office", "abbreviation": "NRO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "ATTN: COMM/Information Review and Release Group\n14675 Lee Road\nChantilly\nVA\n20151-1715", "foia_phone": "703-745-0600", "foia_fax": "", "response_days": 20, "portal_url": "http://www.nro.gov/foia/RequestInput.aspx"},
    {"name": "National Security Agency", "abbreviation": "NSA", "foia_officer_title": "FOIA Officer", "foia_email": "foiarsc@nsa.gov", "foia_address": "9800 Savage Rd.\nSuite 6932\nFort Meade\nMD\n20755-6932", "foia_phone": "301-688-6527", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nsa.gov/about/contact-us/Submit-a-FOIA-Request/"},
    {"name": "Office of Inspector General", "abbreviation": "DoD IG", "foia_officer_title": "FOIA Officer", "foia_email": "foiarequests@dodig.mil", "foia_address": "DoD Office of Inspector General/FOIA Requester Service Center\n4800 Mark Center Drive, Suite 10B24\nAlexandria\nVA\n22350-1500", "foia_phone": "703-604-9775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Defense Health Agency", "abbreviation": "DHA", "foia_officer_title": "FOIA Officer", "foia_email": "dha.foia@health.mil", "foia_address": "DHA FOIA Requester Service Center\n7700 Arlington Blvd, Suite 5101\nFalls Church\nVA\n22042-5101", "foia_phone": "703-681-3310", "foia_fax": "", "response_days": 20, "portal_url": "https://health.mil/Military-Health-Topics/Privacy-and-Civil-Liberties/FOIA/FOIA-Request-Form"},
    {"name": "Joint Personnel Recovery Agency", "abbreviation": "JPRA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA Requester Service Center\n10244 Burbeck Road\nFort Belvoir\nVA\n22060-5805", "foia_phone": "703-704-0637", "foia_fax": "", "response_days": 20, "portal_url": "https://www.jpra.mil/links/FOIA/RFI.html"},
    {"name": "U.S. Africa Command", "abbreviation": "AFRICOM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "HQ U.S. Africa Command, FOIA Requester Service Center\nUnit 29951\nAPO\nAE\n09751", "foia_phone": "+490 711-729-2731", "foia_fax": "", "response_days": 20, "portal_url": "https://www.africom.mil/foia-freedom-of-information-act-requests"},
    {"name": "U.S. Central Command", "abbreviation": "CENTCOM", "foia_officer_title": "FOIA Officer", "foia_email": "centcom.macdill.centcom-hq.mbx.freedom-of-information-act@mail.mil", "foia_address": "United States Central Command J6-8-F (FOIA/PA)\n7115 South Boundary Boulevard\nMacDill AFB\nFL\n33621-5101", "foia_phone": "813-529-6285", "foia_fax": "", "response_days": 20, "portal_url": "https://www3.centcom.mil/foiarequests/Default.aspx"},
    {"name": "U.S. European Command", "abbreviation": "EUCOM", "foia_officer_title": "FOIA Officer", "foia_email": "eucom.stuttgart.ecj1.list.foia-privact-request-mb-access@mail.mil", "foia_address": "FOIA Requester Service Center\nUnit 30400\nAPO\nAE\n09131", "foia_phone": "011.49.711.7080.1328", "foia_fax": "", "response_days": 20, "portal_url": "https://www.eucom.mil/foia-request"},
    {"name": "U.S. Northern Command", "abbreviation": "NORTHCOM", "foia_officer_title": "FOIA Officer", "foia_email": "n-nc.peterson.n-ncspecialstaff.mbx.cska-foia-omb@mail.mil", "foia_address": "USNORTHCOM FOIA Requester Service Center\n250 Vandenberg Street Suite B016\nPeterson AFB\nCO\n80914-3804", "foia_phone": "719-556-2117", "foia_fax": "", "response_days": 20, "portal_url": "https://www.northcom.mil/FOIA/"},
    {"name": "U.S. Indo-Pacific Command", "abbreviation": "INDO-PACOM", "foia_officer_title": "FOIA Officer", "foia_email": "indopacom.hmsmith.pcj6.mbx.foia@us.navy.mil", "foia_address": "USINDOPACOM, ATTN: J06 FOIA\nBOX 64022\nCamp Smith\nHI\n96861-4022", "foia_phone": "808-477-7774", "foia_fax": "", "response_days": 20, "portal_url": "http://www.pacom.mil/Media/Freedom-of-Information-Act/FOIA-Reading-Room/"},
    {"name": "U.S. Southern Command", "abbreviation": "SOUTHCOM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Attn:  SCSJA-FOIA\n9301 NW 33rd St.\nDoral\nFL\n33172", "foia_phone": "305-437-1108", "foia_fax": "", "response_days": 20, "portal_url": "http://www.southcom.mil/FOIA/"},
    {"name": "U.S. Special Operations Command", "abbreviation": "SOCOM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "HQ USSOCOM - SOCS-SJS-F/FOIA Requester Service Center\n7701 Tampa Point Boulevard\nMacDill AFB\nFL\n33621-5323", "foia_phone": "813-826-3296", "foia_fax": "", "response_days": 20, "portal_url": "https://www.socom.mil/FOIA/Pages/E-FOIARequests.aspx"},
    {"name": "U.S. Strategic Command", "abbreviation": "STRATCOM", "foia_officer_title": "FOIA Officer", "foia_email": "stratcom.offutt.j0.mbx.stratcom-foia@mail.mil", "foia_address": "USSTRATCOM/J010 (FOIA)\n900 SAC BLVD STE N3.101\nOffutt AFB\nNE\n68113", "foia_phone": "(402)912-0178", "foia_fax": "", "response_days": 20, "portal_url": "http://www.stratcom.mil/Contact/Freedom-of-Information-Act/Reading-Room/"},
    {"name": "U.S. Transportation Command", "abbreviation": "TRANSCOM", "foia_officer_title": "FOIA Officer", "foia_email": "transcom.scott.tcja.mbx.foia@mail.mil", "foia_address": "USTRANSCOM/TCJA-FO\n508 Scott Drive\nScott AFB\nIL\n62225-5357", "foia_phone": "618-817-4100", "foia_fax": "", "response_days": 20, "portal_url": "https://www.ustranscom.mil/foia/request-form.cfm"},
    {"name": "U.S. Election Assistance Commission", "abbreviation": "EAC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "633 3rd Street NW \nSuite 200\nWashington \nDC\n20001", "foia_phone": "301-563-3957", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Department of Education", "abbreviation": "DoED", "foia_officer_title": "FOIA Officer", "foia_email": "EDFOIAManager@ed.gov", "foia_address": "FOIA Service Center, Office of the Deputy Secretary\n400 Maryland Avenue, SW\nWashington\nDC\n20202-4536", "foia_phone": "202-401-8365", "foia_fax": "", "response_days": 20, "portal_url": "https://doed.secureocp.com/App/AssertionConsumerService.aspx"},
    {"name": "Office of Legal Counsel/Headquarters (FOIA Division)", "abbreviation": "OLC/HDQ", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@eeoc.gov", "foia_address": "Michael L. Heise, Assistant Legal Counsel, FOIA Public Liaison\n131 M Street NE, Suite 5NW02E\nWashington\nDC\n20507", "foia_phone": "202-921-2542", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Atlanta District Office (Savannah Local Office)", "abbreviation": "Atlanta", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Marcia Haymer, Records Disclosure Coordinator\n100 Alabama Street, SW\nAtlanta\nGA\n30303", "foia_phone": "404-562-6909", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Birmingham District Office (Mobile Local Office; Jackson Area Office)", "abbreviation": "Birmingham", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Alicia Vickers, Records Disclosure Coordinator\n1130 22nd Street, Ridge Park Place, Suite 2000\nBirmingham\nAL\n35205", "foia_phone": "205-651-7024", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Chicago District Office (Milwaukee and Minneapolis Area Offices)", "abbreviation": "Chicago", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Shernice Pierce, Records Disclosure Coordinator\nJCK Federal Bldg., 230 S. Dearborn Street\nChicago\nIL\n60604", "foia_phone": "414-662-3707", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Dallas District Office (San Antonio Field Office; El Paso Area Office)", "abbreviation": "Dallas", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Rasheedah McDonald & Nadetia Williams, Records Disclosure Coordinators\n207 S. Houston Street, 3rd Floor\nDallas\nTX\n75202-4726", "foia_phone": "972-918-3617", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Houston District Office (New Orleans Field Office)", "abbreviation": "Houston", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Yvette Stewart, Records Disclosure Coordinator\nMickey Leland Federal Building, 1919 Smith Street, 7th Floor\nHouston\nTX\n77002", "foia_phone": "346-327-7705", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Indianapolis District Office (Detroit Field Office; Cincinnati and Louisville Area Offices)", "abbreviation": "Indianapolis", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Donna E. Williams, Records Disclosure Coordinator\n115 W. Washington Street South, Suite 600\nIndianapolis\nIN\n46204-4203", "foia_phone": "463-999-1139", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Los Angeles District Office (Fresno, Honolulu, Las Vegas, and San Diego Local Offices)", "abbreviation": "Los Angeles", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kathleen Gilliam, Records Disclosure Coordinator\n255 E. Temple Street, 4th Floor\nLos Angeles\nCA\n90012", "foia_phone": "213-785-3078", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Memphis District Office (Little Rock and Nashville Area Offices)", "abbreviation": "Memphis", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Mark Adams, Records Disclosure Coordinator\n1407 Union Avenue, 9th Floor\nMemphis\nTN\n38104", "foia_phone": "901-685-4648", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Miami District Office (Tampa Field Office and San Juan Local Office)", "abbreviation": "Miami", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Fredricka Warren & Alicia Powell, Records Disclosure Coordinators\n100 SE Street, Suite 1500\nMiami\nFL\n33131", "foia_phone": "786-648-5822", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "New York District Office (Boston and Newark Area Offices; Buffalo Local Office)", "abbreviation": "New York", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Shynee Eason, Records Disclosure Coordinator\n33 Whitehall Street, 5th Floor\nNew York\nNY\n10004", "foia_phone": "929-506-5272", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Philadelphia District Office (Baltimore and Cleveland Field Offices; Pittsburgh Area Office)", "abbreviation": "Philadelphia", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Bridget Lange & Lori Becker, Records Disclosure Coordinators, Suite 1000\n801 Market Street\nPhiladelphia\nPA\n19107-3127", "foia_phone": "267-589-9753", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Phoenix District Office (Denver Field Office; Albuquerque Area Office)", "abbreviation": "Phoenix", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Silvia Hart, Records Disclosure Coordinator\n3300 N. Central Avenue, Suite 690\nPhoenix\nAZ\n85012-2504", "foia_phone": "602-661-0048", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "San Francisco District Office (Seattle Field Office; Oakland and San Jose Local Offices)", "abbreviation": "San Francisco", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Vincent Clark, Records Disclosure Coordinator\n450 Golden Gate Avenue, 5 West, P.O. Box 36025\nSan Francisco\nCA\n94102-3661", "foia_phone": "650-684-0944", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "St. Louis District Office (Kansas City and Oklahoma Area Offices)", "abbreviation": "St. Louis", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Christopher Jenkins, Records Disclosure Coordinator\n1222 Spruce Street, Room 8100\nSt. Louis\nMO\n63103", "foia_phone": "314-798-1901", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Washington Field Office", "abbreviation": "WFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Cheri Allen, Records Disclosure Coordinator\n131 M Street, NE, Suite 4NW02F\nWashington\nDC\n20507", "foia_phone": "202-921-2773", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Region 1 (States: CT, ME, MA, NH, RI, VT)", "abbreviation": "Region 01", "foia_officer_title": "FOIA Officer", "foia_email": "foia_hq@epa.gov", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "2025662162", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 2 (States: NJ, NY, PR USVI)", "abbreviation": "Region 02", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 3 (States: DE, DC, MD, PA, VA, WV, 7 federally recognized Tribes)", "abbreviation": "Region 03", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 4 (States: AL, FL, GA, KY, MS, NC, SC, TN)", "abbreviation": "Region 04", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 5 (States: IL, IN, MI, MN, OH, WI)", "abbreviation": "Region 05", "foia_officer_title": "FOIA Officer", "foia_email": "foia_hq@epa.gov", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "2025662162", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 6 (States: AR, LA, NM, OK, TX)", "abbreviation": "Region 06", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Freedom of Information Act Office,U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 7 (States: IA, KS, MO, NE)", "abbreviation": "Region 07", "foia_officer_title": "FOIA Officer", "foia_email": "foia_hq@epa.gov", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agen\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 8 (States: CO, MT, ND, SD, UT, WY)", "abbreviation": "Region 08", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Regional Freedom of Information Officer, U.S. EPA, Region 8\n1595 Wynkoop Street\nDenver\nCO\n80202-1129", "foia_phone": "303-312-6306", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 9 (States: AZ, CA, HI, NV, AS, GU)", "abbreviation": "Region 09", "foia_officer_title": "FOIA Officer", "foia_email": "foia_hq@epa.gov", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "2025662162", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Region 10 (States: AK, ID, OR, WA, 271 federally recognized Tribes)", "abbreviation": "Region 10", "foia_officer_title": "FOIA Officer", "foia_email": "foia_hq@epa.gov", "foia_address": "National Freedom of Information Act Office, U.S. Environmental Protection Agency\n1200 Pennsylvania Ave, NW, Suite 5315, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Headquarters", "abbreviation": "HQ", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@exim.gov", "foia_address": "Lennell C. Jackson, FOIA Contact\n811 Vermont Avenue, NW\nWashington\nDC\n20571", "foia_phone": "202-565-3290", "foia_fax": "", "response_days": 20, "portal_url": "http://www.exim.gov/about/foia/foia-request-form"},
    {"name": "Farm Credit Administration", "abbreviation": "FCA", "foia_officer_title": "FOIA Officer", "foia_email": "FCA_FOIAOfficer@fca.gov", "foia_address": "1501 Farm Credit Drive\nMcLean\nVA\n22102-5090", "foia_phone": "703-883-4082", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fca.gov/home/FOIA_index.html"},
    {"name": "Federal Communications Commission", "abbreviation": "FCC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Cathy Williams, Associate Managing Director, Performance and Program Management \n45 L Street NE; Room 3.303\nWashington\nDC\n20554", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://fcc.arkcase.com/foia"},
    {"name": "Farm Credit System Insurance Corporation", "abbreviation": "FCSIC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Ward Griffin, Chief FOIA Officer \n1501 Farm Credit Drive\nMcLean\nVA\n22102-5090", "foia_phone": "703-883-4082", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fcsic.gov/notices/index/foia-reading-room"},
    {"name": "Federal Deposit Insurance Corporation", "abbreviation": "FDIC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA Contact, Legal Division, FOIA/PA Group\n550 17th Street, NW\nWashington\nDC\n20429", "foia_phone": "202-898-7021", "foia_fax": "", "response_days": 20, "portal_url": "https://efoiarequest.fdic.gov/palMain.aspx?protocol=https"},
    {"name": "Federal Election Commission", "abbreviation": "FEC", "foia_officer_title": "FOIA Officer", "foia_email": "foia@fec.gov", "foia_address": "FOIA Contact, FOIA Requester Service Center\n1050 First Street, NE\nWashington\nDC\n20463", "foia_phone": "202-694-1100", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fec.gov/freedom-information-act/#foia-documents-and-the-e-foia-reading-room"},
    {"name": "Federal Energy Regulatory Commission", "abbreviation": "FERC", "foia_officer_title": "FOIA Officer", "foia_email": "foia@ferc.gov", "foia_address": "Benjamin Williams, Director, Office of External Affairs\n888 First Street NE\nWashington\nDC\n20426", "foia_phone": "202-502-6088", "foia_fax": "", "response_days": 20, "portal_url": "https://www.ferc.gov/enforcement-legal/foia/electronic-foia-privacy-act-request-form"},
    {"name": "Federal Financial Institutions Examination Council", "abbreviation": "FFIEC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Judith E. Dupre, Executive Secretary, E-2035-C\n3501 Fairfax Drive\nArlington\nVA\n22226-3550", "foia_phone": "703-516-5590", "foia_fax": "", "response_days": 20, "portal_url": "https://www.ffiec.gov/foia.htm"},
    {"name": "Headquarters", "abbreviation": "FHFA-HQ", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@fhfa.gov", "foia_address": "Constitution Center\n400 7th Street, S.W. 4th Floor\nWashington\nDC\n20024", "foia_phone": "202-649-3803", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fhfa.gov/AboutUs/FOIAPrivacy/Pages/Reading-Room.aspx"},
    {"name": "Office of Inspector General", "abbreviation": "FHFA-OIG", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@fhfaoig.gov", "foia_address": "Attn: FHFA OIG FOIA Office\nConstitution Center, 400 7th Street, SW\nWashington\nDC\n20024", "foia_phone": "202-730-0399", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fhfaoig.gov/FOIA/ReadingRoom"},
    {"name": "The Authority", "abbreviation": "Authority", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "FLRA Office of the Solicitor\n1400 K Street, NW\nWashington\nDC\n20424", "foia_phone": "771-444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "Office of the General Counsel", "abbreviation": "OGC", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "Office of the Solicitor, Federal Labor Relations Authority\n1400 K Street, NW\nWashington\nDC\n20424", "foia_phone": "771-444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "Atlanta Regional Office", "abbreviation": "ATRO", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "Office of the Solicitor, Federal Labor Relations Authority\n1400 K Street NW\nWashington\nDC\n20424", "foia_phone": "771-444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "Chicago Regional Office", "abbreviation": "CHRO", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "Office of the Solicitor, Federal Labor Relations Authority\n1400 K Street NW\nWashington\nDC\n20424", "foia_phone": "771-444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "Denver Regional Office", "abbreviation": "DERO", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "Office of the Solicitor, Federal Labor Relations Authority\n1400 K Street, NW\nWashington\nDC\n20424", "foia_phone": "771-444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "San Francisco Regional Office", "abbreviation": "SFRO", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "Office of the Solicitor, Federal Labor Relations Authority\n1400 K Street, NW\nWashington\nDC\n20424", "foia_phone": "771-444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "Washington Regional Office", "abbreviation": "WARO", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "Office of the Solicitor, Federal Labor Relations Authority\n1400 K Street, NW\nWashington\nDC\n20424-0001", "foia_phone": "771-444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "The Federal Service Impasses Panel", "abbreviation": "FSIP", "foia_officer_title": "FOIA Officer", "foia_email": "solmail@flra.gov", "foia_address": "Office of the Solicitor, Federal Labor Relations Authority\n1400 K Street, NW\nWashington\nDC\n20424", "foia_phone": "(771) 444-5775", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "Office of the Inspector General", "abbreviation": "IG", "foia_officer_title": "FOIA Officer", "foia_email": "oigmail@flra.gov", "foia_address": "FLRA Office of the Inspector General, FOIA Contact\n1400 K Street, NW\nWashington\nDC\n20424", "foia_phone": "771-444-5712", "foia_fax": "", "response_days": 20, "portal_url": "https://www.flra.gov/elibrary"},
    {"name": "Federal Maritime Commission", "abbreviation": "FMC", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@FMC.gov", "foia_address": "Secretary, Federal Maritime Commission\n800 North Capitol Street, NW\nWashington\nDC\n20573", "foia_phone": "202-523-5725", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fmc.gov/about-the-fmc/freedom-of-information-act-requests-reports/"},
    {"name": "Federal Mediation and Conciliation Service", "abbreviation": "FMCS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Anna Davis, Chief FOIA Officer\n250 E Street, SW\nWashington\nDC\n20427", "foia_phone": "202-606-3737", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fmcs.gov/resources/forms-applications/general-foia-request-form/"},
    {"name": "Federal Mine Safety and Health Review Commission", "abbreviation": "FMSHRC", "foia_officer_title": "FOIA Officer", "foia_email": "sgeorge@fmshrc.gov", "foia_address": "Chief FOIA Officer\n1331 Pennsylvania, NW, Suite 520N\nWashington\nDC\n20004-1710", "foia_phone": "2024349935", "foia_fax": "", "response_days": 20, "portal_url": "http://www.fmshrc.gov/foia/foia-request-form"},
    {"name": "Federal Open Market Committee", "abbreviation": "FOMC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "20th Street and Constitution Avenue NW\nWashington\nDC\n20551", "foia_phone": "202-973-7400", "foia_fax": "", "response_days": 20, "portal_url": "https://fomc.secureocp.com/"},
    {"name": "Federal Reserve System", "abbreviation": "FRB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Federal Reserve System\n2001 C Street, NW\nWashington\nDC\n20551", "foia_phone": "202-452-3684", "foia_fax": "", "response_days": 20, "portal_url": "https://www.federalreserve.gov/secure/forms/efoiaform.aspx"},
    {"name": "Federal Retirement Thrift Investment Board", "abbreviation": "FRTIB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Amanda Haas, FOIA Officer, 10th Floor\n77 K Street, NE\nWashington\nDC\n20002", "foia_phone": "202-942-1660", "foia_fax": "", "response_days": 20, "portal_url": "https://www.frtib.gov/ReadingRoom/index.html"},
    {"name": "Federal Trade Commission", "abbreviation": "FTC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Freedom of Information Act Request Office of General Counsel\n600 Pennsylvania Avenue, NW H-585\nWashington\nDC\n20580", "foia_phone": "202-326-2735", "foia_fax": "", "response_days": 20, "portal_url": "https://www.ftc.gov/foia/request-form"},
    {"name": "Gulf Coast Ecosystem Restoration Council", "abbreviation": "GCERC", "foia_officer_title": "FOIA Officer", "foia_email": "Lisa.Robertson@restorethegulf.gov", "foia_address": "Lisa Robertson, FOIA Officer, Suite 1117\n500 Poydras Street\nNew Orleans\nLA\n70130", "foia_phone": "504-884-7685", "foia_fax": "", "response_days": 20, "portal_url": "https://www.restorethegulf.gov/resources/council-documents-foia-library"},
    {"name": "Headquarters", "abbreviation": "GSA-Main", "foia_officer_title": "FOIA Officer", "foia_email": "gsa.foia@gsa.gov", "foia_address": "FOIA Contact, FOIA Requester Service Center (LG)\n1800 F. Street, NW\nWashington\nDC\n20405", "foia_phone": "202-219-3078", "foia_fax": "", "response_days": 20, "portal_url": "https://securefoia.gsa.gov"},
    {"name": "Office of the Secretary", "abbreviation": "OS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIARequest@hhs.gov", "foia_address": "FOIA Officer/Director, Freedom of Information and Privacy Acts Division, Hubert H. Humphrey Bldg, Suite 729H\n200 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-690-7453", "foia_fax": "", "response_days": 20, "portal_url": "https://requests.publiclink.hhs.gov/palMain.aspx"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Stefanie Jewett, FOIA Officer, Wilbur J. Cohen Building, Room 5527\n330 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-619-2541", "foia_fax": "", "response_days": 20, "portal_url": "https://foia.pal.oig.hhs.gov/app/Home.aspx"},
    {"name": "Administration for Community Living", "abbreviation": "ACL", "foia_officer_title": "FOIA Officer", "foia_email": "FOIARequest@hhs.gov", "foia_address": "Hubert H. Humphrey Building, Room 729H\n200 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-795-7377", "foia_fax": "", "response_days": 20, "portal_url": "https://www.acl.gov/node/447#Eroom"},
    {"name": "Administration for Children and Families", "abbreviation": "ACF", "foia_officer_title": "FOIA Officer", "foia_email": "FOIARequest@hhs.gov", "foia_address": "Hubert H. Humphrey Building, Room 729H\n200 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-690-7453", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.acf.hhs.gov/App/Home.aspx"},
    {"name": "Agency for Healthcare Research and Quality", "abbreviation": "AHRQ", "foia_officer_title": "FOIA Officer", "foia_email": "FOIArequest@psc.hhs.gov", "foia_address": "Michael S. Marquis, FOIA Officer, Hubert H. Humphrey Building, Suite 729H\n200 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-260-6933", "foia_fax": "", "response_days": 20, "portal_url": "http://wcdapps.hhs.gov/FoiaRequest/"},
    {"name": "Centers for Disease Control and Prevention", "abbreviation": "CDC", "foia_officer_title": "FOIA Officer", "foia_email": "FOIARequest@hhs.gov", "foia_address": "Hubert H. Humphrey Building, Room 729H\n200 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-690-7453", "foia_fax": "", "response_days": 20, "portal_url": "https://foia.cdc.gov/app/Home.aspx"},
    {"name": "Agency for Toxic Substances and Disease Registry", "abbreviation": "ATSDR", "foia_officer_title": "FOIA Officer", "foia_email": "FOIARequests@cdc.gov", "foia_address": "Roger Andoh, FOIA Officer, Building 16, D-54\n1600 Clifton Road, N.E.\nAtlanta\nGA\n30333", "foia_phone": "770-488-6399", "foia_fax": "", "response_days": 20, "portal_url": "http://www.cdc.gov/od/foia/request/index.htm"},
    {"name": "Center for Medicare and Medicaid Services", "abbreviation": "CMS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA_Request@cms.hhs.gov", "foia_address": "Joseph Tripline, FOIA Officer, Central Building Room C5-11-06 \n7500 Security Boulevard\nBaltimore\nMD\n21244", "foia_phone": "410-786-5353", "foia_fax": "", "response_days": 20, "portal_url": "https://www.cms.gov/Regulations-and-Guidance/Legislation/FOIA/rdgroom.html"},
    {"name": "Food and Drug Administration", "abbreviation": "FDA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FDA Division of Headquarters Freedom of Information, Office of the Executive Secretariat, OC \n5630 Fishers Lane, Room 1035\nRockville\nMD\n20857", "foia_phone": "301-796-3900", "foia_fax": "", "response_days": 20, "portal_url": "https://www.accessdata.fda.gov/scripts/foi/foirequest/requestform.cfm"},
    {"name": "Health Resources and Services Administration", "abbreviation": "HRSA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@hrsa.gov", "foia_address": "HRSA FOIA Office, 5600 Fishers Lane, Room 13N32\nRockville\nMD\n20857", "foia_phone": "301-443-1467", "foia_fax": "", "response_days": 20, "portal_url": "https://www.hrsa.gov/foia/electronic-reading.html"},
    {"name": "Indian Health Service", "abbreviation": "IHS", "foia_officer_title": "FOIA Officer", "foia_email": "IHSFOIAMailbox@ihs.gov", "foia_address": "Indian Health Service FOIA Office, Division of Regulatory and Policy Coordination (DRPC)\n5600 Fishers Lane, Mailstop 09E70\nRockville\nMD\n20857", "foia_phone": "301-443-1116", "foia_fax": "", "response_days": 20, "portal_url": "https://www.ihs.gov/foia/foiarequest/foiarequestform/"},
    {"name": "National Institutes of Health", "abbreviation": "NIH", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NIH FOIA Office, Building 31, Room 5B35\n31 Center Drive, MSC 2107\nBethesda\nMD\n20892-2107", "foia_phone": "301-496-5633", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nih.gov/institutes-nih/nih-office-director/office-communications-public-liaison/freedom-information-act-office/nih-foia-library"},
    {"name": "Public Health Service", "abbreviation": "PHS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIArequest@psc.hhs.gov", "foia_address": "FOIA Officer, Hubert H. Humphrey Building, Suite 729H\n200 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-260-6933", "foia_fax": "", "response_days": 20, "portal_url": "https://www.hhs.gov/foia/electronic-reading-room/index.html"},
    {"name": "Substance Abuse and Mental Health Services Administration", "abbreviation": "SAMHSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA Office, 5600 Fishers Lane, 18th Foor\nRockville\nMD\n20857", "foia_phone": "877-726-4727", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Harry S. Truman Scholarship Foundation", "abbreviation": "HSTSF", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Tara Yglesias, FOIA Officer\n712 Jackson Place, NW\nWashington\nDC\n20006", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Headquarters", "abbreviation": "HQ", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Chief, Room 10139\n451 7th Street, SW\nWashington\nDC\n20410-3000", "foia_phone": "202-708-3866", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/"},
    {"name": "Region I (Boston, Hartford, Manchester, Providence)", "abbreviation": "R01", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "10 Causeway Street\nBoston\nMA\n02222", "foia_phone": "(617) 994-8213", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region II (New York, Albany, Buffalo, Newark)", "abbreviation": "R02", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "26 Federal Plaza\nNew York\nNY\n10278-0068", "foia_phone": "212-542-7109", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region III (Philadelphia, Baltimore, Pittsburgh, Richmond, Washington, D.C., Charleston, Wilmington)", "abbreviation": "R03", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Debra Bensala, The Strawbridge Building \n801 Market Street, 2th Floor\nPhiladelphia\nPA\n19107", "foia_phone": "(215) 430-6624", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region IV (Atlanta, Birmingham, Caribbean, Columbia, Greensboro, Jackson, Jacksonville, Knoxville, Louisville, Nashville, Memphis, Miami, Orlando, Tampa)", "abbreviation": "R04", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Tiffany Cobb, Deputy Regional Administrator\n77 Forsyth Street, SW\nAtlanta\nGA\n30303", "foia_phone": "(678) 732-2321", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region V (Chicago, Columbus, Cleveland, Detroit, Indianapolis, Milwaukee, Minneapolis-St. Paul)", "abbreviation": "R05", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kimberly Danna\n77 W. Jackson Boulevard\nChicago\nIL\n60604", "foia_phone": "312-353-5680", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region VI (Texas, Arkansas, Louisiana, New Mexico and Oklahoma)", "abbreviation": "R06", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "307 W 7th Street\nSuite 1000\nFt. Worth\nTX\n76102", "foia_phone": "817-978-5974", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region VII (Kansas City, Des Moines, Omaha, St. Louis)", "abbreviation": "R07", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Fredrick D. Brisco, Regional FOIA Liaison\n400 State Avenue	\nKansas City\nKS\n66101", "foia_phone": "913-551-5462", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region VIII (Denver)", "abbreviation": "R08", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1670 Broadway \n25th Floor\nDenver\nCO\n80202-4801", "foia_phone": "303-672-5456", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region IX (San Francisco, Honolulu, Los Angeles, Phoenix, Sacramento)", "abbreviation": "R09", "foia_officer_title": "FOIA Officer", "foia_email": "Robert.A.Jamieson@hud.gov", "foia_address": "San Francisco Regional Office\nOne Sansome Street, Suite 1200\nSan Francisco\nCA\n94104", "foia_phone": "714-955-0902", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Region X (Seattle, Anchorage, Portland)", "abbreviation": "R0X", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michael Look, FOIA Public Liaison, Suite 200\n909 First Avenue\nSeattle\nWA\n98104-1000", "foia_phone": "(206) 220-5414", "foia_fax": "", "response_days": 20, "portal_url": "https://hudpal.efoia-host.com/palMain.aspx"},
    {"name": "Inter-American Foundation", "abbreviation": "IAF", "foia_officer_title": "FOIA Officer", "foia_email": "foia@iaf.gov", "foia_address": "1331 Pennsylvania Avenue, NW \n3rd Floor South\nWashington\nDC\n20004", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.iaf.gov/foia"},
    {"name": "Institute of Museum and Library Services", "abbreviation": "IMLS", "foia_officer_title": "FOIA Officer", "foia_email": "foia@imls.gov", "foia_address": "955 L'Enfant Plaza North, SW\nSuite 4000\nWashington\nDC\n20024", "foia_phone": "2026534657", "foia_fax": "", "response_days": 20, "portal_url": "https://www.imls.gov/form/foia-request-form"},
    {"name": "James Madison Memorial Fellowship Foundation", "abbreviation": "JMMFF", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Oliver Alwes, FOIA Officer\n1613 Duke Street\nAlexandria\nVA\n22314", "foia_phone": "571-858-4208", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Millennium Challenge Corporation", "abbreviation": "MCC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Susan Gerson, Chief FOIA Officer\n1099 14th St. NW Ste 700\nWashington\nDC\n20005", "foia_phone": "202-521-3600", "foia_fax": "", "response_days": 20, "portal_url": "https://www.mcc.gov/resources/foia/requests"},
    {"name": "Morris K. Udall and Stewart L. Udall Foundation", "abbreviation": "MKUF", "foia_officer_title": "FOIA Officer", "foia_email": "franks@udall.gov", "foia_address": "Morris K. Udall and Stewart L. Udall  Foundation\n434 E. University Blvd., Suite 300\nTucson\nAZ\n85705", "foia_phone": "5203068898", "foia_fax": "", "response_days": 20, "portal_url": "https://udall.gov/AboutUs/FOIA.aspx"},
    {"name": "Marine Mammal Commission", "abbreviation": "MMC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michael Gosliner, FOIA Officer, Suite 700\n4340 East-West Highway\nBethesda\nMD\n20814", "foia_phone": "301-504-0087", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Headquarters", "abbreviation": "MSPB", "foia_officer_title": "FOIA Officer", "foia_email": "foia@mspb.gov", "foia_address": "1615 M Street, NW\nWashington\nDC\n20419", "foia_phone": "202-653-7200", "foia_fax": "", "response_days": 20, "portal_url": "https://www.mspb.gov/FOIA/e-foiareadingroom.htm"},
    {"name": "Office of General Counsel, Operational FOIA and Access Division", "abbreviation": "NGC", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@nara.gov", "foia_address": "National Archives at College Park, Office of the General Counsel, Room 3110\n8601 Adelphi Road\nCollege Park\nMD\n20740", "foia_phone": "301-837-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/home"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "FOIA Contact, Room 2800\n8601 Adelphi Road\nCollege Park\nMD\n20740", "foia_phone": "301-837-3000", "foia_fax": "", "response_days": 20, "portal_url": "https://www.archives.gov/foia/electronic-reading-room"},
    {"name": "Office of Research Services, Special Access and FOIA - Washington, DC Area", "abbreviation": "RF", "foia_officer_title": "FOIA Officer", "foia_email": "specialaccess_FOIA@nara.gov", "foia_address": "Special Access and FOIA Program (RF), Room 5500\n8601 Adelphi Road\nCollege Park\nMD\n20740", "foia_phone": "301-837-3190", "foia_fax": "", "response_days": 20, "portal_url": "https://www.archives.gov/foia/electronic-reading-room"},
    {"name": "National Archives at Atlanta", "abbreviation": "ATL", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archvies at Atlanta\n57800 Jonesboro Road\nMorrow\nGA\n30260", "foia_phone": "770-968-2100", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Boston", "abbreviation": "BOS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Frederick C. Murphy Federal Center\n380 Trapelo Road\nWaltham\nMA\n02452-6399", "foia_phone": "781-663-0130", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Chicago", "abbreviation": "CHI", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives at Chicago\n7358 South Pulaski Road\nChicago\nIL\n60629-5898", "foia_phone": "773-948-9001", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Denver", "abbreviation": "DEN", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives in Denver\n17101 Huron St.\nBroomfield\nCO\n80223", "foia_phone": "303-407-5740", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Fort Worth", "abbreviation": "FW", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives at Ft Worth\n1400 John Burgess Drive\nFort Worth\nTX\n76140", "foia_phone": "817-831-5620", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Kansas City", "abbreviation": "KS-CITY", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archvies at Kansas City\n400 West Pershing Road\nKansas City\nMO\n64108", "foia_phone": "816-268-8000", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at New York City", "abbreviation": "NYC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives at New York\n1 Bowling Green\nNew York\nNY\n10004", "foia_phone": "212-401-1620", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Philadelphia", "abbreviation": "PHIL", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives at Philadelphia\n14700 Townsend Road\nPhiladelphia\nPA\n19154-1096", "foia_phone": "215-305-2044", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Riverside", "abbreviation": "RIV", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives at Riverside\n23123 Cajalco Road\nPerris\nCA\n92570", "foia_phone": "951-956-2000", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at San Francisco", "abbreviation": "SF", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Leo J. Ryan Memorial Federal Bldg\n100 Commodore Drive\nSan Bruno\nCA\n94066-2350", "foia_phone": "650-238-3501", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at Seattle", "abbreviation": "SEA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives at Seattle\n6125 Sand Point Way, NE\nSeattle\nWA\n98115-7999", "foia_phone": "206-336-5115", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives at St. Louis", "abbreviation": "SL", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Archives at St. Louis\n1 Archives Drive\nSt. Louis\nMO\n63138", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Ronald Reagan Presidential Library", "abbreviation": "LP-RR", "foia_officer_title": "FOIA Officer", "foia_email": "reagan.library@nara.gov", "foia_address": "Ronald Reagan Presidential Library & Museum\n40 Presidential Drive\nSimi Valley\nCA\n93065-0699", "foia_phone": "800-410-8354", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "George Bush Presidential Library", "abbreviation": "GBPL", "foia_officer_title": "FOIA Officer", "foia_email": "bush.library@nara.gov", "foia_address": "George Bush Presidential Library and Museum\n1000 George Bush Drive\nWest College Station\nTX\n77845", "foia_phone": "979-691-4000", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "William J. Clinton Presidential Library", "abbreviation": "LP-WJC", "foia_officer_title": "FOIA Officer", "foia_email": "clinton.library@nara.gov", "foia_address": "William J. Clinton Presidential Library & Museum\n1200 President Clinton Avenue\nLittle Rock\nAR\n72201", "foia_phone": "501-374-4242", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "George W. Bush Presidential Library", "abbreviation": "LP-GWB", "foia_officer_title": "FOIA Officer", "foia_email": "gwbush.library@nara.gov", "foia_address": "George W. Bush Presidential Library and Museum\n2943 SMU Blvd\nDallas\nTX\n75205-2300", "foia_phone": "214-346-1650", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Presidential Libraries, Archival Operations Division", "abbreviation": "PA", "foia_officer_title": "FOIA Officer", "foia_email": "presidential.materials@nara.gov", "foia_address": "The National Archives Building, Room G-7\n700 Pennsylvania Avenue, NW\nWashington\nDC\n20408-0001", "foia_phone": "202-357-5200", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Headquarters", "abbreviation": "HQ", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Ames Research Center", "abbreviation": "ARC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Armstrong Flight Research Center", "abbreviation": "AFRC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nS.W. Washington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Glenn Research Center", "abbreviation": "GRC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Goddard Space Flight Center", "abbreviation": "GSFC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Jet Propulsion Laboratory", "abbreviation": "JPL", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "JPL FOIA Office, NASA Management Office\n4800 Oak Grove Drive\nPasadena\nCA\n91109-8001", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Johnson Space Center", "abbreviation": "JSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Kennedy Space Center", "abbreviation": "KSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Langley Research Center", "abbreviation": "LaRC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Marshall Space Flight Center", "abbreviation": "MSFC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "hq-foia-oig@mail.nasa.gov", "foia_address": "OIG FOIA Officer, Room 8V69\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "202-358-2575", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "NASA Shared Services Center", "abbreviation": "NSSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NASA HQ FOIA Office, MS 5-R30\n300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Stennis Space Center", "abbreviation": "SSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "SSC FOIA\nBuilding 1100, Room 304D\nStennis Space Center\nMS\n39529", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.hq.nasa.gov/"},
    {"name": "Principal Agency FOIA Officer", "abbreviation": "Principal Agency FOIA Officer", "foia_officer_title": "FOIA Officer", "foia_email": "hq-foia@mail.nasa.gov", "foia_address": "NASA Headquarters\nRoom 5P 32, 300 E Street, SW\nWashington\nDC\n20546", "foia_phone": "202-358-0625", "foia_fax": "", "response_days": 20, "portal_url": "https://socialforms.nasa.gov/foia"},
    {"name": "National Council on Disability", "abbreviation": "NCD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1331 F St. NW. Ste. 850\nWashington\nDC\n20004", "foia_phone": "202-731-5910", "foia_fax": "", "response_days": 20, "portal_url": "https://foia@ncd.gov"},
    {"name": "National Capital Planning Commission", "abbreviation": "NCPC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Meghan Hottel-Cox, Chief FOIA Officer, North Lobby, Suite 500\n401 9th Street, NW\nWashington\nDC\n20004", "foia_phone": "202-482-7200", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Main Office", "abbreviation": "NCUA - Main", "foia_officer_title": "FOIA Officer", "foia_email": "foia@ncua.gov", "foia_address": "1775 Duke Street\nAlexandria\nVA\n22314", "foia_phone": "703-518-1570", "foia_fax": "", "response_days": 20, "portal_url": "https://ncua.gov/foia/library"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "oigmail@ncua.gov", "foia_address": "Marta Erceg, Counsel to the Inspector General / Assistant IG for Investigations\n1775 Duke Street\nAlexandria\nVA\n22314", "foia_phone": "703-518-6350", "foia_fax": "", "response_days": 20, "portal_url": "https://ncua.gov/foia/library"},
    {"name": "National Endowment for the Arts", "abbreviation": "NEA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@arts.gov", "foia_address": "400 7th Street, SW\nATTN:NEA-OGC/FOIA\nWashington\nDC\n20506", "foia_phone": "202-682-5418", "foia_fax": "", "response_days": 20, "portal_url": "https://www.arts.gov/about/foia/library"},
    {"name": "National Endowment for the Humanities", "abbreviation": "NEH", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Freedom of Information Act Officer\n400 7th Street SW, 4th Floor\nWashington\nDC\n20506", "foia_phone": "202-606-8322", "foia_fax": "", "response_days": 20, "portal_url": "http://www.neh.gov/about/foia/submit-request-online"},
    {"name": "National Indian Gaming Commission", "abbreviation": "NIGC", "foia_officer_title": "FOIA Officer", "foia_email": "foia@nigc.gov", "foia_address": "1849 C Street, NW, MS 1621\nWashington\nDC\n20240", "foia_phone": "202-632-7003", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nigc.gov/freedom-of-information-act-electronic-reading-room/"},
    {"name": "National Labor Relations Board", "abbreviation": "NLRB HQ", "foia_officer_title": "FOIA Officer", "foia_email": "synta.keeling@nlrb.gov", "foia_address": "NLRB FOIA Branch\n1015 Half Street SE, Ste. 4000\nWashington\nDC\n20570", "foia_phone": "(202) 273-2995", "foia_fax": "", "response_days": 20, "portal_url": "https://www.securerelease.us/"},
    {"name": "National Mediation Board", "abbreviation": "NMB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Maria-Kate Dowling,  General Counsel, Suite 250E\n1301 K Street, NW\nWashington\nDC\n20005", "foia_phone": "202-692-5040", "foia_fax": "", "response_days": 20, "portal_url": "http://www.nmb.gov/documents/press-contacts/reading-room-certificate.pdf"},
    {"name": "National Railroad Passenger Corporation (AMTRAK)", "abbreviation": "NRPC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Rebecca Conner, Manager, Records and Information Management\n One  Massachusetts Avenue, NW\nWashington\nDC\n20001", "foia_phone": "202-906-3215", "foia_fax": "", "response_days": 20, "portal_url": "https://www.amtrak.com/about-amtrak/foia.html"},
    {"name": "National Science Foundation", "abbreviation": "NSF", "foia_officer_title": "FOIA Officer", "foia_email": "foia@nsf.gov", "foia_address": "Freedom of Information Act Officer\n2415 Eisenhower Avenue, Suite W 18200\nAlexandria\nVA\n22314", "foia_phone": "703-292-8060", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nsf.gov/publications/ods/"},
    {"name": "National Transportation Safety Board", "abbreviation": "NTSB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Transportation Safety Board\nAttn:  FOIA Requester Service Center, CIO-40  490 L'Enfant Plaza, SW\nWashington\nDC\n20594", "foia_phone": "202-314-6540", "foia_fax": "", "response_days": 20, "portal_url": "https://foiarequest.ntsb.gov/palMain.aspx"},
    {"name": "Neighborhood Reinvestment Corporation", "abbreviation": "NW", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@nw.org", "foia_address": "Neighborhood Reinvestment Corporation c/o Chief FOIA Officer\n1255 Union St, NE, Suite 500\nWashington\nDC\n20002", "foia_phone": "202-733-6989", "foia_fax": "", "response_days": 20, "portal_url": "https://www.neighborworks.org/About-Us/Public-Policy/FOIA/Reading-room"},
    {"name": "Office of the Director of National Intelligence", "abbreviation": "ODNI", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Louis DiNatale, Chief, Information Management Office, Office of the Director of National Intelligence\nWashington\nDC\n20511", "foia_phone": "703-275-1313", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dni.gov/FOIA"},
    {"name": "Office of Government Ethics", "abbreviation": "OGE", "foia_officer_title": "FOIA Officer", "foia_email": "usoge@oge.gov", "foia_address": "Sara Nekou, OGE FOIA Officer\n250 E Street SW, Suite 750\nWashington\nDC\n20024", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://extapps2.oge.gov/FOIAStatus/FOIAResponse.nsf/2FC940AD2A3190BD8525811B004560CE"},
    {"name": "Office of Management and Budget", "abbreviation": "OMB", "foia_officer_title": "FOIA Officer", "foia_email": "OMBFOIA@omb.eop.gov", "foia_address": "Tim Nusraty, FOIA Officer\n725 17th Street NW, Room 9272\nWashington\nDC\n20503", "foia_phone": "202-395-3642", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of National Drug Control Policy", "abbreviation": "ONDCP", "foia_officer_title": "FOIA Officer", "foia_email": "foia@ondcp.eop.gov", "foia_address": "Office of National Drug Control Policy, Office of Legal Counsel\n1800 G Street, 9th Floor\nWashington\nDC\n20503", "foia_phone": "202-395-6622", "foia_fax": "", "response_days": 20, "portal_url": "https://www.foia.gov/agency-search.html?id=5f5caf48-9039-46a7-9715-591d5aa3ac62&type=component"},
    {"name": "Office of Navajo and Hopi Indian Relocation", "abbreviation": "ONHIR", "foia_officer_title": "FOIA Officer", "foia_email": "laruzow@onhir.gov", "foia_address": "P.O. Box KK\n201 E. Birch Avenue\nFlagstaff\nAZ\n86002", "foia_phone": "9287792721114", "foia_fax": "", "response_days": 20, "portal_url": "https://www.onhir.gov"},
    {"name": "Overseas Private Investment Corporation", "abbreviation": "OPIC", "foia_officer_title": "FOIA Officer", "foia_email": "foia@opic.gov", "foia_address": "Nichole Skoyles, FOIA Director\n1100 New York Avenue, NW\nWashington\nDC\n20527", "foia_phone": "202-336-8400", "foia_fax": "", "response_days": 20, "portal_url": "https://www.opic.gov/content/foia-reading-room"},
    {"name": "Office of Personnel Management", "abbreviation": "OPM", "foia_officer_title": "FOIA Officer", "foia_email": "foia@opm.gov", "foia_address": "FOIA Requester Service Center, Room 7H22\n1900 E Street NW\nWashington\nDC\n20415-7900", "foia_phone": "202-606-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://www.opm.gov/information-management/freedom-of-information-act/#url=Electronic-Reading-Room"},
    {"name": "U.S. Office of Special Counsel", "abbreviation": "OSC", "foia_officer_title": "FOIA Officer", "foia_email": "foiarequest@osc.gov", "foia_address": "1730 M Street, NW\nSuite 311\nWashington\nDC\n20036", "foia_phone": "2028047000", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia.osc.gov/"},
    {"name": "Occupational Safety and Health Review Commission", "abbreviation": "OSHRC", "foia_officer_title": "FOIA Officer", "foia_email": "OSHRC_FOIA@oshrc.gov", "foia_address": "FOIA Requester Service Center, 9th Floor\n1120 20th Street, NW\nWashington\nDC\n20036-3457", "foia_phone": "202-606-5724", "foia_fax": "", "response_days": 20, "portal_url": "http://www.oshrc.gov/foia/index.html"},
    {"name": "Office of Science and Technology Policy", "abbreviation": "OSTP", "foia_officer_title": "FOIA Officer", "foia_email": "OSTPFOIA@ostp.eop.gov", "foia_address": "Rachel Wallace, Chief FOIA Officer\n1650 Pennsylvania Avenue, NW\nWashington\nDC\n20502", "foia_phone": "202-456-4444", "foia_fax": "", "response_days": 20, "portal_url": "https://www.whitehouse.gov/ostp/information-resources/"},
    {"name": "Pension Benefit Guaranty Corporation", "abbreviation": "PBGC", "foia_officer_title": "FOIA Officer", "foia_email": "disclosure@pbgc.gov", "foia_address": "Disclosure Division\n445 12th Street SW\nWashington\nDC\n20024", "foia_phone": "(202) 229-4040", "foia_fax": "", "response_days": 20, "portal_url": "https://securefoia.pbgc.gov/"},
    {"name": "Main Office", "abbreviation": "FOIA Office", "foia_officer_title": "FOIA Officer", "foia_email": "foia@peacecorps.gov", "foia_address": "1275 First Street, NE\nWashington\nDC\n20526-0001", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://pc.secureocp.com/"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1275 First Street, NE\nWashington\nDC\n20526-0001", "foia_phone": "202-692-2922", "foia_fax": "", "response_days": 20, "portal_url": "https://cwppc.secureprtportal.com/"},
    {"name": "Privacy and Civil Liberties Oversight Board", "abbreviation": "PCLOB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Privacy & Civil Liberties Oversight Board\n800 North Capitol Street NW\nWashington\nDC\n20002", "foia_phone": "202-331-1986", "foia_fax": "", "response_days": 20, "portal_url": "https://www.pclob.gov/Foia/Foia"},
    {"name": "Postal Regulatory Commission", "abbreviation": "PRC", "foia_officer_title": "FOIA Officer", "foia_email": "foia@prc.gov", "foia_address": "Postal Regulatory Commission, Attn: Chief FOIA Officer\n901 New York Avenue, NW, Suite 200\nWashington\nDC\n20268", "foia_phone": "202-789-6800", "foia_fax": "", "response_days": 20, "portal_url": "http://www.prc.gov/foia/onlinerequest"},
    {"name": "Presidio Trust", "abbreviation": "PT", "foia_officer_title": "FOIA Officer", "foia_email": "foiarequest@presidiotrust.gov", "foia_address": "1750 Lincoln Boulevard\nSan Francisco\nCA\n94129", "foia_phone": "4155615300", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Small Business Administration", "abbreviation": "SBA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@sba.gov", "foia_address": "Oreoluwa Fashola, Branch Chief, Office of FOI/PA\n409 3rd Street, SW\nWashington\nDC\n20416", "foia_phone": "202-401-7793", "foia_fax": "", "response_days": 20, "portal_url": "https://pal.sba.gov/Components/1/Requests/CreateRequest"},
    {"name": "Securities and Exchange Commission", "abbreviation": "SEC", "foia_officer_title": "FOIA Officer", "foia_email": "foiapa@sec.gov", "foia_address": "Office of FOIA Services\n100 F Street, NE, Mail Stop 2465\nWashington\nDC\n20549-2465", "foia_phone": "202-551-7900", "foia_fax": "", "response_days": 20, "portal_url": "https://www.sec.gov/forms/request_public_docs#no-back"},
    {"name": "Special Inspector General for Afghanistan Reconstruction", "abbreviation": "SIGAR", "foia_officer_title": "FOIA Officer", "foia_email": "sigar.pentagon.gen-coun.mbx.foia@mail.mil", "foia_address": "Special Inspector General for Afghanistan, FOIA Office\n2530 Crystal Drive\nArlington\nVA\n22202", "foia_phone": "703-545-6000", "foia_fax": "", "response_days": 20, "portal_url": "https://www.sigar.mil/contact/foia/index.aspx?SSR=9&SubSSR=38&WP=FOIA%20|%20Privacy%20Act"},
    {"name": "Social Security Administration", "abbreviation": "SSA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA.Public.Liaison@ssa.gov", "foia_address": "6401 Security Boulevard\nG401 West High Rise\nBaltimore\nMD\n21235", "foia_phone": "410-965-1727", "foia_fax": "", "response_days": 20, "portal_url": "https://foia.ssa.gov"},
    {"name": "Social Security Advisory Board", "abbreviation": "SSAB", "foia_officer_title": "FOIA Officer", "foia_email": "cheri.cannon@ssab.gov", "foia_address": "400 Virginia Ave SW Ste 625\n400 Virginia Ave SW\nWashington DC\nDC\n20024", "foia_phone": "2029231746", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Selective Service System", "abbreviation": "SSS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@sss.gov", "foia_address": "Selective Service System\nNational Headquarters\nArlington\nVA\n22209-2425", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.sss.gov/foia/foia-electronic-reading-room/"},
    {"name": "Surface Transportation Board", "abbreviation": "STB", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA.Privacy@stb.gov", "foia_address": "Chris Oehrle, FOIA Officer\n395 E Street, SW\nWashington\nDC\n20423-0001", "foia_phone": "202-934-0375", "foia_fax": "", "response_days": 20, "portal_url": "https://www.stb.gov/foia/foia-request/"},
    {"name": "Department of State", "abbreviation": "DOS - Main", "foia_officer_title": "FOIA Officer", "foia_email": "foiarequest@state.gov", "foia_address": "Information Access Programs Directorate\n2201 C Street, NW\nWashington\nDC\n20520", "foia_phone": "202-261-8484", "foia_fax": "", "response_days": 20, "portal_url": "http://foia.state.gov/Request/Submit.aspx"},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "foia@stateoig.gov", "foia_address": "Gina Goldblatt, FOIA Officer\n1700 North Moore Street, Suite 800\nArlington\nVA\n22209", "foia_phone": "571-348-2405", "foia_fax": "", "response_days": 20, "portal_url": "http://foia.state.gov/foiareq/foialetter.asp"},
    {"name": "Tennessee Valley Authority", "abbreviation": "TVA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@tva.gov", "foia_address": "FOIA Requester Service Center\n400 West Summit Hill Drive, 6a\nKnoxville\nTN\n37902-1401", "foia_phone": "865-632-6945", "foia_fax": "", "response_days": 20, "portal_url": "https://foia.tva.gov/"},
    {"name": "Departmental Offices", "abbreviation": "DO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Mark Bittner, Director, FOIA & Transparency\n1500  Pennsylvania Ave., N.W.\nWashington\nDC\n20220", "foia_phone": "202-622-0930", "foia_fax": "", "response_days": 20, "portal_url": "https://home.treasury.gov/footer/freedom-of-information-act/submit-a-request"},
    {"name": "Alcohol and Tobacco Tax and Trade Bureau", "abbreviation": "TTB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Quinton Mason, FOIA Contact, Disclosure Services, Box 12\n1310 G Street, NW\nWashington\nDC\n20005", "foia_phone": "202-882-9904", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Bureau of Engraving and Printing", "abbreviation": "BEP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "14th & C Streets, SW Room 419A\nWashington\nDC\n20228", "foia_phone": "202-874-2500", "foia_fax": "", "response_days": 20, "portal_url": "https://www.bep.gov/footer/foia/bep-foia-reading-room"},
    {"name": "Office of the Comptroller of the Currency", "abbreviation": "OCC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Pauline Byrd, Freedom of Information Act Officer\n400 7th Street, SW\nWashington\nDC\n20219", "foia_phone": "202-649-6700", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.occ.gov/"},
    {"name": "Financial Crimes Enforcement Network", "abbreviation": "FinCEN", "foia_officer_title": "FOIA Officer", "foia_email": "FinCENFOIA@fincen.gov", "foia_address": "Terri Robertson, Disclosure (FOIA) Office\nP.O. Box 39\nVienna\nVA\n22183", "foia_phone": "703-905-5034", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fincen.gov/reading-room-records"},
    {"name": "Bureau of the Fiscal Service", "abbreviation": "Fiscal Service", "foia_officer_title": "FOIA Officer", "foia_email": "foia@fiscal.treasury.gov", "foia_address": "Attn: FOIA Disclosure Office/Room 508B\n3201 Pennsy Drive, Building E.\nLandover \nMD\n20785", "foia_phone": "202-874-5602", "foia_fax": "", "response_days": 20, "portal_url": "https://www.fiscal.treasury.gov/foia-readingroom.html"},
    {"name": "United States Mint", "abbreviation": "MINT", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Lateau Jones, FOIA Contact, 8th Floor\n801 9th Street, NW\nWashington\nDC\n20220", "foia_phone": "202-354-7600", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Treasury Inspector General for Tax Administration", "abbreviation": "TIGTA", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA.Reading.Room@tigta.treas.gov", "foia_address": "Office of Chief Counsel Disclosure Branch\nTreasury Inspector General for Tax Administration (TIGTA) 901 D. Street, SW, Suite 600\nWashington\nDC\n20024", "foia_phone": "202-622-4068", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Internal Revenue Service - Headquarters Office", "abbreviation": "IRS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Internal Revenue Service, GLDS Support Services Stop 211\nP.O. Box 621506\nAtlanta\nGA\n30362-3006", "foia_phone": "312-292-2929", "foia_fax": "", "response_days": 20, "portal_url": "https://www.irs.gov/uac/electronic-reading-room"},
    {"name": "Consumer Product Safety Commission", "abbreviation": "CPSC", "foia_officer_title": "FOIA Officer", "foia_email": "CPSCFOIARequests@cpsc.gov", "foia_address": "U.S. Consumer Product Safety Commission\n4330 East West Highway, Room 704\nBethesda\nMD\n20814-4408", "foia_phone": "1-800-638-2772", "foia_fax": "", "response_days": 20, "portal_url": "https://www.cpsc.gov/Newsroom/FOIA"},
    {"name": "Main Office", "abbreviation": "DOL Main", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Ramona Branch Oliver, Director, Office of Information Services , Office of the Solicitor/MALS Division, Room N-2420\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-5391", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Bureau of International Labor Affairs", "abbreviation": "ILAB", "foia_officer_title": "FOIA Officer", "foia_email": "gaskins.kia@dol.gov", "foia_address": "Kia Gaskins, FOIA Coordinator, Room S-5303\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-4903", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/agencies/ilab"},
    {"name": "Bureau of Labor Statistics", "abbreviation": "BLS", "foia_officer_title": "FOIA Officer", "foia_email": "blsfoiaservicecenter@bls.gov", "foia_address": "Suitland Federal Center Floor 7\n4600 Silver Hill Road\nWashington\nDC\n20212-0002", "foia_phone": "202-691-7628", "foia_fax": "", "response_days": 20, "portal_url": "https://www.bls.gov/bls/blsfoia.htm"},
    {"name": "Employee Benefits Security Administration", "abbreviation": "EBSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Kathy Hoover, FOIA Officer, Public Disclosure Room, Suite N-1513\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-8673", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/dol/foia/readroom.htm"},
    {"name": "Employment & Training Administration", "abbreviation": "ETA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Maria Sanders, FOIA Disclosure Officer\n200 Constitution Avenue, NW\nWashington\nDC\n20210-0002", "foia_phone": "202-693-3101", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Jobs Corps", "abbreviation": "JOB CORP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Maria Sanders, FOIA Disclosure Officer\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-3101", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Mine Safety & Health Administration", "abbreviation": "MSHA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Lanesia Washington, FOIA Officer, Room 5W231\n12th Street South\nArlington\nVA\n22202", "foia_phone": "202-693-9542", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Administrative Law Judges", "abbreviation": "OALJ", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Frank Clubb, FOIA Disclosure Officer, Suite 400 North\n800 K Street\nN.W. Washington\nDC\n20001-8002", "foia_phone": "202-693-7348", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Assistant Secretary for Administration and Management", "abbreviation": "OASAM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Candy March, FOIA Coordinator, Room S-3317\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-7126", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Federal Contract Compliance Programs", "abbreviation": "OFCCP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Bruce Andersen, FOIA Coordinator, USDOL OFCCP, Room C-3325\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-5500", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/ofccp/foia/foiareadingroom/index.html"},
    {"name": "Office of Labor-Management Standards", "abbreviation": "OLMS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Ebony Griffin, FOIA Coordinator, Suite N-5609\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-0112", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Workers' Compensation Programs", "abbreviation": "OWCP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Mia Day, FOIA Coordinator, Suite N-5609\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-0125", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/owcp/energy/regs/compliance/efoia.htm"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA.PrivacyAct@oig.dol.gov", "foia_address": "Michael Coen, Government Information Specialist, Room S-5506\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-5234", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Occupational Safety & Health Administration", "abbreviation": "OSHA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Christopher Durso, FOIA Officer, Room N3647\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-2009", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Veterans' Employment & Training Service", "abbreviation": "VETS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Carrie Timus, National FOIA Officer, Room S-1325\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-4700", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Wage and Hour Division", "abbreviation": "WHD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michael Leiterman, FOIA Coordinator, Room S-3013-F\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-1004", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/general/foia"},
    {"name": "Women's Bureau", "abbreviation": "WB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Danielle Ballard, FOIA Coordinator, Room S-3305\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "202-693-6729", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "United States African Development Foundation", "abbreviation": "USADF", "foia_officer_title": "FOIA Officer", "foia_email": "info@usadf.gov", "foia_address": "1400 I Street NW\nSuite 1000\nWashington\nDC\n20005", "foia_phone": "202-233-8800", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Main Office", "abbreviation": "RRB - Main", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Ana M. Kocur, General Counsel/Chief FOIA Officer\n844 North Rush Street\nChicago\nIL\n60611-1275", "foia_phone": "312-751-4948", "foia_fax": "", "response_days": 20, "portal_url": "https://secure.rrb.gov/efoia/"},
    {"name": "U.S. Access Board", "abbreviation": "USAB", "foia_officer_title": "FOIA Officer", "foia_email": "Foia@access-board.gov", "foia_address": "1331 F Street NW, Suite 1000\nWashington\nDC\n20004", "foia_phone": "202-272-0041", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "United States Agency for International Development", "abbreviation": "USAID", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1300 Pennsylvania Avenue, N.W.\nUSAID Annex, M/MS/IRD, Room 2.4.0A\nWashington\nDC\n20523-2701", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiarequest.usaid.gov/index.aspx"},
    {"name": "Commission on Civil Rights", "abbreviation": "USCCR", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@usccr.gov", "foia_address": "David Ganz, General Counsel, Suite 1150\n1331 Pennsylvania Ave, NW\nWashington\nDC\n20425", "foia_phone": "202-839-7255", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Agricultural Marketing Service", "abbreviation": "AMS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue, SW\nATTN: AMS FOIA Office, Room 2055-S, Stop 0201\nWashington\nDC\n20250-0201", "foia_phone": "(202) 720-2763", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Animal and Plant Health Inspection Service", "abbreviation": "APHIS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "5601 Sunnyside Ave\nAP-740\nBeltsville\nMD\n20705", "foia_phone": "301-851-4102", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Departmental Administration", "abbreviation": "DA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "(202) 690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Farm Service Agency", "abbreviation": "FSA", "foia_officer_title": "FOIA Officer", "foia_email": "SM.FP.FOIA@usda.gov", "foia_address": "1400 Independence Avenue SW\nSouth Building, Room 3203\nWashington\nDC\n20250-9410", "foia_phone": "202-590-6168", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Food and Nutrition Service", "abbreviation": "FNS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA-FNS@usda.gov", "foia_address": "Food & Nutrition Service\n1320 Braddock Place\nAlexandria\nVA\n22314", "foia_phone": "(703) 605-0773", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Food Safety and Inspection Service", "abbreviation": "FSIS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue SW\nSouth Building, Room 1170\nWashington\nDC\n20250", "foia_phone": "(202) 720-2109", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Foreign Agricultural Service", "abbreviation": "FAS", "foia_officer_title": "FOIA Officer", "foia_email": "usdafoia@usda.gov", "foia_address": "1400 Independence Avenue SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Forest Service", "abbreviation": "FS", "foia_officer_title": "FOIA Officer", "foia_email": "SM.FS.WOFOIA@usda.gov", "foia_address": "USDA Forest Service\n201 14th Street SW\nWashington\nDC\n20250", "foia_phone": "(202) 205-1542", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "National Appeals Division", "abbreviation": "NAD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue, SW\nRoom 4104-S\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "National Finance Center", "abbreviation": "NFC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue, SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Natural Resources Conservation Service", "abbreviation": "NRCS", "foia_officer_title": "FOIA Officer", "foia_email": "SM.FP.FOIA@usda.gov", "foia_address": "1400 Independence Avenue, SW\nSouth Building, Room 3203\nWashington\nDC\n20250-9410", "foia_phone": "(202) 590-6168", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Office of Budget & Program Analysis", "abbreviation": "OBPA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Office of the Chief Financial Officer", "abbreviation": "OCFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Office of the Assistant Secretary for Civil Rights", "abbreviation": "OASCR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue, SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Office of the General Counsel", "abbreviation": "OGC", "foia_officer_title": "FOIA Officer", "foia_email": "usdafoia@usda.gov", "foia_address": "1400 Independence Avenue, SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue, SW\nWhitten Building, Room 441-E\nWashington\nDC\n20250", "foia_phone": "(202) 720-8112", "foia_fax": "", "response_days": 20, "portal_url": "https://www.usda.gov/oig/foia"},
    {"name": "Office of the Secretary", "abbreviation": "OSEC/OCIO/DM", "foia_officer_title": "FOIA Officer", "foia_email": "usdafoia@usda.gov", "foia_address": "1400 Independence Avenue, SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "(202) 690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Risk Management Agency", "abbreviation": "RMA", "foia_officer_title": "FOIA Officer", "foia_email": "SM.FP.FOIA@usda.gov", "foia_address": "1400 Independence Avenue, SW\nSouth Building, Room 3203\nWashington\nDC\n20250-9410", "foia_phone": "202-590-6168", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Research, Education, and Economics", "abbreviation": "REE", "foia_officer_title": "FOIA Officer", "foia_email": "usdafoia@usda.gov", "foia_address": "1400 Independence Avenue, SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Rural Development", "abbreviation": "RD", "foia_officer_title": "FOIA Officer", "foia_email": "RD.FOIA@usda.gov", "foia_address": "1400 Independence Avenue, SW\nRoom 6900-S, Stop 0706\nWashington\nDC\n20250", "foia_phone": "(202) 720-2826", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "United States International Boundary and Water Commission", "abbreviation": "USIBWC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Rebecca Rizzuti, FOIA Officer & Public Liaison, FOIA - Legal Office\n4191 N. Mesa\nEl Paso\nTX\n79902", "foia_phone": "915-832-4729", "foia_fax": "", "response_days": 20, "portal_url": "http://www.ibwc.gov/Organization/FOIA.html#How"},
    {"name": "U.S. Interagency Council on Homelessness", "abbreviation": "USICH", "foia_officer_title": "FOIA Officer", "foia_email": "sanya.cade@usich.gov", "foia_address": "Sanya Cade, FOIA Officer, USICH\n301 7th Street, SW, Room 2080\nWashington\nDC\n20407", "foia_phone": "202-594-7880", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "United States Institute of Peace", "abbreviation": "USIP", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA-Request@usip.org", "foia_address": "2301 Constitution Ave., NW\nWashington\nDC\n20037", "foia_phone": "202-457-1700", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "United States International Trade Commission", "abbreviation": "USITC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Lisa R. Barton, Chief FOIA Officer, 112-AA\n500 E Street, SW\nWashington\nDC\n20436", "foia_phone": "202-205-2000", "foia_fax": "", "response_days": 20, "portal_url": "https://pubapps.usitc.gov/applications/foia/request.asp"},
    {"name": "Nuclear Regulatory Commission", "abbreviation": "NRC", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA.Resource@nrc.gov", "foia_address": "FOIA Officer\nMail Stop TWFN-6 A60M\nWashington\nDC\n20555-0001", "foia_phone": "301-415-7169", "foia_fax": "", "response_days": 20, "portal_url": "https://nrc.secureocp.com"},
    {"name": "USPS Headquarters Office", "abbreviation": "USPS HQ", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA12@usps.gov", "foia_address": "USPS Privacy & Records Mngt Office\n475 L'Enfant Plaza, SW, Rm 1P830\nWashington\nDC\n20260-1101", "foia_phone": "202-268-2608", "foia_fax": "", "response_days": 20, "portal_url": "https://about.usps.com/who/legal/foia/make-request.htm"},
    {"name": "USPS Field Office", "abbreviation": "PS", "foia_officer_title": "FOIA Officer", "foia_email": "FOIAPAField@usps.gov", "foia_address": "FOIA Contact, St. Louis General Law Service Center, Room 2400\n1720 Market Street\nSt. Louis\nMO\n63155-9948", "foia_phone": "314-345-5894", "foia_fax": "", "response_days": 20, "portal_url": "https://pfoiapal.usps.com/palMain.aspx"},
    {"name": "Postal Inspection Service", "abbreviation": "PIS", "foia_officer_title": "FOIA Officer", "foia_email": "Foia@uspis.gov", "foia_address": "FOIA Contact, Office of Counsel, Room 3301\n475 L'Enfant Plaza, SW\nWashington\nDC\n20260-2101", "foia_phone": "202-268-7004", "foia_fax": "", "response_days": 20, "portal_url": "https://pfoiapal.usps.com/"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@uspsoig.gov", "foia_address": "USPS-OIG FOIA Office\n1735 N LYNN ST 2FL\nARLINGTON\nVA\n22209-2020", "foia_phone": "703-248-2410", "foia_fax": "", "response_days": 20, "portal_url": "https://www.uspsoig.gov/uspsoig-contact"},
    {"name": "United States Trade and Development Agency", "abbreviation": "USTDA", "foia_officer_title": "FOIA Officer", "foia_email": "foia@ustda.gov", "foia_address": "1101 Wilson Boulevard\nSuite 1100\nArlington\nVA\n22209-3901", "foia_phone": "703-875-4357", "foia_fax": "", "response_days": 20, "portal_url": "https://www.foia.gov/request/agency-component/7ec78e51-723a-4b6d-99ed-46cad80edd25/"},
    {"name": "Office of the United States Trade Representative", "abbreviation": "USTR", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@ustr.eop.gov", "foia_address": "Janice A. Kaye, FOIA Officer, Anacostia Naval Annex Building 410/Door 123\n250 Murray Lane SW\nWashington\nDC\n20509", "foia_phone": "202-881-7849", "foia_fax": "", "response_days": 20, "portal_url": "https://ustr.gov/about-us/reading-room/foia"},
    {"name": "Office of Information and Technology - VA Central Office FOIA Service", "abbreviation": "OIT", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs, (OO5X6D) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-738-2974", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "National Cemetery Administration", "abbreviation": "NCA", "foia_officer_title": "FOIA Officer", "foia_email": "cemncafoia@va.gov", "foia_address": "Department of Veterans Affairs, (43D) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202 948-6706", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Veterans Benefits Administration", "abbreviation": "VBA", "foia_officer_title": "FOIA Officer", "foia_email": "foia.vbaco@va.gov", "foia_address": "Department of Veterans Affairs, (20M3) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-9516", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Veterans Health Administration", "abbreviation": "VHA", "foia_officer_title": "FOIA Officer", "foia_email": "VHAFOIARequests@va.gov", "foia_address": "Department of Veterans Affairs  \nVHA FOIA Office (105HIG), 810 Vermont Ave NW\nWashington\nDC\n20420", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/contacts.aspx"},
    {"name": "Office of the Executive Secretary", "abbreviation": "SEC", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs, 002B VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202 632-5286", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/contacts.aspx"},
    {"name": "Office of Assistant Secretary for Public and Intergovernmental Affairs", "abbreviation": "OASP&IA", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs, (002B) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-7440", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_library.aspx"},
    {"name": "Office of Assistant Secretary for Management", "abbreviation": "MGMT", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs, (004A) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-6991", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_library.aspx"},
    {"name": "Office of Asset Enterprise Management", "abbreviation": "OAEM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Department of Veterans Affairs(044) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-590-8793", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_library.aspx"},
    {"name": "Office of Deputy Assistant Secretary for Budget", "abbreviation": "ODASB", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "ODASB FOIA Contact, (041) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-6631", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Office of Public Affairs", "abbreviation": "OPA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Department of Veterans Affairs (047GD) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-5035", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Corporate Data Center Operations", "abbreviation": "CDCO", "foia_officer_title": "FOIA Officer", "foia_email": "cdcofoia@va.gov", "foia_address": "Department of Veterans Affairs (0050O6)\nPhiladelphia Information Technology Center, 5000 Wissahickon Avenue\nPhiladelphia\nPA\n19144", "foia_phone": "215-842-2000 x 4460", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_library.aspx"},
    {"name": "Office of Assistant Secretary for Information and Technology", "abbreviation": "OIT - Inactive", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiaservice@va.gov", "foia_address": "Department of Veterans Affairs (005R1C) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "877 750-3642", "foia_fax": "", "response_days": 20, "portal_url": "http://www.foia.va.gov/"},
    {"name": "Office of Assistant Secretary for Human Resources and Administration/Operations, Security and Preparedness", "abbreviation": "ASHRAOSP", "foia_officer_title": "FOIA Officer", "foia_email": "Milton.Roberts@va.gov", "foia_address": "Department of Veterans Affairs (006) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "(202) 632-7160", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_reports.aspx"},
    {"name": "Office of Deputy Assistant Secretary for Human Resources Management", "abbreviation": "HRM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Department of Veterans Affairs (054A) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-7863", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_reports.aspx"},
    {"name": "Office of Diversity and Inclusion", "abbreviation": "ODI", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Department of Veterans Affairs, (06) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-4237", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_library.aspx"},
    {"name": "Office of Resolution Management", "abbreviation": "ORM", "foia_officer_title": "FOIA Officer", "foia_email": "ormfoia@va.gov", "foia_address": "Department of Veterans Affairs (08) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "216-390-3607", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Library.asp"},
    {"name": "Office of Deputy Assistant Secretary for Administration", "abbreviation": "ADMIN", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs, (032A) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-5343", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_library.aspx"},
    {"name": "Office of the Assistant Secretary for Operations, Security, and Preparedness", "abbreviation": "OSP", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs, (007) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-4984", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Office of Enterprise Integration", "abbreviation": "OEI", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs (008) VACO, Room 314\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-632-5246", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Library.asp"},
    {"name": "Office of Assistant Secretary for Congressional and Legislative Affairs", "abbreviation": "C&LA", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs (009) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-6459", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Office of Construction & Facilities Management", "abbreviation": "CFM", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs (OOCM2) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-632-4755", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Office of Acquisition and Logistics", "abbreviation": "OALC", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "FOIA Officer, Office of Acquisition and Logistics (OAL), Room 3E.405\n425 I Street, NW\nWashington\nDC\n20001", "foia_phone": "708-786-7804", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Board of Veterans Appeals", "abbreviation": "BVA", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs (01C1) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-382-2906", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Office of the General Counsel", "abbreviation": "GC", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Gregory Draves, FOIA Contact, (026H) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-461-7752", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/foia/"},
    {"name": "Office of Inspector General", "abbreviation": "IG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Department of Veterans Affairs\n810 Vermont Avenue, NW (50CI)\nWashington\nDC\n20420", "foia_phone": "202-461-4631", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Library.asp"},
    {"name": "Office of Small and Disadvantaged Business Utilization", "abbreviation": "OSDBU", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "Department of Veterans Affairs, (00SB) VACO\n810 Vermont Avenue, NW\nWashington\nDC\n20420", "foia_phone": "202-632-8204", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "U.S. Nuclear Waste Technical Review Board", "abbreviation": "NWTRB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "2300 Clarendon Blvd., Suite 1300\nArlington\nVA\n22201", "foia_phone": "703-235-4494", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Legal Affairs", "abbreviation": "OLA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1825 Eye Street NW, Suite 800\nWashington\nDC\n20006", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://lsc.gov/foia-electronic-public-reading-room"},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1825 Eye Street NW, Suite 800\nWashington\nDC\n20006", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://lsc.gov/foia-electronic-public-reading-room"},
    {"name": "Office of the Assistant Secretary for Health", "abbreviation": "OASH", "foia_officer_title": "FOIA Officer", "foia_email": "FOIArequest@psc.hhs.gov", "foia_address": "Hubert H. Humphrey Building, Room 729H\n200 Independence Avenue, SW\nWashington\nDC\n20201", "foia_phone": "202-260-6933", "foia_fax": "", "response_days": 20, "portal_url": "https://www.hhs.gov/foia/electronic-reading-room/index.html"},
    {"name": "Charlotte District Office (Greensboro, Greenville, Norfolk, Richmond Local Offices; Raleigh Area Office)", "abbreviation": "Charlotte", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Francine Michaelson, Records Disclosure Coordinator  \n129 West Trade Street, Suite 400\nCharlotte\nNC\n28202", "foia_phone": "980-296-1260", "foia_fax": "", "response_days": 20, "portal_url": "https://eeoc.arkcase.com/foia/portal/login"},
    {"name": "Select Department of Justice component for me", "abbreviation": "Select", "foia_officer_title": "FOIA Officer", "foia_email": "MRUFOIA.Requests@usdoj.gov", "foia_address": "FOIA/PA Mail Referral Unit\nJustice Management Division, Room 115, LOC Building\nWashington\nDC\n20530", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Department of Commerce (General)", "abbreviation": "DOC-G", "foia_officer_title": "FOIA Officer", "foia_email": "eFOIA@doc.gov", "foia_address": "Office of Privacy and Open Government\n1401 Constitution Avenue NW, Room 61013\nWashington\nDC\n20230", "foia_phone": "202-482-3257", "foia_fax": "", "response_days": 20, "portal_url": "https://foia-pal.commerce.gov"},
    {"name": "Select Equal Employment Opportunity Commission component for me", "abbreviation": "EEOC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Michael L. Heise, Assistant Legal Counsel, FOIA Public Liaison\n131 M Street NE, Suite 5NW02E\nWashington\nDC\n20507", "foia_phone": "(202) 663-4634, (202) 663-7026 (TTY)", "foia_fax": "", "response_days": 20, "portal_url": "https://publicportalfoiapal.eeoc.gov/"},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "foiaoig@nsf.gov", "foia_address": "2415 Eisenhower Avenue\nW16100\nAlexandria\nVA\n22314", "foia_phone": "(703) 292-7100", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nsf.gov/oig/reports/"},
    {"name": "Select Department of Homeland Security component for me", "abbreviation": "DHS-Select component", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "STOP-0655\n2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528-0655", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dhs.gov/make-foia-request"},
    {"name": "Select Department of Transportation component for me", "abbreviation": "DOT-Select component", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Departmental FOIA Office, W94-122\n1200 New Jersey Avenue, SE\nWashington\nDC\n20590", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Select National Archives component for me", "abbreviation": "NARA-select component", "foia_officer_title": "FOIA Officer", "foia_email": "foia@nara.gov", "foia_address": "National Archives at College Park, Room 3110\n8601 Aldephi Road\nCollege Park\nMD\n20740", "foia_phone": "(301) 837-3642", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "VA Records Management Center", "abbreviation": "VA-RMC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Claims Intake Center\nP.O. Box 4444\nJanesville\nWI\n53547", "foia_phone": "608-373-6690", "foia_fax": "", "response_days": 20, "portal_url": "https://www.oprm.va.gov/foia/foia_howTo.aspx"},
    {"name": "Office of Accountability & Whistleblower Protection", "abbreviation": "OAWP", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "810 Vermont Avenue, NW\n(70A) VACO\nWashington\nDC\n20420", "foia_phone": "202-461-4214", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Inspector General", "abbreviation": "J", "foia_officer_title": "FOIA Officer", "foia_email": "OIGFOIA-PrivacyAct@gsaig.gov", "foia_address": "1800 F Street, NW\nWashington\nDC\n20405", "foia_phone": "(202) 501-1932", "foia_fax": "", "response_days": 20, "portal_url": "https://www.gsa.gov/reference/freedom-of-information-act-foia/electronic-reading-room"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "Patricia.Marshall@oig.rrb.gov", "foia_address": "Room 450\n844 North Rush Street\nChicago\nIL\n60611", "foia_phone": "(312) 751-4690", "foia_fax": "", "response_days": 20, "portal_url": "https://www.rrb.gov/Resources/FOIA%20Reading%20Room"},
    {"name": "Select Department of Labor component for me", "abbreviation": "Select DOL", "foia_officer_title": "FOIA Officer", "foia_email": "foiarequest@dol.gov", "foia_address": "Room N-2420\n200 Constitution Avenue, NW\nWashington\nDC\n20210", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/dol/foia/readroom.htm"},
    {"name": "National Personnel Records Center - Military", "abbreviation": "NPRC-M", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1 Archives Drive  \nSt. Louis\nMO\n63138-1002", "foia_phone": "314-801-0800", "foia_fax": "", "response_days": 20, "portal_url": "https://vetrecs.archives.gov/VeteranRequest/home.html"},
    {"name": "Office of the Solicitor", "abbreviation": "SOL", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Sharon Hudson, U.S. Department of Labor - Office of the Solicitor (MALS Division)\n200 Constitution Avenue, NW  - Room N2420\nWashington\nDC\n20210", "foia_phone": "202-693-5406", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/general/foia/readroom"},
    {"name": "Office of Public Affairs", "abbreviation": "OPA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Beverly Eaves, U.S. Department of Labor - Office of Public Affairs\n200 Constitution Avenue, NW - Room S2514\nWashington\nDC\n20210", "foia_phone": "202-693-4654", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Congressional and Intergovernmental Affairs", "abbreviation": "OCIA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Edward \"Pierce\" Bue, U.S. Department of Labor - Office of Congressional and Intergovernmental Affairs\n200 Constitution Avenue, NW - Room S2220\nWashington\nDC\n20210", "foia_phone": "202-693-4600", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Chief Financial Officer", "abbreviation": "OCFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Chris Polen, U.S. Department of Labor - Office of the Chief Financial Officer\n200 Constitution Avenue, NW - Room S4030\nWashington\nDC\n20210", "foia_phone": "202-693-6825", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Assistant Secretary for Policy", "abbreviation": "OASP", "foia_officer_title": "FOIA Officer", "foia_email": "oaspfoia@dol.gov", "foia_address": "Terry Fryer, U.S. Department of Labor, Office of the Assistant Secretary for Policy\n200 Constitution Avenue, NW - Room S2312\nWashington\nDC\n20210", "foia_phone": "202-693-5925", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/asp/foia"},
    {"name": "Adjudicatory Boards", "abbreviation": "Adj Bds", "foia_officer_title": "FOIA Officer", "foia_email": "boardsfoia@dol.gov", "foia_address": "Justin E. Murphy - U.S. Department of Labor, Adjudicatory Boards\n200 Constitution Avenue, NW - Room S5220\nWashington\nDC\n20210", "foia_phone": "202-693-6234", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "U.S. Cyber Command", "abbreviation": "CYBERCOM", "foia_officer_title": "FOIA Officer", "foia_email": "CYBERCOM_FOIA@cybercom.mil", "foia_address": "9800 Savage Road\nFort Meade\nMD\n20755", "foia_phone": "(301)688-3585", "foia_fax": "", "response_days": 20, "portal_url": "https://www.cybercom.mil/FOIA-Privacy-Act/Reading-Room/"},
    {"name": "Advisory Council on Historic Preservation", "abbreviation": "ACHP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Freedom of Information Act Officer Advisory Council on Historic Preservation \n401 F Street, NW, Suite 308 \nWashington\nDC\n20001", "foia_phone": "(202) 517-0200", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Commission on Military, National, and Public Service", "abbreviation": "NCMNPS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National Commission on Military, National, and Public Service\n2530 Crystal Drive, Suite 1000, Box No. 63\nArlington\nVA\n22202", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "U.S. International Development Finance Corporation", "abbreviation": "DFC", "foia_officer_title": "FOIA Officer", "foia_email": "foia@dfc.gov", "foia_address": "Eric Styles, FOIA Director\n1100 New York Ave., NW\nWashington\nDC\n20527", "foia_phone": "202-336-8400", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dfc.gov"},
    {"name": "Division of Legal Counsel", "abbreviation": "DLC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Credit Union Administration", "abbreviation": "NCUA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of General Counsel (International Law Division):  (Appeals and Litigation Only)", "abbreviation": "OGC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Archives and Records Administration", "abbreviation": "NARA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Grain Inspection, Packers and Stockyards Administration", "abbreviation": "GIPSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Background Investigations Bureau", "abbreviation": "NBIB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OPM Central Office", "abbreviation": "OPM CO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Secretary", "abbreviation": "OS", "foia_officer_title": "FOIA Officer", "foia_email": "eFOIA@doc.gov", "foia_address": "1401 Constitution Ave NW\nMail Stop 61025\nWashington \nDC\n20230", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/api/agencies/bafcb54b-e2fe-4055-a03c-1c80f491d3c5/requests"},
    {"name": "Naval Reactors Laboratory Field Office", "abbreviation": "NRLFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "P.O. Box 109\nAttn:  C. P. Nunn\nWest Mifflin\nPA\n15122-0109", "foia_phone": "412-476-7202", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "FOIA/Privacy Act Appeals Office", "abbreviation": "FOIA/PA Appeals", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Executive Secretariat", "abbreviation": "EXEC SEC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Disability Employment Policy", "abbreviation": "ODEP", "foia_officer_title": "FOIA Officer", "foia_email": "foiarequests@dol.gov", "foia_address": "200 Constituton Ave, NW\nWashington\nDC\n20210", "foia_phone": "202-693-7880", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/general/foia"},
    {"name": "Office of the Secretary", "abbreviation": "OSEC", "foia_officer_title": "FOIA Officer", "foia_email": "foiarequests@dol.gov", "foia_address": "200 Constituton Ave, NW\nWashington\nDC\n20210", "foia_phone": "202-693-4059", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dol.gov/general/foia/osec"},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Boston Regional Office", "abbreviation": "BORO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Dallas Regional Office", "abbreviation": "DARO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of General Counsel", "abbreviation": "OGC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Headquarters", "abbreviation": "HQ", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "National FOIA Office, Office of General Counsel, US Environmental Protection Agency\n1200 Pennsylvania Avenue, NW, Mail Code 2310A\nWashington\nDC\n20460", "foia_phone": "202-566-1667", "foia_fax": "", "response_days": 20, "portal_url": "https://foiapublicaccessportal.epa.gov/"},
    {"name": "Office of the Deputy Assistant Secretary for Intergovernmental Affairs", "abbreviation": "ODASIA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Electronic Health Record Modernization", "abbreviation": "OEHRM", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "811 Vermont Avenue NW\nWashington\nDC\n20420", "foia_phone": "(202) 461-0227", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Policy and Planning", "abbreviation": "P&P", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Public Affairs", "abbreviation": "PA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Secretary", "abbreviation": "OSVA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "do not use\nWashington\nDC\n20001", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Veterans Experience Office", "abbreviation": "VEO", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "810 Vermont Ave NW\nWashington\nDC\n20240", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.va.gov/FOIA/Requests.asp"},
    {"name": "Federal Student Aid", "abbreviation": "FSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Institute of Education Sciences", "abbreviation": "IES", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Institute of Education Sciences - National Assessment Governing Board", "abbreviation": "IES-NAGB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region I - Boston", "abbreviation": "OCR - 1 Boston", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region X - Seattle", "abbreviation": "OCR - 10 Seattle", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region XII - Cleveland", "abbreviation": "OCR - 12 Cleveland", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region II - New York", "abbreviation": "OCR - 2 New York", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region III - Philadelphia", "abbreviation": "OCR - 3 Philadelphia", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region IV - Atlanta", "abbreviation": "OCR - 4 Atlanta", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region V - Chicago", "abbreviation": "OCR - 5 Chicago", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region VI - Dallas", "abbreviation": "OCR - 6 Dallas", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region VII- Kansas City", "abbreviation": "OCR - 7 Kansas City", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region VIII - Denver", "abbreviation": "OCR - 8 Denver", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR Region IX - San Francisco", "abbreviation": "OCR - 9 San Francisco", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OCR District of Columbia", "abbreviation": "OCR - Metro D.C.", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Multiple Offices Assigned", "abbreviation": "M", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Planning, Evaluation, and Policy Development", "abbreviation": "OPEPD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Deputy Secretary", "abbreviation": "ODS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Under Secretary", "abbreviation": "OUS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Officer of Special Education and Rehabilitative Services", "abbreviation": "OSERS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Secretary", "abbreviation": "OS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Postsecondary Education", "abbreviation": "OPE", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OM - Office of the Chief Privacy Officer", "abbreviation": "OM-OCPO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Management", "abbreviation": "OM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Legislation and Congressional Affairs", "abbreviation": "OLCA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Innovation and Improvement", "abbreviation": "OII", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OFO - Contracts and Acquisitions Management", "abbreviation": "OFO-CAM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Chief Financial Officer", "abbreviation": "OCFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Chief Information Officer", "abbreviation": "OCIO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Communications and Outreach", "abbreviation": "OCO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office for Civil Rights (Headquarters)", "abbreviation": "OCR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Career, Technical & Adult Education", "abbreviation": "OCTAE", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Elementary and Secondary Education", "abbreviation": "OESE", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the General Counsel", "abbreviation": "OGC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "OIG FOIA Coordinator\n400 Maryland Avenue, S.W.\nWashington\nDC\n20202-1500", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.ed.gov/about/ed-overview/required-notices/freedom-of-information-act-foia"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "FOIARequests@hudoig.gov", "foia_address": "HUD OIG FOIA Office\n451 Seventh St., SW, Room 8186\nWashington\nDC\n20410-4500", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.hudoig.gov/freedom-information-act-foia/foia-request-form"},
    {"name": "Railroad Retirement Board", "abbreviation": "RRB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Departmental Administration / Office of the Secretary", "abbreviation": "DA/OSEC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Executive Secretariat", "abbreviation": "Exec Sec", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Finance and Operations", "abbreviation": "OFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Information Law Division", "abbreviation": "InfoLaw", "foia_officer_title": "FOIA Officer", "foia_email": "FOIAappeals@doc.gov", "foia_address": "1401 Constitution Ave NW\nWashington\nDC\n20230", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "NLRB OIG FOIA Office\n1015 Half Street SE\nWashington\nDC\n20570", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.securerelease.us/"},
    {"name": "National Labor Relations Board", "abbreviation": "NLRB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Security Commission on Artificial Intelligence", "abbreviation": "NSCAI", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@nscai.gov", "foia_address": "National Security Commission on Artificial Intelligence ATTN: FOIA Request\n2530 Crystal Drive, Box 45\nArlington\nVA\n22202", "foia_phone": "703-614-6386", "foia_fax": "", "response_days": 20, "portal_url": "https://www.nscai.gov/about"},
    {"name": "Office of the General Manager", "abbreviation": "OGM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the General Counsel", "abbreviation": "OGC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Drug Intelligence Center", "abbreviation": "NDIC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Dispute Resolution", "abbreviation": "ODR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Federal Detention Trustee", "abbreviation": "OFDT", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Intergovernmental and Public Liaison", "abbreviation": "OIPL", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Research and Innovative Technology Administration", "abbreviation": "RITA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Pacific Northwest Site Office", "abbreviation": "PNSO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Legal Counsel", "abbreviation": "OLC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Veterans Affairs Central Office", "abbreviation": "VACO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Veterans Canteen Service VACO", "abbreviation": "VCS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Management Directorate", "abbreviation": "MGMT", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the General Counsel", "abbreviation": "OGC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Operations Coordination", "abbreviation": "OPS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Policy", "abbreviation": "PLCY", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "United States Visitor and Immigrant Status Indicator Technology", "abbreviation": "US-VISIT", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Minerals Management Service", "abbreviation": "MMS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Chief Information Officer", "abbreviation": "OCIO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Small Business Programs", "abbreviation": "OSBP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Chief Financial Officer - Financial Systems Services", "abbreviation": "OCFO-FSS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of English Language Acquisition", "abbreviation": "OELA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Management - Regulatory Information Management Services", "abbreviation": "OM-RIMS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Safe and Drug-Free Schools", "abbreviation": "OSDFS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "U.S. Joint Forces Command", "abbreviation": "JFCOM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Administration on Aging", "abbreviation": "AOA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Public Health and Science", "abbreviation": "OPHS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Program Support Center", "abbreviation": "PSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Field Office", "abbreviation": "FO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Dryden Flight Research Center", "abbreviation": "DFRC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Financial Management Service", "abbreviation": "FMS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Bureau of Public Debt", "abbreviation": "BPD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Thrift Supervision", "abbreviation": "OTS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Appeals", "abbreviation": "Appeals", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Employment Standards Administration", "abbreviation": "ESA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Vocational and Adult Education", "abbreviation": "OVAE", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Defense Security Service", "abbreviation": "DSS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "TRICARE Management Agency, DoD Health Care System", "abbreviation": "TRICARE", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Housing and Urban Development", "abbreviation": "Agency", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "General Services Administration", "abbreviation": "GSA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Export-Import Bank of the U.S.", "abbreviation": "EXIM Bank", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Federal Housing Finance Agency", "abbreviation": "FHFA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Health Affairs", "abbreviation": "OHA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Bureau of Ocean Energy Management, Regulation and Enforcement", "abbreviation": "BOEMRE", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "HQ Postal Service", "abbreviation": "PS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Law Department Appeals only", "abbreviation": "LD Appeals", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Inspector General Appeals only", "abbreviation": "OIG Appeals", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Public Health and Science", "abbreviation": "OPHS/PSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Clerk of the Board", "abbreviation": "OCB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Food and Nutrition Consumer Service", "abbreviation": "FNCS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Departmental Management", "abbreviation": "DM", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "1400 Independence Avenue SW \nRoom 4104-S \nWashington\nDC\n20250-0706", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "Office of Civil Rights", "abbreviation": "ASCR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Secretary", "abbreviation": "OSEC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Management", "abbreviation": "Mgmt - do not use", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "do not use\nwashington\nDC\n20001", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Management and Administrative Programs", "abbreviation": "OMAP", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Special Inspector General for Iraq Reconstruction", "abbreviation": "SIGIR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Legal Services Corporation", "abbreviation": "LSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "OM - Privacy, Information, Record Management Services", "abbreviation": "OM-PIRMS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "FOIA Policy, Office of Executive Secretariat and Regulatory Affairs", "abbreviation": "OES", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Consumer Protection Financial Board", "abbreviation": "CFPB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Secretary / Office of the Chief Information Officer", "abbreviation": "OSEC/OCIO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Assistant General Counsel for Administration", "abbreviation": "AGC/Admin", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Safe and Healthy Students", "abbreviation": "OSHS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Recovery Accountability and Transparency Board", "abbreviation": "RATB", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Assistant Secretary – Indian Affairs", "abbreviation": "ASIA", "foia_officer_title": "FOIA Officer", "foia_email": "as-ia_foia@bia.gov", "foia_address": "Justin Davis, FOIA Officer\nMS-4660; 1849 C Street, NW\nWashington\nDC\n20240", "foia_phone": "(202) 513-7707", "foia_fax": "", "response_days": 20, "portal_url": "https://www.bia.gov/as-ia/foia/reading-room"},
    {"name": "Bureau of Indian Education", "abbreviation": "BIE", "foia_officer_title": "FOIA Officer", "foia_email": "foia@bie.edu", "foia_address": "Justin Davis, FOIA Officer\nMS-4660; 1849 C Street, NW\nWashington\nDC\n20240", "foia_phone": "(202) 513-7707", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Heritage Abroad", "abbreviation": "HA", "foia_officer_title": "FOIA Officer", "foia_email": "uscommission@heritageabroad.gov", "foia_address": "1050 Connecticut Ave., NW\nSuite 500\nWashington\nDC\n20036", "foia_phone": "12022543824", "foia_fax": "", "response_days": 20, "portal_url": "https://www.heritageabroad.gov/about"},
    {"name": "Department of Labor - Main", "abbreviation": "DOL - Main", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Legal Services Corporation - Main", "abbreviation": "LSC - Main", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "USPS General Counsel", "abbreviation": "PS & PIS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Federal Housing Enterprise Oversight", "abbreviation": "OFHEO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Federal Protective Service", "abbreviation": "FPS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "DHS Privacy Office, Mail Stop 0655\n2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528", "foia_phone": "202-343-1743", "foia_fax": "", "response_days": 20, "portal_url": "http://www.dhs.gov/dhs-foia-request-submission-form"},
    {"name": "Office of Hearings and Appeals", "abbreviation": "OHA", "foia_officer_title": "FOIA Officer", "foia_email": "usdafoia@usda.gov", "foia_address": "1400 Independence Avenue, SW\nRoom 4039-A\nWashington\nDC\n20250-0706", "foia_phone": "202-690-3318", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia-pal.usda.gov/"},
    {"name": "FOIA & Privacy Act Appeals Office", "abbreviation": "FPAO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Departmental FOIA Office", "abbreviation": "DFO", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Surface Mining Reclamation and Enforcement", "abbreviation": "OSMRE", "foia_officer_title": "FOIA Officer", "foia_email": "foia@osmre.gov", "foia_address": "Julie Moore, FOIA Officer\nOSMRE, Three Parkway Center\nPittsburgh\nPA\n15220", "foia_phone": "(412) 552-9440", "foia_fax": "", "response_days": 20, "portal_url": "https://www.osmre.gov/about/general-information"},
    {"name": "Office of Chief Human Capitol Officer", "abbreviation": "OCHCO", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "810 Vermont Ave NW\nWashington\nDC\n20420", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Resolution Management", "abbreviation": "ORMDI", "foia_officer_title": "FOIA Officer", "foia_email": "vacofoiase@va.gov", "foia_address": "810 Vermont Avenue, NW\n(08) VACO \nWashington\nDC\n20420", "foia_phone": "727-295-9139", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "National Passenger Railroad Corporation Headquarters", "abbreviation": "NRPC HQ", "foia_officer_title": "FOIA Officer", "foia_email": "rebecca.conner@amtrak.com", "foia_address": "1 Massachusetts Ave NW\nFOIA Office\nWashington\nDC\n20001", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.amtrak.com/about-amtrak/foia.html"},
    {"name": "Office of the Inspector General", "abbreviation": "OIG", "foia_officer_title": "FOIA Officer", "foia_email": "foia@amtrakoig.gov", "foia_address": "Amtrak Office of Inspector General\n10 G St. NE, Suite 3W-300\nWashington\nDC\n20002", "foia_phone": "202.906.4521", "foia_fax": "", "response_days": 20, "portal_url": "https://amtrakoig.gov/reading-room-documents"},
    {"name": "Bureau of Trust Funds Administration", "abbreviation": "BTFA", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Department of Commerce (General) - 2", "abbreviation": "DOCG", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Secretary", "abbreviation": "DOC OS", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the Special Trustee", "abbreviation": "OST", "foia_officer_title": "FOIA Officer", "foia_email": "ost_foia@ost.doi.gov", "foia_address": "Keith Parsky, FOIA Officer\n1849 C Street, NW, Room 3214\nWashington\nDC\n20240", "foia_phone": "202-208-3521", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/request"},
    {"name": "Barack Obama Presidential Library", "abbreviation": "LP-BHO", "foia_officer_title": "FOIA Officer", "foia_email": "obama.library@nara.gov", "foia_address": "2500 W. Golf Road\nHoffman Estates\nIL\n60169-1114", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "U.S. Space Command", "abbreviation": "SPACECOM", "foia_officer_title": "FOIA Officer", "foia_email": "usspacecom.J0.FOIA-PA@us.af.mil", "foia_address": "Office of the Staff Judge Advocate\n150 Vandenberg Street\nPeterson SFB \nCO\n80914", "foia_phone": "(719) 552-1631", "foia_fax": "", "response_days": 20, "portal_url": "https://www.spacecom.mil/Support/FOIA/Reading-Room/"},
    {"name": "Federal Permitting Improvement Steering Council", "abbreviation": "FPISC-Main", "foia_officer_title": "FOIA Officer", "foia_email": "foia@permitting.gov", "foia_address": "Federal Permitting Improvement Steering Council / Attn: FOIA Office\n1800 G Street, NW Suite 9100\nWashington\nDC\n20036", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.permitting.gov/foia-reading-room"},
    {"name": "Office of the Intellectual Property Enforcement Coordinator", "abbreviation": "IPEC-Main", "foia_officer_title": "FOIA Officer", "foia_email": "ipecfoia@ipec.eop.gov", "foia_address": "Steven D. Aitken, Room 296, Eisenhower Executive Office Building\n1650 Pennsylvania Ave. NW\nWashington\nDC\n20503", "foia_phone": "(202) 395-4728", "foia_fax": "", "response_days": 20, "portal_url": "https://www.whitehouse.gov/ipec/legal/"},
    {"name": "FOIA Service Center", "abbreviation": "FSC", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "123 test\nwashington\nDC\n20001", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of the National Cyber Director", "abbreviation": "ONCD", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@ncd.eop.gov", "foia_address": "Office of the National Cyber Director\n725 17th Street NW, Room 4203\nWashington\nDC\n20503", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.whitehouse.gov/oncd/information-resources/"},
    {"name": "Office for Access to Justice", "abbreviation": "ATJ", "foia_officer_title": "FOIA Officer", "foia_email": "MRUFOIA.Requests@usdoj.gov", "foia_address": "Justice Management Division, Room 115, LOC Building\nWashington\nDC\n20530", "foia_phone": "202-616-5979", "foia_fax": "", "response_days": 20, "portal_url": "https://www.foia.gov/request/agency-component/f5268984-4616-4862-9890-ae170cbde6c0/"},
    {"name": "DHS Privacy Office", "abbreviation": "PRIVACY", "foia_officer_title": "FOIA Officer", "foia_email": "foia@hq.dhs.gov", "foia_address": "DHS Privacy Office, Mail Stop 0655\n2707 Martin Luther King Jr. AVE SE\nWashington\nDC\n20528-0655", "foia_phone": "202-343-1743", "foia_fax": "", "response_days": 20, "portal_url": "https://www.dhs.gov/dhs-foia-privacy-act-request-submission-form"},
    {"name": "Assistant Secretary – Indian Affairs", "abbreviation": "AS-IA", "foia_officer_title": "FOIA Officer", "foia_email": "as-ia_foia@bia.gov", "foia_address": "Justin Davis, FOIA Officer\nMS-4660; 1849 C Street, NW\nWashington\nDC\n20240", "foia_phone": "(202) 513-7707", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.gov/foiaonline/action/public/request"},
    {"name": "Science & Technology Directorate", "abbreviation": "STD", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "123\nWashington\nDC\n20503", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Office of Pandemic Preparedness and Response Policy", "abbreviation": "OPPR", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Scott Levy\n1650 17th Street NW Suite 497\nWashington\nDC\n20006", "foia_phone": "(202) 456-6124", "foia_fax": "", "response_days": 20, "portal_url": "https://www.whitehouse.gov/oppr/"},
    {"name": "United States Space Force", "abbreviation": "USSF", "foia_officer_title": "FOIA Officer", "foia_email": "DAF.FOIA@us.af.mil", "foia_address": "SAF/AAII (FOIA)\n1000 Air Force Pentagon\nWashington\nDC\n20330-1000", "foia_phone": "703-693-2735", "foia_fax": "", "response_days": 20, "portal_url": "https://efoia.milcloud.mil/palMain.aspx"},
    {"name": "Office of the Executive Secretariat", "abbreviation": "OES", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "Andrew Fiorillo, Chief, Initial Request Staff\n441 G St, NW, 6th Floor \nWashington\nDC\n20530", "foia_phone": "202-514-3642", "foia_fax": "", "response_days": 20, "portal_url": "https://foiaonline.regulations.gov/foia/action/public/home"},
    {"name": "Advanced Research Projects Agency for Health", "abbreviation": "ARPA-H", "foia_officer_title": "FOIA Officer", "foia_email": "FOIA@arpa-h.gov", "foia_address": "ARPA-H FOIA Office\n1301 K Street NW\nWashington\nDC\n20005", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://arpa-h.gov/freedom-of-information-act"},
    {"name": "Bureau of Consular Affairs, Passport Services", "abbreviation": "PPT", "foia_officer_title": "FOIA Officer", "foia_email": "", "foia_address": "DOS Office of Records Management/Records Review and Release Division\n44132 Mercure Circle, P.O. Box 1227\nSterling\nVA\n20166", "foia_phone": "202-485-6550", "foia_fax": "", "response_days": 20, "portal_url": "https://foia.state.gov/FOIALIBRARY/SearchResults.aspx"},
    {"name": "Civil Rights Cold Case Records Review Board", "abbreviation": "CRCCRRB", "foia_officer_title": "FOIA Officer", "foia_email": "info@coldcaserecords.gov", "foia_address": "Civil Rights Cold Case Records Review Board\n1800 F St NW\nWashington\nDC\n20405", "foia_phone": "771-221-0014", "foia_fax": "", "response_days": 20, "portal_url": ""},
    {"name": "Donald J. Trump Presidential Library", "abbreviation": "PL-DJT", "foia_officer_title": "FOIA Officer", "foia_email": "trump.library@nara.gov", "foia_address": "Donald J. Trump Presidential Library\n8601 Adelphi Road, Room 1900\nCollege Park\nMD\n20740", "foia_phone": "", "foia_fax": "", "response_days": 20, "portal_url": "https://www.trumplibrary.gov/research/research-links"},
]


def _seed_agencies():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as cnt FROM federal_agencies").fetchone()
    if count['cnt'] == 0:
        for a in AGENCIES:
            db.execute("""
                INSERT INTO federal_agencies
                (name, abbreviation, foia_officer_title, foia_email,
                 foia_address, foia_phone, foia_fax, response_days, portal_url)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                a["name"], a["abbreviation"], a["foia_officer_title"],
                a["foia_email"], a["foia_address"], a["foia_phone"],
                a["foia_fax"], a["response_days"], a["portal_url"]
            ))
        db.commit()
    db.close()


# ─────────────────────────────────────────────
# State laws seed data (all 50 states)
# ─────────────────────────────────────────────
STATE_LAWS = [
    ("Alabama",       "AL", "Alabama Open Records Act",                  "Code of Ala. § 36-12-40",        10, 15,  "Must acknowledge within 10 business days; fulfill within 15. Complex requests may take longer with notice."),
    ("Alaska",        "AK", "Alaska Public Records Act",                 "AS 40.25.110",                    10, 10,  "Generally 10 working days; municipal requests can be as short as 2 days."),
    ("Arizona",       "AZ", "Arizona Public Records Law",                "A.R.S. § 39-121.01",              10, 30,  "Acknowledge within 5 business days; produce within reasonable time. HOA requests have 10-day limit."),
    ("Arkansas",      "AR", "Arkansas FOIA",                             "Ark. Code § 25-19-105",            3, 30,  "3 business days to respond; applies to records in active use or storage."),
    ("California",    "CA", "California Public Records Act",             "Cal. Gov. Code § 6253",           10, 10,  "10 days to respond; may extend up to 14 additional days for unusual circumstances."),
    ("Colorado",      "CO", "Colorado Open Records Act (CORA)",          "C.R.S. § 24-72-203",               3, 14,  "Reasonable time presumed to be 3 working days or less."),
    ("Connecticut",   "CT", "Connecticut FOIA",                          "Conn. Gen. Stat. § 1-206",         4, 30,  "Records provided 'promptly,' typically 4 business days. Personnel records may take up to 10 days."),
    ("Delaware",      "DE", "Delaware FOIA",                             "29 Del. C. § 10003",              15, 29,  "15 business days to respond and produce."),
    ("Florida",       "FL", "Florida Public Records Law",                "Fla. Stat. § 119.07",              0, 30,  "No statutory deadline; 'reasonable' time based on size/complexity. Courts enforce promptness."),
    ("Georgia",       "GA", "Georgia Open Records Act",                  "O.C.G.A. § 50-18-71",              3, 30,  "3 business days to produce records or provide written response explaining delay."),
    ("Hawaii",        "HI", "Uniform Information Practices Act",         "HRS § 92F-11",                    10, 30,  "10 business days to respond; may extend reasonable time with notice."),
    ("Idaho",         "ID", "Idaho Public Records Law",                  "Idaho Code § 74-102",              3, 28,  "3 working days to respond; may extend up to 10 additional working days for voluminous records."),
    ("Illinois",      "IL", "Illinois FOIA",                             "5 ILCS 140/3",                     5, 60,  "5 business days to grant/deny; can extend 5 additional business days. Produce within reasonable time."),
    ("Indiana",       "IN", "Indiana Access to Public Records Act",      "IC 5-14-3-9",                      7, 30,  "7 days to produce or deny; reasonable time for production. Denial by non-production within 7 days."),
    ("Iowa",          "IA", "Iowa Open Records Law",                     "Iowa Code § 22.8",                 0, 20,  "As soon as reasonably possible; no specific deadline. Courts find 2–4 weeks reasonable."),
    ("Kansas",        "KS", "Kansas Open Records Act",                   "K.S.A. § 45-218",                  3, 30,  "3 business days to provide records or explain why access will take longer."),
    ("Kentucky",      "KY", "Kentucky Open Records Act",                 "KRS § 61.880",                     5, 40,  "5 business days to provide records or detailed explanation of delay."),
    ("Louisiana",     "LA", "Louisiana Public Records Law",              "La. R.S. 44:32",                   3, 60,  "3 business days to respond; immediate for inspection, reasonable time for copies."),
    ("Maine",         "ME", "Maine FOAA",                                "1 M.R.S. § 408-A",                 5, 30,  "5 working days to respond (grant, deny, or request extension); reasonable time to produce."),
    ("Maryland",      "MD", "Maryland Public Information Act",           "Md. Code, GP § 4-203",            30, 30,  "30 days to respond; may extend 30 additional days with written notice."),
    ("Massachusetts", "MA", "Massachusetts Public Records Law",          "M.G.L. c. 66, § 10",              10, 90,  "10 business days to comply or respond with timeline; may extend 10 additional business days."),
    ("Michigan",      "MI", "Michigan FOIA",                             "MCL 15.235",                       5, 180, "5 business days to respond; can extend 10 additional business days with detailed written justification."),
    ("Minnesota",     "MN", "Minnesota Government Data Practices Act",   "Minn. Stat. § 13.03",             10, 60,  "10 business days to respond; must provide data or explain when available 'as soon as reasonably possible.'"),
    ("Mississippi",   "MS", "Mississippi Public Records Act",            "Miss. Code § 25-61-5",             7,  7,  "7 working days to provide records or written statement of grounds for denial."),
    ("Missouri",      "MO", "Missouri Sunshine Law",                     "RSMo § 610.023",                   3,  0,  "3 business days to provide records, deny, or provide records on a longer but reasonable timeline with written explanation. No appeal deadline specified."),
    ("Montana",       "MT", "Montana Right to Know Act",                 "MCA 2-6-1003",                     5, 60,  "5 business days to respond with records or explain delay."),
    ("Nebraska",      "NE", "Nebraska Public Records Law",               "Neb. Rev. Stat. § 84-712",         4, 30,  "4 business days to respond with determination and timeline."),
    ("Nevada",        "NV", "Nevada Public Records Act",                 "NRS 239.0107",                     5, 30,  "5 business days to respond; must produce within reasonable time."),
    ("New Hampshire", "NH", "New Hampshire Right-to-Know Law",           "RSA 91-A:4",                       5,  0,  "5 business days to make records available or respond with refusal. No appeal deadline specified."),
    ("New Jersey",    "NJ", "New Jersey OPRA",                           "N.J.S.A. 47:1A-5",                 7, 45,  "7 business days to grant access, deny, or seek extension (up to 7 additional business days)."),
    ("New Mexico",    "NM", "New Mexico Inspection of Public Records Act","NMSA § 14-2-11",                  3, 15,  "3 business days for inspection; up to 15 business days for copies if extensive research required."),
    ("New York",      "NY", "New York FOIL",                             "Public Officers Law § 89",         5, 30,  "5 business days to grant/deny access; 20 business days to produce records after granting."),
    ("North Carolina","NC", "North Carolina Public Records Law",         "N.C.G.S. § 132-6",                 0,  0,  "Reasonable time; no statutory deadline. Courts consider size, complexity, staff availability."),
    ("North Dakota",  "ND", "North Dakota Open Records Law",             "N.D.C.C. § 44-04-18",              3, 60,  "3 business days to respond with records or explanation."),
    ("Ohio",          "OH", "Ohio Public Records Act",                   "Ohio Rev. Code § 149.43",           0, 30,  "Reasonable time; must respond 'promptly.' Courts generally find 2 weeks–30 days reasonable."),
    ("Oklahoma",      "OK", "Oklahoma Open Records Act",                 "51 Okla. Stat. § 24A.5",            0, 30,  "Reasonable time based on circumstances; no specific statutory deadline."),
    ("Oregon",        "OR", "Oregon Public Records Law",                 "ORS 192.410",                      0,  0,  "Reasonable time; generally interpreted as within a few days for simple requests."),
    ("Pennsylvania",  "PA", "Pennsylvania Right-to-Know Law",            "65 P.S. § 67.901",                 5, 15,  "5 business days to respond; can extend 30 additional calendar days for complex requests."),
    ("Rhode Island",  "RI", "Rhode Island Access to Public Records Act", "R.I. Gen. Laws § 38-2-3",         10, 35,  "10 business days to respond with records or written statement."),
    ("South Carolina","SC", "South Carolina FOIA",                       "S.C. Code § 30-4-30",             10, 35,  "10 business days to determine whether to grant; if granted, fulfill within 30 calendar days."),
    ("South Dakota",  "SD", "South Dakota Sunshine Law",                 "SDCL § 1-27-1",                   10, 60,  "10 business days to respond; reasonable time to produce."),
    ("Tennessee",     "TN", "Tennessee Public Records Act",              "Tenn. Code § 10-7-503",             7, 60,  "7 business days to respond with determination; reasonable time to produce."),
    ("Texas",         "TX", "Texas Public Information Act",              "Tex. Gov't Code § 552.221",        10, 30,  "10 business days to respond with information or request AG opinion."),
    ("Utah",          "UT", "Utah GRAMA",                                "Utah Code § 63G-2-204",             5, 35,  "5 business days to respond (up to 10 additional business days if voluminous)."),
    ("Vermont",       "VT", "Vermont Public Records Act",                "1 V.S.A. § 318",                   3, 30,  "3 business days to respond; reasonable time to produce."),
    ("Virginia",      "VA", "Virginia FOIA",                             "Va. Code § 2.2-3704",               5,  0,  "5 working days to respond; may provide written explanation of extra time needed (up to 7 additional days)."),
    ("Washington",    "WA", "Washington Public Records Act",             "RCW 42.56.520",                    5,  2,  "5 business days to respond; must make available 'promptly' within reasonable time."),
    ("West Virginia", "WV", "West Virginia FOIA",                        "W. Va. Code § 29B-1-3",            5, 180, "5 working days to respond and provide records or written explanation."),
    ("Wisconsin",     "WI", "Wisconsin Open Records Law",                "Wis. Stat. § 19.35",               0,  0,  "As soon as practicable and without delay; courts have found 2 weeks reasonable for routine requests."),
    ("Wyoming",       "WY", "Wyoming Public Records Act",                "Wyo. Stat. § 16-4-203",            3, 20,  "3 business days to respond; reasonable time to provide copies."),
]


def _seed_states():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as cnt FROM state_laws").fetchone()
    if count['cnt'] == 0:
        for s in STATE_LAWS:
            db.execute("""
                INSERT INTO state_laws
                (state_name, state_code, law_name, citation, response_days, appeal_days, notes)
                VALUES (?,?,?,?,?,?,?)
            """, s)
        db.commit()
    db.close()


# ─────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────
def hash_password(pw):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 260000)
    return salt.hex() + ":" + dk.hex()


def verify_password(stored, provided):
    try:
        salt_hex, dk_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", provided.encode(), salt, 260000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def subscription_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        # ── Lifetime-free users always have access ─────────────────
        db = get_db()
        user = db.execute(
            "SELECT subscription_status, lifetime_free FROM users WHERE id=?",
            (session["user_id"],)
        ).fetchone()
        db.close()
        if user and user["lifetime_free"]:
            return f(*args, **kwargs)
        # ── TEST MODE: subscription gate disabled ──────────────────
        # All registered users have full access.
        # Re-enable Stripe check before going live.
        # ──────────────────────────────────────────────────────────
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
# FOIA number generator
# ─────────────────────────────────────────────
def next_foia_number():
    year = datetime.now().year
    db = get_db()
    db.insert_or_ignore("foia_sequence", ["year", "last_seq"], (year, 0))
    db.execute("UPDATE foia_sequence SET last_seq = last_seq + 1 WHERE year=?", (year,))
    row = db.execute("SELECT last_seq FROM foia_sequence WHERE year=?", (year,)).fetchone()
    db.commit()
    db.close()
    return f"{year}-{row['last_seq']:03d}"


# ─────────────────────────────────────────────
# Business-day deadline calculator
# ─────────────────────────────────────────────
FEDERAL_HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 4),
    date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
    date(2027, 5, 31), date(2027, 6, 19), date(2027, 7, 5),
    date(2027, 9, 6), date(2027, 10, 11), date(2027, 11, 11),
    date(2027, 11, 25), date(2027, 12, 24),
}


def add_business_days(start_date, days):
    """Return the date that is `days` business days after start_date."""
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    current = start_date
    counted = 0
    while counted < days:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in FEDERAL_HOLIDAYS:
            counted += 1
    return current


def days_since(target_date):
    """Positive = overdue by N days, negative = N days remaining."""
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    return (date.today() - target_date).days


# ─────────────────────────────────────────────
# Action logger
# ─────────────────────────────────────────────
def log_action(request_id, user_id, action, note=None):
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    db = get_db()
    db.execute(
        "INSERT INTO action_log (request_id, user_id, action, note, logged_at) VALUES (?,?,?,?,?)",
        (request_id, user_id, action, note, now)
    )
    db.execute(
        "UPDATE requests SET last_action_date=?, last_action_note=?, updated_at=? WHERE id=?",
        (now, action, now, request_id)
    )
    db.commit()
    db.close()


# ─────────────────────────────────────────────
# Letter template builder
# ─────────────────────────────────────────────
SEGREGABILITY = """I request that all reasonably segregable portions of exempt material be released and that redactions be limited to those authorized by law, with specific exemptions cited. If any portion of this request is denied, please provide a detailed justification and inform me of my appeal rights. If my request is denied in whole or in part, please provide the exemptions withholdings by reference to specific exemptions of the applicable law and provide the name of the official responsible for the denial."""

FEE_WAIVER_FEDERAL = """I request a waiver of fees associated with this request. I am an independent journalist and researcher working in the public interest. Disclosure of these records will contribute significantly to public understanding of government operations and policy. This request is not being made for commercial purposes. Therefore, I request a waiver of all fees under 5 U.S.C. § 552(a)(4)(A)(iii). If fees are expected to exceed $25, please notify me in advance."""

FEE_WAIVER_STATE = """I request a waiver of fees associated with this request. I am an independent journalist and researcher working in the public interest. Disclosure of these records will contribute significantly to public understanding of government operations and policy. This request is not being made for commercial purposes. Therefore, I request a waiver of all fees under the applicable state public records law. If fees are expected to exceed $25, please notify me in advance."""

# Keep backward-compatible aliases
FEE_WAIVER = FEE_WAIVER_FEDERAL
# Keep backward-compatible alias
FEE_WAIVER = FEE_WAIVER_FEDERAL


def build_letter_text(agency_name, foia_officer_title, subject, statute="5 U.S.C. § 552",
                      agency_type="Federal", law_name=None, response_days=20):
    today = date.today().strftime("%B %d, %Y")
    is_state = agency_type == "State"

    fee_waiver = FEE_WAIVER_STATE if is_state else FEE_WAIVER_FEDERAL

    if is_state:
        via_line = "Via Written Request"
        re_line = f"Re: Public Records Request — {subject}"
        salutation = f"Dear {foia_officer_title}:"
        opening = f"Pursuant to the {law_name or 'state public records law'}, {statute}, I hereby request access to and copies of the following records:"
        closing = (
            f"Thank you for your attention to this request. I look forward to your response "
            f"within a reasonable time as required by {statute}.\n\nRespectfully submitted,"
            if not response_days else
            f"Thank you for your attention to this request. I look forward to your response "
            f"within {response_days} business days as required by {statute}.\n\nRespectfully submitted,"
        )
    else:
        via_line = "Via Electronic Submission"
        re_line = f"Re: Freedom of Information Act Request — {subject}"
        salutation = f"Dear {foia_officer_title}:"
        opening = f"Pursuant to the Freedom of Information Act, {statute}, and the regulations promulgated thereunder, I hereby request the following records:"
        closing = (
            f"Thank you for your attention to this request. I look forward to your response "
            f"within the statutory timeframe as required by {statute}.\n\nRespectfully submitted,"
        )

    return f"""{today}

{foia_officer_title}
{agency_name}

{via_line}

{re_line}

{salutation}

{opening}

{subject}

[Please describe the records you are seeking in detail here. Include relevant dates, names, document types, programs, or other identifying information that will help locate the responsive records.]

{SEGREGABILITY}

{fee_waiver}

{closing}

[Your Name]
[Your Address]
[Your Email]
[Your Phone]"""


# ─────────────────────────────────────────────
# Routes: Admin — Invite Codes
# ─────────────────────────────────────────────
ADMIN_USER = os.environ.get("ADMIN_USER", "")

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_USER:
            return jsonify({"error": "Admin not configured"}), 403
        if session.get("username") != ADMIN_USER:
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated

@app.route("/api/admin/invite-codes", methods=["GET"])
@admin_required
def admin_list_codes():
    db = get_db()
    rows = db.execute(
        "SELECT code, used, used_by, created_at FROM invite_codes ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/invite-codes", methods=["POST"])
@admin_required
def admin_create_codes():
    data = request.get_json() or {}
    count = min(int(data.get("count", 1)), 50)
    db = get_db()
    created = []
    for _ in range(count):
        suffix = secrets.token_hex(4).upper()
        code = f"AEOD-{suffix}"
        db.execute("INSERT INTO invite_codes (code) VALUES (?)", (code,))
        created.append(code)
    db.commit()
    db.close()
    return jsonify({"created": created})

# ─────────────────────────────────────────────
# Routes: Auth
# ─────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    invite_code = (data.get("invite_code") or "").strip().upper()

    if not username or not email or not password:
        return jsonify({"error": "All fields required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    db = get_db()
    existing = db.execute(
        "SELECT id FROM users WHERE username=? OR email=?", (username, email)
    ).fetchone()
    if existing:
        db.close()
        return jsonify({"error": "Username or email already taken"}), 409

    # Check invite code if provided
    lifetime_free = 0
    code_row = None
    if invite_code:
        code_row = db.execute(
            "SELECT id FROM invite_codes WHERE code=? AND used=0", (invite_code,)
        ).fetchone()
        if not code_row:
            db.close()
            return jsonify({"error": "Invalid or already used invite code"}), 400
        lifetime_free = 1

    db.execute(
        "INSERT INTO users (username, email, password, subscription_status, lifetime_free) VALUES (?,?,?,?,?)",
        (username, email, hash_password(password), "active", lifetime_free)
    )
    db.commit()
    user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()

    # Mark invite code as used
    if code_row:
        db.execute(
            "UPDATE invite_codes SET used=1, used_by=? WHERE id=?",
            (user["id"], code_row["id"])
        )
        db.commit()

    db.close()
    session["user_id"] = user["id"]
    session["username"] = username
    return jsonify({"ok": True, "username": username, "subscription_status": "active", "lifetime_free": lifetime_free})


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE username=? OR email=?", (username, username)
    ).fetchone()
    db.close()

    if not user or not verify_password(user["password"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return jsonify({
        "ok": True,
        "username": user["username"],
        "subscription_status": user["subscription_status"]
    })


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def me():
    if "user_id" not in session:
        return jsonify({"authenticated": False})
    db = get_db()
    user = db.execute(
        "SELECT id, username, email, subscription_status FROM users WHERE id=?",
        (session["user_id"],)
    ).fetchone()
    db.close()
    return jsonify({"authenticated": True, **dict(user)})


# ─────────────────────────────────────────────
# Routes: Stripe / Subscription
# ─────────────────────────────────────────────
@app.route("/api/stripe/create-checkout", methods=["POST"])
@login_required
def create_checkout():
    """
    In production this calls stripe.checkout.Session.create().
    Here we return instructions since network is unavailable in dev.
    Replace the body with real Stripe SDK calls when deploying.
    """
    return jsonify({
        "message": "Stripe integration ready — add your STRIPE_SECRET_KEY and STRIPE_PRICE_ID env vars.",
        "setup_instructions": [
            "1. pip install stripe",
            "2. Set STRIPE_SECRET_KEY and STRIPE_PRICE_ID in your .env",
            "3. Uncomment the Stripe SDK code in app.py",
            "4. Set your webhook endpoint to /api/stripe/webhook"
        ]
    })


@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """
    Handles: checkout.session.completed, customer.subscription.updated,
    customer.subscription.deleted
    """
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # Verify signature in production:
    # event = stripe.Webhook.construct_event(payload, sig, webhook_secret)

    try:
        event = json.loads(payload)
    except Exception:
        return "Bad payload", 400

    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    db = get_db()
    if event_type == "checkout.session.completed":
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        email = obj.get("customer_details", {}).get("email", "")
        db.execute(
            """UPDATE users SET stripe_customer_id=?, stripe_sub_id=?,
               subscription_status='active' WHERE email=?""",
            (customer_id, sub_id, email.lower())
        )
    elif event_type == "customer.subscription.updated":
        sub_id = obj.get("id")
        status = obj.get("status", "inactive")
        db.execute(
            "UPDATE users SET subscription_status=? WHERE stripe_sub_id=?",
            (status, sub_id)
        )
    elif event_type == "customer.subscription.deleted":
        sub_id = obj.get("id")
        db.execute(
            "UPDATE users SET subscription_status='inactive' WHERE stripe_sub_id=?",
            (sub_id,)
        )
    db.commit()
    db.close()
    return "ok", 200


# DEV ONLY — activate subscription without Stripe (remove in production)
@app.route("/api/dev/activate", methods=["POST"])
@login_required
def dev_activate():
    db = get_db()
    db.execute(
        "UPDATE users SET subscription_status='active' WHERE id=?",
        (session["user_id"],)
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "message": "Dev subscription activated"})


# ─────────────────────────────────────────────
# Routes: Agencies
# ─────────────────────────────────────────────
@app.route("/api/agencies")
@login_required
def get_agencies():
    try:
        db = get_db()
        rows = db.execute(
            """SELECT id, name, abbreviation, foia_officer_title, foia_email,
                      foia_address, foia_phone, foia_fax, response_days, portal_url
               FROM federal_agencies WHERE name NOT LIKE ? ORDER BY name""",
            ('%DO NOT USE%',)
        ).fetchall()
        db.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agencies/<int:agency_id>")
@login_required
def get_agency(agency_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM federal_agencies WHERE id=?", (agency_id,)
    ).fetchone()
    db.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))

# ─────────────────────────────────────────────────────────────────────────────
# Routes: State & Local Law Enforcement Agencies
# Add these routes to app.py after your existing /api/agencies routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/law-enforcement/agencies")
@login_required
def get_le_agencies():
    """
    List state & local law enforcement agencies.

    Query params:
        state     — filter by state abbreviation (e.g. ?state=TX)
        type      — filter by agency type (e.g. ?type=City)
        q         — search by name (e.g. ?q=austin)
        limit     — max results, default 50, max 200
        offset    — pagination offset, default 0

    Examples:
        /api/law-enforcement/agencies?state=TX
        /api/law-enforcement/agencies?state=TX&type=City
        /api/law-enforcement/agencies?q=austin&state=TX
        /api/law-enforcement/agencies?type=Sheriff&limit=100
    """
    db = get_db()

    state  = request.args.get("state", "").upper().strip()
    agency_type = request.args.get("type", "").strip()
    query  = request.args.get("q", "").strip()

    try:
        limit  = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        limit, offset = 50, 0

    # Build query dynamically based on filters provided
    conditions = []
    params = []

    if state:
        conditions.append("state_abbr = ?")
        params.append(state)

    if agency_type:
        conditions.append("agency_type = ?")
        params.append(agency_type)

    if query:
        conditions.append("(agency_name LIKE ? OR county_name LIKE ?)")
        params.append(f"%{query}%")
        params.append(f"%{query}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Get total count for pagination
    count_row = db.execute(
        f"SELECT COUNT(*) as total FROM state_local_agencies {where}",
        tuple(params)
    ).fetchone()
    total = count_row["total"] if count_row else 0

    # Get paginated results
    params_with_limit = params + [limit, offset]
    rows = db.execute(
        f"""
        SELECT
            id, ori, agency_name, agency_unit, state_abbr,
            county_name, agency_type, population_group, population,
            division_name, region_name, officer_ct, civilian_ct,
            total_pe_ct, data_year,
            foia_officer, foia_email, foia_phone, foia_address, foia_portal_url,
            notes
        FROM state_local_agencies
        {where}
        ORDER BY state_abbr, agency_name
        LIMIT ? OFFSET ?
        """,
        tuple(params_with_limit)
    ).fetchall()

    db.close()

    return jsonify({
        "total":   total,
        "limit":   limit,
        "offset":  offset,
        "results": [dict(r) for r in rows]
    })


@app.route("/api/law-enforcement/agencies/<int:agency_id>")
@login_required
def get_le_agency(agency_id):
    """
    Get a single state/local agency by its database ID.

    Example:
        /api/law-enforcement/agencies/42
    """
    db = get_db()
    row = db.execute(
        "SELECT * FROM state_local_agencies WHERE id = ?",
        (agency_id,)
    ).fetchone()
    db.close()

    if not row:
        return jsonify({"error": "Not found"}), 404

    return jsonify(dict(row))


@app.route("/api/law-enforcement/agencies/ori/<string:ori>")
@login_required
def get_le_agency_by_ori(ori):
    """
    Look up an agency by its ORI code (FBI unique identifier).
    Useful for joining to crime data, NIBRS, Fatal Force datasets, etc.

    Example:
        /api/law-enforcement/agencies/ori/TX0100100
    """
    db = get_db()
    row = db.execute(
        "SELECT * FROM state_local_agencies WHERE ori = ?",
        (ori.upper(),)
    ).fetchone()
    db.close()

    if not row:
        return jsonify({"error": "Not found"}), 404

    return jsonify(dict(row))


@app.route("/api/law-enforcement/agencies/<int:agency_id>/foia", methods=["POST"])
@login_required
def update_le_agency_foia(agency_id):
    """
    Update FOIA contact info for a state/local agency.
    This is how you populate foia_officer, foia_email, etc.
    from your own investigations.

    Body (JSON, all fields optional):
        {
            "foia_officer":   "Jane Smith",
            "foia_email":     "foia@agency.gov",
            "foia_phone":     "512-555-0100",
            "foia_address":   "123 Main St, Austin TX 78701",
            "foia_portal_url":"https://agency.gov/foia",
            "notes":          "Prefers email. Slow responder."
        }
    """
    db = get_db()

    # Verify agency exists
    row = db.execute(
        "SELECT id FROM state_local_agencies WHERE id = ?",
        (agency_id,)
    ).fetchone()

    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404

    data = request.get_json() or {}

    allowed = ["foia_officer", "foia_email", "foia_phone",
               "foia_address", "foia_portal_url", "notes"]

    updates = {k: v for k, v in data.items() if k in allowed}

    if not updates:
        db.close()
        return jsonify({"error": "No valid fields provided"}), 400

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values     = list(updates.values()) + [agency_id]

    db.execute(
        f"UPDATE state_local_agencies SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        tuple(values)
    )
    db.commit()

    # Return updated row
    updated = db.execute(
        "SELECT * FROM state_local_agencies WHERE id = ?",
        (agency_id,)
    ).fetchone()
    db.close()

    return jsonify(dict(updated))


@app.route("/api/law-enforcement/types")
@login_required
def get_le_agency_types():
    """
    List all distinct agency types with counts.
    Useful for populating filter dropdowns in the UI.

    Example response:
        [
            {"agency_type": "City",       "count": 10202},
            {"agency_type": "County",     "count": 2728},
            ...
        ]
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT agency_type, COUNT(*) as count
        FROM state_local_agencies
        GROUP BY agency_type
        ORDER BY count DESC
        """
    ).fetchall()
    db.close()

    return jsonify({"types": [dict(r) for r in rows]})


@app.route("/api/law-enforcement/states")
@login_required
def get_le_states():
    """
    List all states with their agency counts.
    Useful for state picker dropdowns.

    Example response:
        [
            {"state_abbr": "TX", "count": 1512},
            {"state_abbr": "PA", "count": 1165},
            ...
        ]
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT state_abbr, COUNT(*) as count
        FROM state_local_agencies
        GROUP BY state_abbr
        ORDER BY state_abbr
        """
    ).fetchall()
    db.close()

    return jsonify({"states": [dict(r) for r in rows]})

# ─────────────────────────────────────────────
# Routes: State Laws
# ─────────────────────────────────────────────
@app.route("/api/states")
@login_required
def get_states():
    db = get_db()
    rows = db.execute(
        "SELECT id, state_name, state_code, law_name, citation, response_days, appeal_days "
        "FROM state_laws ORDER BY state_name"
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/states/<state_code>")
@login_required
def get_state(state_code):
    db = get_db()
    row = db.execute(
        "SELECT * FROM state_laws WHERE state_code=?", (state_code.upper(),)
    ).fetchone()
    db.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


# ─────────────────────────────────────────────
# Routes: Requests — CRUD
# ─────────────────────────────────────────────
@app.route("/api/requests/new-number")
@login_required
def preview_number():
    """Return what the next FOIA number will be (without consuming it)."""
    year = datetime.now().year
    db = get_db()
    row = db.execute(
        "SELECT last_seq FROM foia_sequence WHERE year=?", (year,)
    ).fetchone()
    db.close()
    seq = (row["last_seq"] if row else 0) + 1
    return jsonify({"foia_number": f"{year}-{seq:03d}", "date": date.today().isoformat()})


@app.route("/api/requests", methods=["POST"])
@subscription_required
def create_request():
    data = request.get_json()
    agency_id    = data.get("agency_id")
    agency_name  = data.get("agency_name", "")
    state_code   = data.get("state_code", None)
    foia_officer = data.get("foia_officer_title", "")

    # Fetch agency name if federal id given
    if agency_id:
        db = get_db()
        ag = db.execute(
            "SELECT name FROM federal_agencies WHERE id=?", (agency_id,)
        ).fetchone()
        db.close()
        if ag:
            agency_name = ag["name"]

    # Normalize state code
    if state_code:
        state_code = state_code.upper()

    foia_number  = next_foia_number()

    # Use user-provided filed date or default to today
    filed_date = data.get("filed_date")
    if filed_date:
        try:
            # Validate the date format
            date.fromisoformat(filed_date)
            created_date = filed_date
        except (ValueError, TypeError):
            created_date = date.today().isoformat()
    else:
        created_date = date.today().isoformat()

    db = get_db()
    req_id = db.insert("""
        INSERT INTO requests
        (user_id, foia_number, created_date, agency_type, agency_id, agency_name,
         state_code, foia_officer_title, subject, notes, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        session["user_id"], foia_number, created_date,
        data.get("agency_type", "Federal"),
        agency_id, agency_name,
        state_code, foia_officer,
        data.get("subject", ""), data.get("notes", ""),
        "new"
    ))
    db.commit()
    db.close()
    log_action(req_id, session["user_id"], "Request created")
    return jsonify({"ok": True, "id": req_id, "foia_number": foia_number})


@app.route("/api/requests")
@subscription_required
def list_requests():
    status_filter = request.args.get("status")
    db = get_db()
    if status_filter:
        rows = db.execute(
            """SELECT r.*, fa.name as agency_display
               FROM requests r
               LEFT JOIN federal_agencies fa ON r.agency_id = fa.id
               WHERE r.user_id=? AND r.status=?
               ORDER BY r.created_at DESC""",
            (session["user_id"], status_filter)
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT r.*, fa.name as agency_display
               FROM requests r
               LEFT JOIN federal_agencies fa ON r.agency_id = fa.id
               WHERE r.user_id=?
               ORDER BY r.created_at DESC""",
            (session["user_id"],)
        ).fetchall()
    db.close()

    result = []
    for r in rows:
        d = dict(r)
        if d.get("deadline_date"):
            d["days_since_deadline"] = days_since(d["deadline_date"])
        result.append(d)
    return jsonify(result)


@app.route("/api/requests/<int:req_id>")
@subscription_required
def get_request(req_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    db.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    if d.get("deadline_date"):
        d["days_since_deadline"] = days_since(d["deadline_date"])
    return jsonify(d)


@app.route("/api/requests/<int:req_id>", methods=["DELETE"])
@subscription_required
def delete_request(req_id):
    db = get_db()
    row = db.execute(
        "SELECT id FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM requests WHERE id=?", (req_id,))
    db.execute("DELETE FROM action_log WHERE request_id=?", (req_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# Routes: Save letter → move to Active
# ─────────────────────────────────────────────
@app.route("/api/requests/<int:req_id>/save-letter", methods=["POST"])
@subscription_required
def save_letter(req_id):
    data = request.get_json()
    letter_text = data.get("letter_text", "")

    db = get_db()
    row = db.execute(
        "SELECT * FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404

    # Calculate deadline
    rd = row["response_days"] or 20
    deadline = add_business_days(row["created_date"], rd)

    db.execute("""
        UPDATE requests SET
            letter_text=?, letter_saved_at=?,
            status='active', response_days=?, deadline_date=?,
            updated_at=?
        WHERE id=?
    """, (
        letter_text, datetime.now().isoformat(sep=" ", timespec="seconds"),
        rd, deadline.isoformat(),
        datetime.now().isoformat(sep=" ", timespec="seconds"),
        req_id
    ))
    db.commit()
    db.close()
    log_action(req_id, session["user_id"], "Letter saved — moved to Active",
               f"Deadline: {deadline.isoformat()}")
    return jsonify({"ok": True, "deadline_date": deadline.isoformat(), "response_days": rd})


# ─────────────────────────────────────────────
# Routes: Generate letter text
# ─────────────────────────────────────────────
@app.route("/api/requests/<int:req_id>/generate-letter")
@subscription_required
def generate_letter(req_id):
    db = get_db()
    row = db.execute(
        "SELECT r.*, fa.foia_officer_title FROM requests r "
        "LEFT JOIN federal_agencies fa ON r.agency_id = fa.id "
        "WHERE r.id=? AND r.user_id=?",
        (req_id, session["user_id"])
    ).fetchone()

    # For state requests, look up state law details
    state_law = None
    if row and row["agency_type"] == "State" and row["state_code"]:
        state_law = db.execute(
            "SELECT * FROM state_laws WHERE state_code=?",
            (row["state_code"],)
        ).fetchone()

    db.close()
    if not row:
        return jsonify({"error": "Not found"}), 404

    if state_law:
        text = build_letter_text(
            row["agency_name"] or "State Agency",
            row["foia_officer_title"] or "Records Custodian",
            row["subject"] or "[Subject of Request]",
            statute=state_law["citation"],
            agency_type="State",
            law_name=state_law["law_name"],
            response_days=state_law["response_days"] or 0
        )
    else:
        text = build_letter_text(
            row["agency_name"] or "Federal Agency",
            row["foia_officer_title"] or "FOIA Officer",
            row["subject"] or "[Subject of Request]"
        )
    return jsonify({"letter_text": text})


# ─────────────────────────────────────────────
# Routes: Update active request
# ─────────────────────────────────────────────
@app.route("/api/requests/<int:req_id>/update", methods=["POST"])
@subscription_required
def update_request(req_id):
    data = request.get_json()
    db = get_db()
    row = db.execute(
        "SELECT id FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404

    fields = []
    values = []
    allowed = [
        "notes", "turn", "secondary_contact_name",
        "secondary_contact_email", "deadline_date", "response_days"
    ]
    for field in allowed:
        if field in data:
            fields.append(f"{field}=?")
            values.append(data[field])

    if not fields:
        db.close()
        return jsonify({"error": "Nothing to update"}), 400

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    fields.append("updated_at=?")
    values.append(now)
    values.append(req_id)

    db.execute(f"UPDATE requests SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    db.close()
    log_action(req_id, session["user_id"], "Request updated",
               ", ".join(k for k in data if k in allowed))
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# Appeal template builder
# ─────────────────────────────────────────────
APPEAL_CLOSING = """Please issue a determination on this appeal within twenty (20) business days as required by 5 U.S.C. § 552(a)(6)(A)(ii). If you uphold the denial, please provide a detailed written explanation citing the specific statutory basis for each withholding so that I may seek judicial review.

Thank you for your attention to this request. I look forward to your response within the statutory timeframe.

Respectfully submitted,"""

STATE_APPEAL_CLOSING = """Please issue a timely determination on this appeal as required by state law. If you uphold the denial, please provide a detailed written explanation citing the specific statutory basis for each withholding so that I may seek further review.

Thank you for your attention to this request. I look forward to your response.

Respectfully submitted,"""


def build_appeal_text(appeal_type, agency_name, foia_officer_title,
                      subject, foia_number, created_date, exemption=None,
                      agency_type="Federal", state_info=None):

    today = date.today().strftime("%B %d, %Y")
    filed = foia_number

    # ─────────────────────────────────────────────
    # Improper Exemption
    # ─────────────────────────────────────────────
    if appeal_type == "improper_exemption":
        exemption_ref = f"Exemption {exemption}" if exemption else "the cited exemption"
        if agency_type in ("State", "Local", "University"):
            law_name = state_info.get("law_name", "state public records law") if state_info else "state public records law"
            body = f"""I am writing to appeal the denial of my public records request, {filed}, submitted on {created_date}, seeking: {subject}

The agency denied my request in whole or in part based on {exemption_ref}. I respectfully challenge this withholding under {law_name}.

Exemptions to public records laws must be construed narrowly. The agency bears the burden of demonstrating that withheld material falls squarely within the claimed exemption.

Furthermore, even if some portions of the responsive records fall within a claimed exemption, the agency is required to segregate and release all non-exempt portions."""
        else:
            body = f"""I am writing to appeal the denial of my Freedom of Information Act request, {filed}, submitted on {created_date}, seeking: {subject}

The agency denied my request in whole or in part based on {exemption_ref}. I respectfully challenge this withholding on the following grounds:

FOIA exemptions must be construed narrowly. See FBI v. Abramson, 456 U.S. 615, 621 (1982). The agency bears the burden of demonstrating that withheld material falls squarely within the claimed exemption.

Furthermore, even if some portions of the responsive records fall within a claimed exemption, the agency is required to segregate and release all non-exempt portions. 5 U.S.C. § 552(b)."""

    # ─────────────────────────────────────────────
    # Constructive Denial
    # ─────────────────────────────────────────────
    elif appeal_type == "constructive_denial":

        # STATE / LOCAL TEMPLATE
        if agency_type in ("State", "Local", "University"):
            body = f"""I am writing to appeal the constructive denial of my public records request, {filed}, submitted on {created_date}, seeking: {subject}

As of the date of this appeal, the agency has failed to issue a determination within the timeframe required by state law. This constitutes a constructive denial."""

        # FEDERAL TEMPLATE
        else:
            body = f"""I am writing to appeal the constructive denial of my Freedom of Information Act request, {filed}, submitted on {created_date}, seeking: {subject}

As of the date of this appeal, the agency has failed to issue a determination within the twenty (20) business day period mandated by 5 U.S.C. § 552(a)(6)(A)(i)."""

    # ─────────────────────────────────────────────
    # Inadequate Search
    # ─────────────────────────────────────────────
    elif appeal_type == "inadequate_search":
        if agency_type in ("State", "Local", "University"):
            law_name = state_info.get("law_name", "state public records law") if state_info else "state public records law"
            body = f"""I am writing to appeal the adequacy of the search conducted in response to my public records request, {filed}, submitted on {created_date}, seeking: {subject}

The agency's response does not demonstrate that it conducted a search reasonably calculated to uncover all responsive documents under {law_name}."""
        else:
            body = f"""I am writing to appeal the adequacy of the search conducted in response to my Freedom of Information Act request, {filed}, submitted on {created_date}, seeking: {subject}

The agency's response does not demonstrate that it conducted a search reasonably calculated to uncover all responsive documents."""

    # ─────────────────────────────────────────────
    # Fee Dispute
    # ─────────────────────────────────────────────
    elif appeal_type == "fee_dispute":
        if agency_type in ("State", "Local", "University"):
            law_name = state_info.get("law_name", "state public records law") if state_info else "state public records law"
            body = f"""I am writing to appeal the fee assessment associated with my public records request, {filed}, submitted on {created_date}, seeking: {subject}

The agency has assessed fees that I challenge under {law_name}."""
        else:
            body = f"""I am writing to appeal the fee assessment associated with my Freedom of Information Act request, {filed}, submitted on {created_date}, seeking: {subject}

The agency has assessed fees that I challenge on multiple grounds."""

    # ─────────────────────────────────────────────
    # Unknown Appeal Type
    # ─────────────────────────────────────────────
    else:
        body = "[Appeal type not recognized]"

    # ─────────────────────────────────────────────
    # HEADER GENERATION
    # ─────────────────────────────────────────────
    if agency_type in ("State", "Local", "University"):
        state_name = state_info.get("state_name", "") if state_info else ""
        appeal_authority = state_info.get("appeal_authority", "Attorney General") if state_info else "Attorney General"
        appeal_address = state_info.get("appeal_address", "") if state_info else ""
        law_name = state_info.get("law_name", "Public Records") if state_info else "Public Records"

        header = f"""{today}

{appeal_authority}
{state_name}
{appeal_address}

Via Electronic Submission

Re: Administrative Appeal of {law_name} Request {foia_number} — {subject}

Dear {appeal_authority.split()[-1]}:"""

    else:
        header = f"""{today}

Director, Office of Information Policy
U.S. Department of Justice
441 G Street NW, Suite 11050
Washington, DC 20530

Via Electronic Submission

Re: Administrative Appeal of FOIA Request {foia_number} — {subject}

Dear Director:"""

    closing = STATE_APPEAL_CLOSING if agency_type in ("State", "Local", "University") else APPEAL_CLOSING
    return f"""{header}

{body}

{closing}

[Your Name]
[Your Address]
[Your Email]
[Your Phone]

Enclosure: Copy of original FOIA request {foia_number} and agency response (if any)"""


# ─────────────────────────────────────────────
# Routes: Appeals
# ─────────────────────────────────────────────
@app.route("/api/requests/<int:req_id>/generate-appeal")
@subscription_required
def generate_appeal(req_id):
    appeal_type = request.args.get("type", "constructive_denial")
    exemption   = request.args.get("exemption", "")

    db = get_db()
    row = db.execute(
        "SELECT * FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()

    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404

    state_info = None

    if row["agency_type"] in ("State", "Local", "University") and row["state_code"]:
        state_row = db.execute(
            "SELECT * FROM state_local_agencies WHERE state_abbr=? AND agency_name=?",
            (row["state_code"], row["agency_name"])
        ).fetchone()

        if state_row:
            state_info = dict(state_row)
        else:
            # Fall back to state_laws for state-level info (law name, state name)
            law_row = db.execute(
                "SELECT * FROM state_laws WHERE state_code=?",
                (row["state_code"],)
            ).fetchone()
            if law_row:
                state_info = dict(law_row)

    db.close()

    text = build_appeal_text(
        appeal_type=appeal_type,
        agency_name=row["agency_name"] or "Agency",
        foia_officer_title=row["foia_officer_title"] or "Records Custodian",
        subject=row["subject"] or "[Subject]",
        foia_number=row["foia_number"],
        created_date=row["created_date"] or "",
        exemption=exemption or row["appeal_exemption"],
        agency_type=row["agency_type"],
        state_info=state_info
    )
    return jsonify({"appeal_text": text})

# ─────────────────────────────────────────────
# Routes: Attachments
# ─────────────────────────────────────────────
@app.route("/api/requests/<int:req_id>/attachments")
@subscription_required
def list_attachments(req_id):
    db = get_db()
    row = db.execute(
        "SELECT id FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404
    files = db.execute(
        "SELECT * FROM request_attachments WHERE request_id=? ORDER BY uploaded_at DESC",
        (req_id,)
    ).fetchall()
    db.close()
    return jsonify([dict(f) for f in files])


@app.route("/api/requests/<int:req_id>/attachments", methods=["POST"])
@subscription_required
def upload_attachment(req_id):
    db = get_db()
    row = db.execute(
        "SELECT id FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404
    db.close()

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400

    # Size check
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"File exceeds {MAX_UPLOAD_MB}MB limit"}), 400

    # Sanitize filename and store with unique prefix
    import uuid
    original_name = f.filename
    ext = os.path.splitext(original_name)[1].lower()
    safe_name = f"{uuid.uuid4().hex}{ext}"

    # Store in uploads/user_id/req_id/
    dest_dir = os.path.join(UPLOAD_FOLDER, str(session["user_id"]), str(req_id))
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, safe_name)
    f.save(dest_path)

    label = request.form.get("label", "").strip()
    now = datetime.now().isoformat(sep=" ", timespec="seconds")

    db = get_db()
    att_id = db.insert("""
        INSERT INTO request_attachments
            (request_id, user_id, filename, original_name, file_size, mime_type, label, uploaded_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (req_id, session["user_id"], safe_name, original_name, size,
          f.content_type or "", label, now))
    db.commit()
    db.close()

    log_action(req_id, session["user_id"], "Attachment uploaded", original_name)
    return jsonify({"ok": True, "id": att_id, "original_name": original_name, "file_size": size})


@app.route("/api/attachments/<int:att_id>/download")
@subscription_required
def download_attachment(att_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM request_attachments WHERE id=? AND user_id=?",
        (att_id, session["user_id"])
    ).fetchone()
    db.close()
    if not row:
        abort(404)

    path = os.path.join(UPLOAD_FOLDER, str(session["user_id"]),
                        str(row["request_id"]), row["filename"])
    if not os.path.exists(path):
        abort(404)

    return send_file(path, as_attachment=True, download_name=row["original_name"])


@app.route("/api/attachments/<int:att_id>", methods=["DELETE"])
@subscription_required
def delete_attachment(att_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM request_attachments WHERE id=? AND user_id=?",
        (att_id, session["user_id"])
    ).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404

    path = os.path.join(UPLOAD_FOLDER, str(session["user_id"]),
                        str(row["request_id"]), row["filename"])
    if os.path.exists(path):
        os.remove(path)

    db.execute("DELETE FROM request_attachments WHERE id=?", (att_id,))
    db.commit()
    db.close()
    log_action(row["request_id"], session["user_id"], "Attachment deleted", row["original_name"])
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# Routes: Action log
# ─────────────────────────────────────────────
@app.route("/api/requests/<int:req_id>/log")
@subscription_required
def get_action_log(req_id):
    db = get_db()
    row = db.execute(
        "SELECT id FROM requests WHERE id=? AND user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "Not found"}), 404
    logs = db.execute(
        "SELECT * FROM action_log WHERE request_id=? ORDER BY logged_at DESC",
        (req_id,)
    ).fetchall()
    db.close()
    return jsonify([dict(l) for l in logs])


# ─────────────────────────────────────────────
# Routes: Word doc download
# ─────────────────────────────────────────────
@app.route("/api/requests/<int:req_id>/download-docx")
@subscription_required
def download_docx(req_id):
    """Generate and stream a .docx letter for a request."""
    import subprocess, tempfile, json as _json

    db = get_db()
    row = db.execute(
        "SELECT r.*, fa.foia_officer_title FROM requests r "
        "LEFT JOIN federal_agencies fa ON r.agency_id = fa.id "
        "WHERE r.id=? AND r.user_id=?",
        (req_id, session["user_id"])
    ).fetchone()
    db.close()
    if not row:
        return jsonify({"error": "Not found"}), 404

    letter_text = row["letter_text"] or build_letter_text(
        row["agency_name"] or "Federal Agency",
        row["foia_officer_title"] or "FOIA Officer",
        row["subject"] or "[Subject]"
    )

    tmpdir = tempfile.mkdtemp()
    out_path = os.path.join(tmpdir, f"{row['foia_number']}.docx")
    script = os.path.join(os.path.dirname(__file__), "generate_letter.js")

    payload = _json.dumps({
        "foia_number":  row["foia_number"],
        "agency_name":  row["agency_name"] or "Federal Agency",
        "officer_title": row["foia_officer_title"] or "FOIA Officer",
        "subject":      row["subject"] or "[Subject]",
        "output":       out_path
    })

    try:
        result = subprocess.run(
            ["node", script, payload],
            capture_output=True, text=True, timeout=15
        )
        res = _json.loads(result.stdout)
        if not res.get("ok"):
            return jsonify({"error": res.get("error", "Generation failed")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    log_action(req_id, session["user_id"], "Word doc downloaded")
    return send_file(
        out_path,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=f"{row['foia_number']}.docx"
    )


# ─────────────────────────────────────────────
# Serve static assets (icons, img, manifest)
# ─────────────────────────────────────────────
@app.route("/icons/<path:filename>")
def serve_icons(filename):
    return send_file(os.path.join(os.path.dirname(__file__), "icons", filename))

@app.route("/img/<path:filename>")
def serve_img(filename):
    return send_file(os.path.join(os.path.dirname(__file__), "img", filename))

@app.route("/manifest.json")
def serve_manifest():
    return send_file(os.path.join(os.path.dirname(__file__), "manifest.json"))

# ─────────────────────────────────────────────
# Serve frontend (catch-all — must be last)
# ─────────────────────────────────────────────
@app.route("/")
@app.route("/<path:path>")
def serve_frontend(path=""):
    return send_file(os.path.join(os.path.dirname(__file__), "index.html"))

# Initialize database on startup
with app.app_context():
    init_db()

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)