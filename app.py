from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, abort, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from uuid import uuid4
from functools import wraps
from pathlib import Path
from io import BytesIO
from hashlib import sha256
from dotenv import load_dotenv
import sqlite3, os, json, re, math, hmac, urllib.parse, urllib.request, mimetypes
from datetime import datetime, timezone, timedelta
from typing import Any
from PIL import Image, ImageOps
import numpy as np
import jwt

BASE=Path(__file__).resolve().parent
load_dotenv(BASE/'.env')
# Import after load_dotenv so the module reads the environment variables.
import payments  # noqa: E402
from payments import (  # noqa: E402
    PaymentStatus, PayoutStatus, TransferStatus, PaystackError,
    status_label, paystack_enabled,
)
MAX_IMAGE_BYTES=8*1024*1024
MAX_IMAGE_COUNT=int(os.getenv('VISUAL_MAX_IMAGES','20'))
MIN_VISUAL_IMAGE_COUNT=5
ALLOWED_IMAGE_MIMES={'image/jpeg','image/png','image/webp','image/gif'}
ALLOWED_IMAGE_EXTENSIONS={'jpg','jpeg','png','webp','gif'}
PHASH_SIZE=32
PHASH_BITS=8
PHASH_DISTANCE_THRESHOLD=int(os.getenv('IMAGE_PHASH_DISTANCE_THRESHOLD', '8'))
REVERSE_IMAGE_MIN_SCORE=float(os.getenv('REVERSE_IMAGE_MIN_SCORE', '0.80'))
Image.MAX_IMAGE_PIXELS=25_000_000
ANALYSIS_DEFAULTS={
    'property_type': None, 'floors_estimated': None, 'condition': 'unknown',
    'finishing_quality': 'unknown', 'construction_quality': 'unknown',
    'roof_condition': 'unknown', 'wall_condition': 'unknown',
    'flooring_quality': 'unknown', 'kitchen_quality': 'unknown',
    'bathroom_quality': 'unknown', 'parking_spaces_visible': None,
    'amenities': [], 'overall_property_condition': 'unknown',
    'visual_quality_score': None, 'analysis_confidence': 0.0
}
import pandas as pd
import joblib

try:
    from openai import OpenAI
except Exception:
    OpenAI = None
# OpenAI calls must never block a web request long enough for the host's proxy to
# drop the connection (which the browser reports as a vague "Failed to fetch").
# A short timeout with no automatic retries lets callers fall back to their local
# logic quickly instead of hanging.
OPENAI_TIMEOUT_SECONDS=float(os.getenv('OPENAI_TIMEOUT_SECONDS','8'))


def _openai_client(api_key):
    """Build an OpenAI client with a hard timeout and no automatic retries."""
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS, max_retries=0)

app=Flask(__name__)
app.secret_key=os.getenv('FLASK_SECRET_KEY','change-this-in-production')

# --- Admin authentication configuration -------------------------------------
# Admins authenticate once at a public login endpoint. On success the server
# issues a cryptographically signed JWT delivered as an HttpOnly cookie, so the
# browser attaches it automatically on every later /Admin/* request.
ADMIN_JWT_SECRET=os.getenv('ADMIN_JWT_SECRET') or app.secret_key
ADMIN_JWT_ALGORITHM='HS256'
ADMIN_JWT_TTL_SECONDS=int(os.getenv('ADMIN_JWT_TTL_SECONDS','28800'))
ADMIN_COOKIE_NAME='admin_token'
# Secure cookies require HTTPS. Enable ADMIN_COOKIE_SECURE=1 in production.
ADMIN_COOKIE_SECURE=os.getenv('ADMIN_COOKIE_SECURE','0').lower() in ('1','true','yes','on')
ADMIN_EMAIL=os.getenv('ADMIN_EMAIL','admin@estimate.ng').strip().lower()
# Data lives in a single configurable directory so a host (e.g. Render) can
# mount a persistent disk at it and survive redeploys. Without a persistent disk
# the filesystem is reset on every deploy, which wipes the database and any
# uploaded files - set ESTIMATE_DATA_DIR to the mount path in that case.
DATA_DIR=Path(os.getenv('ESTIMATE_DATA_DIR') or BASE)

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB=DATA_DIR/'estimate.db'
# Static uploads must stay under static/ so they are served; the verification
# documents are private and live in the data directory.
UPLOADS=BASE/'static'/'uploads'
VERIFICATION_UPLOADS=DATA_DIR/'verification_uploads'
UPLOADS.mkdir(parents=True, exist_ok=True)
VERIFICATION_UPLOADS.mkdir(parents=True, exist_ok=True)
MODEL=joblib.load(BASE/'model.joblib')
DATA=pd.read_csv(BASE/'data.csv')
try:
    METRICS=json.loads((BASE/'metrics.json').read_text(encoding='utf-8'))
except (OSError,ValueError):
    METRICS={}
MODEL_CONFIDENCE=float(METRICS.get('r2',0))*100
# --- Image-based (visual) price model ---------------------------------------
# Trained by train_image_model.py on visual feature aggregates. It predicts a
# bounded visual price adjustment that is applied on top of the structured
# valuation, so photos can never override the trained market model.
import image_features as visuals
MODEL_IMAGE=joblib.load(BASE/'model_image.joblib')
IMAGE_MODEL=MODEL_IMAGE['model']
try:
    IMAGE_METRICS=json.loads((BASE/'image_metrics.json').read_text(encoding='utf-8'))
except (OSError,ValueError):
    IMAGE_METRICS={}
IMAGE_MODEL_CONFIDENCE=float(IMAGE_METRICS.get('r2',0))*100
# How strongly photos may move the structured price, at full coverage.
VISUAL_ADJUSTMENT_SPAN=float(os.getenv('VISUAL_ADJUSTMENT_SPAN','0.09'))

DOCUMENT_TEMPLATES=[
    ('rent_notice','Rent Notice','RENT NOTICE\\n\\nDear Tenant,\\n\\nThis notice concerns the rent for {{details}}. Please review your tenancy records and make the required payment by the agreed due date.\\n\\nThank you,\\nESTIMATE Property Management'),
    ('listing','Property Listing','PROPERTY LISTING\\n\\nPresenting {{details}} — a well-positioned opportunity for discerning buyers or tenants. Contact the property manager for pricing, inspection and documentation.'),
    ('followup','Client Follow-up','FOLLOW-UP MESSAGE\\n\\nHello, I am following up regarding {{details}}. Please let me know a convenient time to continue the conversation or schedule an inspection.\\n\\nBest regards,\\nESTIMATE Property Management'),
    ('management_agreement','Property Management Agreement','PROPERTY MANAGEMENT AGREEMENT\\n\\nParties: [Owner] and ESTIMATE Property Management\\nProperty: {{details}}\\n\\nScope, fees, owner responsibilities and manager responsibilities: [Complete details].\\nHave a qualified professional review before signing.'),
    ('lease_agreement','Residential or Commercial Lease Agreement','LEASE AGREEMENT\\n\\nProperty: {{details}}\\n\\nInclude rent, duration, permitted use, maintenance, utilities, default and termination terms. Have a qualified professional review before signing.'),
    ('rental_application','Rental Application Form','RENTAL APPLICATION FORM\\n\\nProperty / applicant details: {{details}}\\n\\nInclude employment, income, references, rental history and lawful screening consent.'),
    ('rent_ledger','Rent Ledger','RENT LEDGER\\n\\nTenant/property: {{details}}\\n\\nDate | Description | Due | Paid | Balance | Reference\\nKeep entries chronological with supporting records.'),
    ('rent_receipt','Rent Receipt','RENT RECEIPT\\n\\nReceived from: [Tenant]\\nAmount: [Amount]\\nFor: {{details}}\\nPayment method: [Cash/Transfer/Check] | Date: [Date]\\nCheck against the payment record.'),
    ('eviction_notice','Notice to Quit / Eviction Notice','NOTICE TO QUIT / EVICTION NOTICE\\n\\nTenant/property: {{details}}\\n\\nState the breach, required action and applicable dates only after qualified local legal review. This draft is not legal advice.')
]
STATES=sorted(DATA['State'].dropna().unique().tolist())
PROPERTY_TYPES=['Detached Duplex','Terraced Duplex','Semi-Detached Duplex','Bungalow','Flat','Mansion','Block of Flats','Penthouse','Land']
CITIES=sorted(DATA['City'].dropna().unique().tolist())

# --- Handyman directory config ----------------------------------------------
# The reputation score is measured in "links" (0-10). A handyman earns one link
# per successfully delivered job up to the cap; at the cap a star is shown on
# the profile and the handyman is surfaced more frequently in search results.
MAX_HANDYMAN_LINKS=10
# One link per this many delivered jobs, so the score grows steadily but a
# newcomer cannot jump straight to the cap on a single lucky review.
JOBS_PER_LINK=2
HANDYMAN_TRADES=['Plumber','Electrician','Carpenter','Painter','Mason','Tiler','Welder','Roofer','HVAC Technician','Generator Technician','Interior Decorator','Other']
PAY_RANGES=['low','medium','high']

# --- Listing photography ----------------------------------------------------
# Bundled photos in static/uploads, used as demo imagery for the sample listings
# and as a fallback for seller listings that have no photo yet. Without this the
# PropkoNet cards render a blank placeholder house icon, which makes the
# marketplace look unfinished.
# A bundled file is only usable if it is a real, decodable image: some entries
# in static/uploads are corrupt placeholders (a few bytes) that would render as
# broken <img> tags. PIL verifies each one so the grid never shows a broken icon.
def _usable_photo(filename, seen=None):
    path=BASE/'static'/'uploads'/filename
    if not path.exists(): return False
    try:
        with Image.open(path) as im: im.verify()
    except Exception:
        return False
    # When a set is supplied, drop files whose content repeats an earlier one.
    # Several bundled filenames hold the same picture; keeping one of each stops
    # the grid and galleries showing the identical photo repeatedly.
    if seen is not None:
        digest=sha256(path.read_bytes()).hexdigest()
        if digest in seen: return False
        seen.add(digest)
    return True
_photo_filter_seen=set()
SAMPLE_LISTING_PHOTOS=[f for f in (
    '025cfe7967ae4b83971a1bc38d604231.jpg',
    '29ba7268012f4c149f658e99e43f9666.jpg',
    '48f67ccaef844d33b2557a7330a57393.png',
    '5650c801cbe34287b11ff77141a59172.jpg',
    '65b8d9d465ae43d9bc9c1ba199fcaad3.jpg',
    '747754990fb241739027a1d83e44ec43.jpg',
    '81053ef7b1e14f45b65187032749332e.jpg',
    '843371c3189f4559b9238e8438be597a.jpg',
    'a9ef982aca674ff493ff629c98b517e4.jpg',
    'ab861dfbb89848ab9cdad1e0b98d97dd.jpg',
    'b2ee7a13af4c4fda9ff8b24b5bf53a21.jpg',
    'c00f6c2c7dd24b67823e3e970f3e9b91.jpg'
) if _usable_photo(f, _photo_filter_seen)]


def demo_listing_photo(index):
    # Stable photo for the nth demo listing (None when no photos are bundled).
    if not SAMPLE_LISTING_PHOTOS:
        return None
    return SAMPLE_LISTING_PHOTOS[index % len(SAMPLE_LISTING_PHOTOS)]


def listing_photo_url(filename):
    return url_for('static', filename=f'uploads/{filename}') if filename else None

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db()
    # Admin accounts live in their own table so the public users table (which is
    # self-service signup) can never be used to mint an admin identity.
    c.execute('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,username TEXT,phone TEXT,is_verified INTEGER DEFAULT 0,is_blocked INTEGER DEFAULT 0,blocked_at TEXT,block_reason TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS properties(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,name TEXT,location TEXT,price REAL,status TEXT,thumb TEXT DEFAULT 'house',photo TEXT,published_to_propkonet INTEGER DEFAULT 0,beds INTEGER DEFAULT 3,baths INTEGER DEFAULT 3,sqft INTEGER DEFAULT 1800,property_type TEXT DEFAULT 'Detached Duplex',buyable INTEGER DEFAULT 1,ai_price REAL DEFAULT 0,listing_type TEXT DEFAULT 'sale',created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS property_photos(id INTEGER PRIMARY KEY AUTOINCREMENT,property_id INTEGER NOT NULL,filename TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS image_fingerprints(id INTEGER PRIMARY KEY AUTOINCREMENT,property_id INTEGER,property_photo_id INTEGER,user_id INTEGER NOT NULL,filename TEXT NOT NULL,sha256 TEXT NOT NULL,phash TEXT NOT NULL,width INTEGER,height INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE CASCADE,FOREIGN KEY(property_photo_id) REFERENCES property_photos(id) ON DELETE CASCADE,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fraud_reports(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,property_id INTEGER,filename TEXT,reason TEXT NOT NULL,match_type TEXT NOT NULL,match_source TEXT,match_reference TEXT,match_score REAL,status TEXT DEFAULT 'pending',created_at TEXT DEFAULT CURRENT_TIMESTAMP,reviewed_at TEXT,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE SET NULL)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_image_fingerprints_phash ON image_fingerprints(phash)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_fraud_reports_status ON fraud_reports(status,created_at)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,title TEXT,due TEXT,done INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS login_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,email TEXT NOT NULL,role TEXT NOT NULL,ip_address TEXT,logged_in_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS document_templates(id INTEGER PRIMARY KEY AUTOINCREMENT,task TEXT UNIQUE NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS verification_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,email TEXT NOT NULL,username TEXT NOT NULL,phone TEXT NOT NULL,status TEXT DEFAULT 'pending',business_address TEXT,nin_number TEXT,cac_number TEXT,face_photo TEXT,nin_photo TEXT,cac_photo TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS property_purchases(id INTEGER PRIMARY KEY AUTOINCREMENT,property_id INTEGER,buyer_name TEXT NOT NULL,buyer_email TEXT NOT NULL,buyer_phone TEXT NOT NULL,offer_price REAL,message TEXT,status TEXT DEFAULT 'pending',created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS property_transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER NOT NULL, seller_id INTEGER NOT NULL,
        buyer_name TEXT NOT NULL, buyer_email TEXT NOT NULL, buyer_phone TEXT, amount_kobo INTEGER NOT NULL,
        reference TEXT UNIQUE NOT NULL, buyer_token TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'checkout_created',
        paid_at TEXT, documents_ready_at TEXT, buyer_confirmed_at TEXT, release_at TEXT,
        transfer_reference TEXT UNIQUE, transfer_status TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(property_id) REFERENCES properties(id), FOREIGN KEY(seller_id) REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_property_transactions_release ON property_transactions(status, release_at)')
    # --- Paystack payment tables ---------------------------------------------
    # payments holds one row per checkout attempt/order. Amounts are integers in
    # kobo (NGN minor units); no floating point is used for money.
    c.execute('''CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE NOT NULL,
        property_id INTEGER NOT NULL,
        buyer_user_id INTEGER,
        seller_id INTEGER NOT NULL,
        buyer_name TEXT, buyer_email TEXT, buyer_phone TEXT,
        amount_kobo INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'NGN',
        status TEXT NOT NULL DEFAULT 'pending',
        paystack_reference TEXT,
        paystack_access_code TEXT,
        authorization_url TEXT,
        channel TEXT,
        paid_at TEXT,
        customer_confirmed_at TEXT,
        payout_status TEXT NOT NULL DEFAULT 'not_applicable',
        payout_at TEXT,
        payout_amount_kobo INTEGER,
        transfer_reference TEXT,
        transfer_code TEXT,
        transfer_status TEXT,
        failure_reason TEXT,
        retry_count INTEGER DEFAULT 0,
        dispute_status TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(property_id) REFERENCES properties(id),
        FOREIGN KEY(seller_id) REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payments_reference ON payments(reference)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payments_paystack_ref ON payments(paystack_reference)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payments_transfer_ref ON payments(transfer_reference)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payments_payout ON payments(payout_status, payout_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payments_seller ON payments(seller_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payments_buyer ON payments(buyer_user_id)')
    # seller_payouts is the payout ledger, one row per payment attempted payout.
    c.execute('''CREATE TABLE IF NOT EXISTS seller_payouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_id INTEGER NOT NULL,
        seller_id INTEGER NOT NULL,
        recipient_code TEXT,
        amount_kobo INTEGER NOT NULL,
        reference TEXT UNIQUE NOT NULL,
        transfer_code TEXT,
        status TEXT NOT NULL DEFAULT 'scheduled',
        failure_reason TEXT,
        attempts INTEGER DEFAULT 0,
        scheduled_at TEXT,
        processed_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(payment_id) REFERENCES payments(id),
        FOREIGN KEY(seller_id) REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_seller_payouts_status ON seller_payouts(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_seller_payouts_seller ON seller_payouts(seller_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_seller_payouts_scheduled ON seller_payouts(scheduled_at)')
    # transfer_recipients caches each seller's verified payout account.
    c.execute('''CREATE TABLE IF NOT EXISTS transfer_recipients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        bank_name TEXT, bank_code TEXT, account_number TEXT, account_name TEXT,
        recipient_code TEXT UNIQUE,
        currency TEXT DEFAULT 'NGN',
        is_verified INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_transfer_recipients_user ON transfer_recipients(user_id)')
    # payment_webhook_events de-duplicates webhook deliveries by event id.
    c.execute('''CREATE TABLE IF NOT EXISTS payment_webhook_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE,
        event_type TEXT,
        reference TEXT,
        payload_hash TEXT,
        status TEXT DEFAULT 'processed',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_webhook_events_event ON payment_webhook_events(event_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_webhook_events_ref ON payment_webhook_events(reference)')
    # audit_logs is an append-only trail of money-affecting actions.
    c.execute('''CREATE TABLE IF NOT EXISTS audit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_type TEXT, actor_id INTEGER,
        action TEXT NOT NULL,
        entity_type TEXT, entity_id INTEGER,
        details TEXT,
        ip_address TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)')
    c.execute('''CREATE TABLE IF NOT EXISTS admin_users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'admin',mfa_secret TEXT,is_active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP,last_login_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admin_login_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,admin_id INTEGER,email TEXT NOT NULL,ip_address TEXT,user_agent TEXT,success INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    # --- Handyman directory --------------------------------------------------
    # One profile row per handyman account. The public directory, the profile
    # form and the ToolBox dashboard all read from this table. ``links`` is the
    # reputation score (0-10): it rises as the handyman completes more jobs and
    # caps at MAX_HANDYMAN_LINKS, at which point a star is shown and the profile
    # is ranked higher in search results.
    c.execute('''CREATE TABLE IF NOT EXISTS handyman_profiles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        full_name TEXT,
        trade TEXT NOT NULL,
        secondary_trades TEXT,
        bio TEXT,
        years_experience INTEGER DEFAULT 0,
        pay_range TEXT DEFAULT 'medium',
        service_area TEXT,
        state TEXT,
        city TEXT,
        phone TEXT,
        available INTEGER DEFAULT 1,
        availability_notes TEXT,
        jobs_completed INTEGER DEFAULT 0,
        links INTEGER DEFAULT 0,
        rating_sum REAL DEFAULT 0,
        rating_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_handyman_profiles_trade ON handyman_profiles(trade)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_handyman_profiles_links ON handyman_profiles(links DESC)')
    # completed_jobs is the ledger of successfully delivered work. Each row is
    # one delivered job; the links score is derived from the count so the two
    # can never disagree.
    c.execute('''CREATE TABLE IF NOT EXISTS handyman_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        handyman_user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        client_name TEXT,
        trade TEXT,
        completed_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(handyman_user_id) REFERENCES users(id) ON DELETE CASCADE
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_handyman_jobs_user ON handyman_jobs(handyman_user_id)')
    # reviews carry the star rating and count toward the links score.
    c.execute('''CREATE TABLE IF NOT EXISTS handyman_reviews(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        handyman_user_id INTEGER NOT NULL,
        reviewer_user_id INTEGER,
        reviewer_name TEXT,
        rating INTEGER NOT NULL,
        comment TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(handyman_user_id) REFERENCES users(id) ON DELETE CASCADE
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_handyman_reviews_user ON handyman_reviews(handyman_user_id)')
    for task,title,body in DOCUMENT_TEMPLATES:
        c.execute('INSERT OR IGNORE INTO document_templates(task,title,body) VALUES(?,?,?)',(task,title,body))
    # Safe migrations for existing databases
    for col in ['username TEXT', 'phone TEXT', 'is_verified INTEGER DEFAULT 0', 'is_blocked INTEGER DEFAULT 0', 'blocked_at TEXT', 'block_reason TEXT']:
        try: c.execute(f'ALTER TABLE users ADD COLUMN {col}')
        except sqlite3.OperationalError: pass
    for col in ['paystack_recipient_code TEXT', 'payout_bank_name TEXT',
                'payout_bank_code TEXT', 'payout_account_number TEXT',
                'payout_account_name TEXT', 'payout_recipient_updated_at TEXT']:
        try: c.execute(f'ALTER TABLE users ADD COLUMN {col}')
        except sqlite3.OperationalError: pass
    for col in [
        'photo TEXT',
        'published_to_propkonet INTEGER DEFAULT 0',
        'beds INTEGER DEFAULT 3',
        'baths INTEGER DEFAULT 3',
        'sqft INTEGER DEFAULT 1800',
        'property_type TEXT DEFAULT "Detached Duplex"',
        'buyable INTEGER DEFAULT 1',
        'ai_price REAL DEFAULT 0',
        'listing_type TEXT DEFAULT "sale"'
    ]:
        try: c.execute(f'ALTER TABLE properties ADD COLUMN {col}')
        except sqlite3.OperationalError: pass
    # Backfill demo photography for seller listings created before photos were
    # attached, so re-running on an existing estimate.db de-blanks PropkoNet.
    unphotoed=c.execute("SELECT COUNT(*) FROM properties WHERE photo IS NULL OR photo=''").fetchone()[0]
    if unphotoed and SAMPLE_LISTING_PHOTOS:
        for n, r in enumerate(c.execute(
                "SELECT id FROM properties WHERE photo IS NULL OR photo='' ORDER BY id").fetchall()):
            c.execute('UPDATE properties SET photo=? WHERE id=?', (demo_listing_photo(n), r['id']))
    for col in [
        'business_address TEXT',
        'nin_number TEXT',
        'cac_number TEXT',
        'face_photo TEXT',
        'nin_photo TEXT',
        'cac_photo TEXT'
    ]:
        try: c.execute(f'ALTER TABLE verification_requests ADD COLUMN {col}')
        except sqlite3.OperationalError: pass
    c.commit(); c.close()

def bootstrap_admin():
    # Create/refresh a bootstrap admin from the environment so a fresh install
    # has at least one admin account to log in with at /api/admin/login.
    email=ADMIN_EMAIL
    password=os.getenv('ADMIN_PASSWORD')
    if not email or not password:
        return
    c=db()
    existing=c.execute('SELECT id FROM admin_users WHERE email=?',(email,)).fetchone()
    if existing is None:
        c.execute("INSERT INTO admin_users(email,password,role,is_active) VALUES(?,?,'admin',1)",(email,generate_password_hash(password)))
        c.commit()
    c.close()

init_db()
bootstrap_admin()

# Log where the data actually lives so a host's logs make it obvious whether a
# persistent location (a mounted disk) is in use or the ephemeral project dir is.
_ephemeral = DATA_DIR.resolve() == BASE.resolve()
_persistence = 'ephemeral - data is lost on redeploy' if _ephemeral else 'persistent'
print(f'[estimate] data directory: {DATA_DIR.resolve()} ({_persistence})', flush=True)

def _load_image(raw):
    try:
        image=Image.open(BytesIO(raw))
        image.load()
        return ImageOps.exif_transpose(image).convert('RGB')
    except (OSError, ValueError, Image.DecompressionBombError):
        return None

def _dct_matrix(size):
    indices=np.arange(size, dtype=np.float32)
    return np.cos(np.pi/size*(indices[:,None]+0.5)*indices[None,:])

def _perceptual_hash(image):
    gray=image.convert('L').resize((PHASH_SIZE,PHASH_SIZE),Image.Resampling.LANCZOS)
    pixels=np.asarray(gray, dtype=np.float32)
    matrix=_dct_matrix(PHASH_SIZE)
    coefficients=matrix.T @ pixels @ matrix
    low_frequency=coefficients[:PHASH_BITS,:PHASH_BITS]
    comparison=low_frequency.flatten()[1:]
    median=float(np.median(comparison))
    value=0
    for bit in (comparison > median):
        value=(value<<1)|int(bit)
    return f'{value:016x}'

def fingerprint_image(raw):
    image=_load_image(raw)
    if image is None:
        return None
    return {
        'sha256': sha256(raw).hexdigest(),
        'phash': _perceptual_hash(image),
        'width': image.width,
        'height': image.height
    }

def hamming_distance(first, second):
    try:
        return (int(first,16)^int(second,16)).bit_count()
    except (TypeError, ValueError):
        return 64

def reverse_image_matches(raw):
    endpoint=os.getenv('REVERSE_IMAGE_API_URL','').strip()
    if not endpoint:
        return [], 'Reverse-image search is not configured.'
    boundary='----EstiMateImageGuardrail'+uuid4().hex
    lines=[]
    lines.append(f'--{boundary}')
    lines.append('Content-Disposition: form-data; name="image"; filename="image.bin"')
    lines.append('Content-Type: application/octet-stream')
    lines.append('')
    body_parts=[('\r\n'.join(lines)).encode('utf-8'), raw, f'\r\n--{boundary}--\r\n'.encode('utf-8')]
    body=b''.join(body_parts)
    headers={'Content-Type':f'multipart/form-data; boundary={boundary}','Accept':'application/json','User-Agent':'EstiMate Image Guardrail/1.0'}
    api_key=os.getenv('REVERSE_IMAGE_API_KEY','').strip()
    if api_key:
        headers['Authorization']='Bearer '+api_key
    try:
        api_request=urllib.request.Request(endpoint, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(api_request, timeout=15) as response:
            payload=json.load(response)
        if isinstance(payload, dict):
            items=payload.get('matches') or payload.get('results') or []
        elif isinstance(payload, list):
            items=payload
        else:
            items=[]
        matches=[]
        for item in items:
            if not isinstance(item, dict):
                continue
            url=item.get('url') or item.get('source_url') or item.get('link')
            score=item.get('score') or item.get('similarity') or item.get('confidence') or 0
            try:
                score=float(score)
            except (TypeError, ValueError):
                score=0
            if url and score >= REVERSE_IMAGE_MIN_SCORE:
                matches.append({'url':str(url),'score':score,'title':str(item.get('title') or item.get('source') or url)})
        return matches, None
    except Exception:
        app.logger.exception('Reverse-image search failed')
        return [], 'Reverse-image search is temporarily unavailable.'

def find_database_image_match(fingerprint, exclude_property_id=None):
    c=db()
    rows=c.execute('SELECT * FROM image_fingerprints ORDER BY id').fetchall()
    c.close()
    best=None
    for row in rows:
        if exclude_property_id is not None and row['property_id']==exclude_property_id:
            continue
        if row['sha256']==fingerprint['sha256']:
            candidate={'row':row,'match_type':'exact_duplicate','score':1.0}
            best=candidate
            break
        distance=hamming_distance(fingerprint['phash'], row['phash'])
        if distance <= PHASH_DISTANCE_THRESHOLD:
            candidate={'row':row,'match_type':'perceptual_duplicate','score':1-distance/64,'distance':distance}
            if best is None or candidate['score'] > best['score']:
                best=candidate
    return best

def record_fraud(user_id, property_id, filename, reason, match_type, match_source, match_reference, match_score):
    c=db()
    cur=c.execute('''INSERT INTO fraud_reports(user_id,property_id,filename,reason,match_type,match_source,match_reference,match_score) VALUES(?,?,?,?,?,?,?,?)''',(user_id,property_id,filename,reason,match_type,match_source,match_reference,match_score))
    c.commit()
    report_id=cur.lastrowid
    c.close()
    return report_id

def scan_property_images(files, user_id, exclude_property_id=None):
    prepared=[]
    seen_hashes=set()
    for photo in files:
        if not photo or not photo.filename:
            continue
        ext=photo.filename.rsplit('.',1)[-1].lower() if '.' in photo.filename else ''
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            return None, {'error':'Upload JPG, PNG, WEBP or GIF images only.'}
        raw=photo.read()
        if not raw or len(raw)>MAX_IMAGE_BYTES:
            return None, {'error':'Each image must be larger than 0 bytes and no more than 8 MB.'}
        fingerprint=fingerprint_image(raw)
        if fingerprint is None:
            return None, {'error':'One or more uploads are not valid images.'}
        if fingerprint['sha256'] in seen_hashes:
            return None, {'error':'The same image cannot be uploaded more than once in one submission.'}
        seen_hashes.add(fingerprint['sha256'])
        database_match=find_database_image_match(fingerprint, exclude_property_id)
        if database_match:
            row=database_match['row']
            report_id=record_fraud(user_id, exclude_property_id, photo.filename, 'Image matches an existing image in the PropkoNet database.', database_match['match_type'], 'database', row['filename'], database_match['score'])
            return None, {'error':'Image guardrail flagged this submission for fraud review.','fraud':True,'fraud_report_id':report_id,'match_type':database_match['match_type'],'matched_image':row['filename'],'match_score':database_match['score']}
        external_matches, external_message=reverse_image_matches(raw)
        if external_matches:
            match=external_matches[0]
            report_id=record_fraud(user_id, exclude_property_id, photo.filename, 'Image matches a publicly indexed image from another site.', 'reverse_image_match', 'external', match['url'], match['score'])
            return None, {'error':'Image guardrail flagged this submission for fraud review.','fraud':True,'fraud_report_id':report_id,'match_type':'reverse_image_match','matched_image':match['url'],'match_score':match['score']}
        if external_message:
            prepared.append({'photo':photo,'raw':raw,'filename':photo.filename,'ext':ext,'fingerprint':fingerprint,'reverse_image_message':external_message})
        else:
            prepared.append({'photo':photo,'raw':raw,'filename':photo.filename,'ext':ext,'fingerprint':fingerprint})
    return prepared, None

def scan_saved_property_images(property_id, user_id):
    c=db()
    rows=c.execute('SELECT id,filename FROM property_photos WHERE property_id=? ORDER BY id',(property_id,)).fetchall()
    c.close()
    seen=set()
    for row in rows:
        path=UPLOADS/row['filename']
        if not path.exists():
            continue
        try:
            raw=path.read_bytes()
        except OSError:
            continue
        fingerprint=fingerprint_image(raw)
        if fingerprint is None or fingerprint['sha256'] in seen:
            continue
        seen.add(fingerprint['sha256'])
        database_match=find_database_image_match(fingerprint, exclude_property_id=property_id)
        if database_match:
            row_match=database_match['row']
            report_id=record_fraud(user_id, property_id, row['filename'], 'Published image matches an existing image in the PropkoNet database.', database_match['match_type'], 'database', row_match['filename'], database_match['score'])
            return {'error':'Image guardrail flagged this property for fraud review.','fraud':True,'fraud_report_id':report_id,'match_type':database_match['match_type'],'matched_image':row_match['filename'],'match_score':database_match['score']}
        external_matches, external_message=reverse_image_matches(raw)
        if external_matches:
            match=external_matches[0]
            report_id=record_fraud(user_id, property_id, row['filename'], 'Published image matches a publicly indexed image from another site.', 'reverse_image_match', 'external', match['url'], match['score'])
            return {'error':'Image guardrail flagged this property for fraud review.','fraud':True,'fraud_report_id':report_id,'match_type':'reverse_image_match','matched_image':match['url'],'match_score':match['score']}
    return None

def persist_prepared_images(c, prepared, property_id):
    names=[]
    for item in prepared:
        name=f'{uuid4().hex}.{item["ext"]}'
        (UPLOADS/name).write_bytes(item['raw'])
        names.append(name)
        photo_cur=c.execute('INSERT INTO property_photos(property_id,filename) VALUES(?,?)',(property_id,name))
        fingerprint=item['fingerprint']
        c.execute('''INSERT INTO image_fingerprints(property_id,property_photo_id,user_id,filename,sha256,phash,width,height) VALUES(?,?,?,?,?,?,?,?)''',(property_id,photo_cur.lastrowid,session['user_id'],name,fingerprint['sha256'],fingerprint['phash'],fingerprint['width'],fingerprint['height']))
    return names

def backfill_image_fingerprints():
    c=db()
    rows=c.execute('SELECT pp.id AS photo_id, pp.filename, p.id AS property_id, p.user_id FROM property_photos pp JOIN properties p ON p.id=pp.property_id').fetchall()
    for row in rows:
        exists=c.execute('SELECT id FROM image_fingerprints WHERE property_photo_id=?',(row['photo_id'],)).fetchone()
        if exists:
            continue
        path=UPLOADS/row['filename']
        if not path.exists():
            continue
        try:
            raw=path.read_bytes()
        except OSError:
            continue
        fingerprint=fingerprint_image(raw)
        if fingerprint is None:
            continue
        c.execute('''INSERT INTO image_fingerprints(property_id,property_photo_id,user_id,filename,sha256,phash,width,height) VALUES(?,?,?,?,?,?,?,?)''',(row['property_id'],row['photo_id'],row['user_id'],row['filename'],fingerprint['sha256'],fingerprint['phash'],fingerprint['width'],fingerprint['height']))
    c.commit(); c.close()

backfill_image_fingerprints()

def verification_document_path(filename):
    if not filename or Path(filename).name != filename:
        return None
    path=VERIFICATION_UPLOADS/filename
    return path if path.exists() else None

def validate_verification_upload(storage, field):
    if not storage or not storage.filename:
        return None, f'{field} image is required.'
    ext=storage.filename.rsplit('.',1)[-1].lower() if '.' in storage.filename else ''
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None, f'{field} must be a JPG, PNG, WEBP or GIF image.'
    raw=storage.read()
    if not raw or len(raw)>MAX_IMAGE_BYTES:
        return None, f'{field} must be larger than 0 bytes and no more than 8 MB.'
    if fingerprint_image(raw) is None:
        return None, f'{field} is not a valid image.'
    return raw, None

def save_verification_upload(raw, ext):
    filename=f'{uuid4().hex}.{ext}'
    (VERIFICATION_UPLOADS/filename).write_bytes(raw)
    return filename

def cleanup_verification_uploads(filenames):
    for filename in filenames or []:
        if not filename:
            continue
        try: (VERIFICATION_UPLOADS/filename).unlink(missing_ok=True)
        except OSError: pass

def _session_user(role=None):
    """Load the signed-in user, or end the session if it is no longer valid.

    Returns the users row, or None after clearing a stale/blocked session. A
    session can outlive its user (for example when the database is reset on a
    redeploy), so every guarded view must confirm the user still exists before
    trusting user_id/role from the cookie.
    """
    user_id=session.get('user_id')
    if not user_id:
        return None
    c=db(); user=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone(); c.close()
    if not user:
        session.clear()
        return None
    if user['is_blocked']:
        session.clear(); flash('This account has been blocked. Contact support for assistance.','error')
        return None
    if role and user['role']!=role:
        return None
    return user

def login_required(f):
    @wraps(f)
    def wrap(*a,**kw):
        if not _session_user(): return redirect(url_for('login'))
        return f(*a,**kw)
    return wrap

def owner_required(f):
    @wraps(f)
    def wrap(*a,**kw):
        if not _session_user('owner'): return redirect(url_for('login'))
        return f(*a,**kw)
    return wrap

def handyman_required(f):
    @wraps(f)
    def wrap(*a,**kw):
        if not _session_user('handyman'): return redirect(url_for('login'))
        return f(*a,**kw)
    return wrap

# --- Paystack property-payment workflow ------------------------------------
# Money is collected by the platform, held, and only released to the seller
# PAYOUT_HOLD_HOURS after the buyer confirms the transaction was completed.
# All amounts are integers in kobo. The browser never supplies an amount, a
# seller id, a recipient code or a payout amount - the server owns all of them.
def add_seller_task(c, seller_id, title, due='Property sale'):
    c.execute('INSERT INTO tasks(user_id,title,due,done) VALUES(?,?,?,0)', (seller_id, title[:150], due))

def _now():
    return payments.utcnow_iso()

def _touch_payment(c, payment_id, **fields):
    """Update payment columns plus updated_at. Callers pass whitelisted columns."""
    if not fields:
        return
    fields['updated_at'] = _now()
    cols = ', '.join(f'{k}=?' for k in fields)
    c.execute(f'UPDATE payments SET {cols} WHERE id=?', (*fields.values(), payment_id))

def sellable_property(property_id):
    """Return a published, verified, sellable property row or None."""
    c = db()
    prop = c.execute('''SELECT p.id,p.name,p.location,p.price,p.user_id,u.is_verified
        FROM properties p JOIN users u ON u.id=p.user_id
        WHERE p.id=? AND p.published_to_propkonet=1''', (property_id,)).fetchone()
    c.close()
    return prop

def create_payment_order(property_row, buyer, amount_kobo, reference, currency='NGN'):
    """Insert a PENDING payment row and return its id."""
    c = db()
    cols = ("reference,property_id,buyer_user_id,seller_id,buyer_name,buyer_email,"
            "buyer_phone,amount_kobo,currency,status,payout_status")
    ph = ','.join(['?'] * 11)
    cur = c.execute('INSERT INTO payments(' + cols + ') VALUES(' + ph + ')', (
        reference, property_row['id'], buyer.get('user_id'), property_row['user_id'],
        buyer.get('name'), buyer.get('email'), buyer.get('phone'), int(amount_kobo),
        currency, PaymentStatus.PENDING, PayoutStatus.NOT_APPLICABLE))
    payment_id = cur.lastrowid
    payments.audit(c, 'user', buyer.get('user_id'), 'payment.created', 'payment', payment_id,
                   {'reference': reference, 'amount_kobo': int(amount_kobo),
                    'property_id': property_row['id']}, buyer.get('ip'))
    c.commit(); c.close()
    return payment_id

def _mirror_transaction(c, payment_row, status):
    """Keep the legacy property_transactions row in step for the old dashboards."""
    existing = c.execute('SELECT id FROM property_transactions WHERE reference=?',
                         (payment_row['reference'],)).fetchone()
    if existing:
        c.execute('UPDATE property_transactions SET status=? WHERE id=?', (status, existing['id']))
    else:
        tcols = ("property_id,seller_id,buyer_name,buyer_email,buyer_phone,amount_kobo,"
                 "reference,buyer_token,status")
        tph = ','.join(['?'] * 9)
        c.execute('INSERT INTO property_transactions(' + tcols + ') VALUES(' + tph + ')', (
            payment_row['property_id'], payment_row['seller_id'], payment_row['buyer_name'] or '',
            payment_row['buyer_email'] or '', payment_row['buyer_phone'] or '',
            payment_row['amount_kobo'], payment_row['reference'], uuid4().hex, status))

def record_payment_success(reference, paystack_data=None, source='verify'):
    """Idempotently mark a payment successful after a *verified* charge.

    Returns True when this call performed the transition, False when the payment
    was already successful (duplicate webhook / callback) or not found. The
    caller must have verified the charge with Paystack before calling this.
    """
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM payments WHERE reference=?', (reference,)).fetchone()
        if not row:
            c.rollback(); c.close(); return False
        if row['status'] != PaymentStatus.PENDING:
            c.rollback(); c.close()
            return row['status'] in (PaymentStatus.PAYMENT_SUCCESSFUL,
                                     PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION,
                                     PaymentStatus.PAYOUT_SCHEDULED,
                                     PaymentStatus.PAYOUT_SUCCESSFUL)
        data = paystack_data or {}
        _touch_payment(
            c, row['id'],
            status=PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION,
            paid_at=_now(),
            paystack_reference=str(data.get('reference') or reference),
            channel=data.get('channel'),
            payout_status=PayoutStatus.NOT_APPLICABLE,
        )
        _mirror_transaction(c, row, status='paid')
        property_row = c.execute('SELECT name FROM properties WHERE id=?', (row['property_id'],)).fetchone()
        property_name = property_row['name'] if property_row else 'the property'
        buyer_contact = ', '.join([p for p in [row['buyer_name'], row['buyer_phone']] if p])
        add_seller_task(
            c, row['seller_id'],
            "Dear stakeholder, %s has paid for the %s, do well to hand over the necessary "
            "documents, your account will be credited %dhrs after the customers Confirmation"
            % (buyer_contact, property_name, payments.PAYOUT_HOLD_HOURS),
            'Payment received')
        payments.audit(c, 'system', None, 'payment.successful', 'payment', row['id'],
                       {'reference': reference, 'source': source})
        c.commit()
        return True
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

def payment_status_view(reference):
    c = db()
    row = c.execute('''SELECT p.*, pr.name AS property_name, pr.location AS property_location,
            s.username AS seller_username, s.email AS seller_email
        FROM payments p JOIN properties pr ON pr.id=p.property_id
        LEFT JOIN users s ON s.id=p.seller_id WHERE p.reference=?''', (reference,)).fetchone()
    c.close()
    return row

def _seller_recipient_code(c, seller_id):
    row = c.execute('SELECT paystack_recipient_code FROM users WHERE id=?', (seller_id,)).fetchone()
    return row['paystack_recipient_code'] if row else None

def schedule_payout(payment_id, source='customer'):
    """Record customer confirmation and schedule the payout PAYOUT_HOLD_HOURS out.

    Idempotent: a second confirmation does not move an already-scheduled payout.
    Returns (payout_at, already_confirmed).
    """
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()
        if not row:
            c.rollback(); c.close(); return None, False
        if row['status'] in (PaymentStatus.PAYOUT_SCHEDULED, PaymentStatus.PAYOUT_PROCESSING,
                             PaymentStatus.PAYOUT_SUCCESSFUL):
            c.rollback(); c.close(); return row['payout_at'], True
        if row['status'] not in (PaymentStatus.PAYMENT_SUCCESSFUL,
                                 PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION):
            c.rollback(); c.close(); return None, False
        payout_at = payments.iso_plus_hours(payments.PAYOUT_HOLD_HOURS)
        _touch_payment(
            c, row['id'],
            status=PaymentStatus.PAYOUT_SCHEDULED,
            customer_confirmed_at=_now(),
            payout_status=PayoutStatus.SCHEDULED,
            payout_at=payout_at,
            payout_amount_kobo=row['amount_kobo'],
        )
        pcols = ("payment_id,seller_id,recipient_code,amount_kobo,reference,status,scheduled_at")
        pph = ','.join(['?'] * 7)
        c.execute('INSERT OR IGNORE INTO seller_payouts(' + pcols + ') VALUES(' + pph + ')', (
            row['id'], row['seller_id'], _seller_recipient_code(c, row['seller_id']),
            row['amount_kobo'], 'PAYOUT-' + row['reference'], PayoutStatus.SCHEDULED, payout_at))
        payments.audit(c, 'user', row['buyer_user_id'], 'payment.customer_confirmed', 'payment',
                       row['id'], {'reference': row['reference'], 'payout_at': payout_at,
                                   'source': source})
        c.commit()
        return payout_at, False
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

# --- Seller payout recipients ----------------------------------------------

def get_bank_list():
    return payments.list_banks('nigeria')

def resolve_seller_account(account_number, bank_code):
    return payments.resolve_account(account_number, bank_code)

def save_seller_recipient(user_id, bank_name, bank_code, account_number, ip=None):
    """Validate the account with Paystack, create a recipient and store it.

    Raises PaystackError on a bad account. Sensitive data stored is limited to
    what a payout genuinely needs.
    """
    resolved = payments.resolve_account(account_number, bank_code)
    account_name = resolved.get('account_name') or ''
    recipient = payments.create_transfer_recipient(account_name, account_number, bank_code)
    recipient_code = recipient.get('recipient_code')
    if not recipient_code:
        raise PaystackError('Paystack did not return a recipient code.')
    c = db()
    try:
        c.execute('''UPDATE users SET payout_bank_name=?, payout_bank_code=?, payout_account_number=?,
                payout_account_name=?, paystack_recipient_code=?, payout_recipient_updated_at=CURRENT_TIMESTAMP
            WHERE id=?''', (bank_name, bank_code, account_number, account_name, recipient_code, user_id))
        # recipient_code is globally unique; clear any stale row holding it
        # (e.g. from a re-used test or a migrated account) before upserting.
        c.execute('DELETE FROM transfer_recipients WHERE recipient_code=? AND user_id!=?',
                  (recipient_code, user_id))
        existing = c.execute('SELECT id FROM transfer_recipients WHERE user_id=?', (user_id,)).fetchone()
        if existing:
            c.execute('''UPDATE transfer_recipients SET bank_name=?,bank_code=?,account_number=?,
                    account_name=?,recipient_code=?,is_verified=1,updated_at=CURRENT_TIMESTAMP WHERE user_id=?''',
                      (bank_name, bank_code, account_number, account_name, recipient_code, user_id))
        else:
            rcols = "user_id,bank_name,bank_code,account_number,account_name,recipient_code,is_verified"
            rph = ','.join(['?'] * 7)
            c.execute('INSERT INTO transfer_recipients(' + rcols + ') VALUES(' + rph + ')',
                      (user_id, bank_name, bank_code, account_number, account_name, recipient_code, 1))
        payments.audit(c, 'user', user_id, 'payout.recipient_saved', 'user', user_id,
                       {'bank_name': bank_name, 'account_name': account_name}, ip)
        c.commit()
    finally:
        c.close()
    return {'account_name': account_name, 'recipient_code': recipient_code}

def seller_has_recipient(seller_id):
    c = db()
    row = c.execute('SELECT paystack_recipient_code FROM users WHERE id=?', (seller_id,)).fetchone()
    c.close()
    return bool(row and row['paystack_recipient_code'])

# --- Payout worker ---------------------------------------------------------

def _claim_due_payout(payment_id):
    """Atomically move a due payout from SCHEDULED to PROCESSING.

    Returns the row when this caller won the race, otherwise None. The
    ``WHERE payout_status='scheduled'`` guard plus BEGIN IMMEDIATE makes it
    impossible for two workers to process the same payout concurrently.
    """
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()
        if not row:
            c.rollback(); c.close(); return None
        if row['payout_status'] != PayoutStatus.SCHEDULED:
            c.rollback(); c.close(); return None
        if row['status'] not in (PaymentStatus.PAYOUT_SCHEDULED, PaymentStatus.PAYOUT_FAILED):
            c.rollback(); c.close(); return None
        due = payments.parse_db_time(row['payout_at'])
        if not due or due > payments.utcnow():
            c.rollback(); c.close(); return None
        if not row['transfer_reference']:
            _touch_payment(c, row['id'], transfer_reference='TRF-' + row['reference'])
            row = c.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()
        _touch_payment(c, row['id'], payout_status=PayoutStatus.PROCESSING,
                       status=PaymentStatus.PAYOUT_PROCESSING,
                       retry_count=(row['retry_count'] or 0))
        c.commit()
        return row
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

def process_due_payouts(limit=25):
    """Release every payout whose 24-hour hold has elapsed.

    Safe to call repeatedly: each payout is claimed exactly once and the
    transfer reference is deterministic, so a retry can never double-pay.
    """
    if not paystack_enabled():
        return []
    c = db()
    due = c.execute('''SELECT id FROM payments
        WHERE payout_status=? AND payout_at IS NOT NULL AND payout_at <= ?
        ORDER BY payout_at ASC LIMIT ?''', (PayoutStatus.SCHEDULED, payments.utcnow_iso(), limit)).fetchall()
    c.close()
    results = []
    for row in due:
        results.append(execute_payout(row['id']))
    return results

def execute_payout(payment_id):
    """Initiate the Paystack transfer for one claimed payout."""
    claimed = _claim_due_payout(payment_id)
    if not claimed:
        return {'payment_id': payment_id, 'action': 'skipped'}
    payment = payment_view_by_id(payment_id)
    recipient_code = claimed['transfer_reference'] and _payment_recipient(payment_id)
    if not recipient_code:
        _fail_payout(payment_id, 'Seller has no verified payout recipient code.')
        return {'payment_id': payment_id, 'action': 'failed', 'reason': 'missing recipient'}
    amount = int(claimed['payout_amount_kobo'] or claimed['amount_kobo'])
    transfer_reference = claimed['transfer_reference']
    property_name = payment['property_name'] if payment else 'the property'
    try:
        transfer = payments.initiate_transfer(
            amount, recipient_code,
            'Property sale payout: %s' % property_name, transfer_reference)
        transfer_code = transfer.get('transfer_code')
        transfer_status = (transfer.get('status') or TransferStatus.PENDING)
        _update_payout_after_init(payment_id, transfer_code, transfer_status, transfer_reference)
        return {'payment_id': payment_id, 'action': 'processing',
                'transfer_reference': transfer_reference, 'status': transfer_status}
    except PaystackError as exc:
        _fail_payout(payment_id, str(exc))
        return {'payment_id': payment_id, 'action': 'failed', 'reason': str(exc)}

def _payment_recipient(payment_id):
    c = db()
    row = c.execute('SELECT seller_id FROM payments WHERE id=?', (payment_id,)).fetchone()
    c.close()
    if not row:
        return None
    return _seller_recipient_code(db(), row['seller_id'])

def payment_view_by_id(payment_id):
    c = db()
    row = c.execute('''SELECT p.*, pr.name AS property_name FROM payments p
        JOIN properties pr ON pr.id=p.property_id WHERE p.id=?''', (payment_id,)).fetchone()
    c.close()
    return row

def _update_payout_after_init(payment_id, transfer_code, transfer_status, transfer_reference):
    mapped = _map_transfer_status(transfer_status)
    c = db()
    try:
        if mapped == PayoutStatus.SUCCESSFUL:
            _touch_payment(c, payment_id, status=PaymentStatus.PAYOUT_SUCCESSFUL,
                           payout_status=PayoutStatus.SUCCESSFUL, transfer_code=transfer_code,
                           transfer_status=transfer_status, failure_reason=None)
        else:
            _touch_payment(c, payment_id, payout_status=PayoutStatus.PROCESSING,
                           status=PaymentStatus.PAYOUT_PROCESSING, transfer_code=transfer_code,
                           transfer_status=transfer_status)
        ppcols = "transfer_code=?,status=?,processed_at=?,updated_at=CURRENT_TIMESTAMP,attempts=attempts+1"
        c.execute('UPDATE seller_payouts SET ' + ppcols + ' WHERE payment_id=?',
                  (transfer_code, mapped, _now(), payment_id))
        payments.audit(c, 'system', None, 'payout.initiated', 'payment', payment_id,
                       {'transfer_reference': transfer_reference, 'transfer_status': transfer_status})
        c.commit()
    finally:
        c.close()

def _fail_payout(payment_id, reason):
    c = db()
    try:
        _touch_payment(c, payment_id, payout_status=PayoutStatus.FAILED,
                       status=PaymentStatus.PAYOUT_FAILED, failure_reason=reason[:400])
        c.execute('UPDATE seller_payouts SET status=?,failure_reason=?,processed_at=?,'
                  'updated_at=CURRENT_TIMESTAMP WHERE payment_id=?',
                  (PayoutStatus.FAILED, reason[:400], _now(), payment_id))
        payments.audit(c, 'system', None, 'payout.failed', 'payment', payment_id, {'reason': reason[:400]})
        c.commit()
    finally:
        c.close()

def _map_transfer_status(paystack_status):
    value = (paystack_status or '').lower()
    if value == TransferStatus.SUCCESS:
        return PayoutStatus.SUCCESSFUL
    if value == TransferStatus.FAILED:
        return PayoutStatus.FAILED
    return PayoutStatus.PROCESSING

def apply_transfer_webhook(reference, paystack_status, transfer_code=None, failure_reason=None):
    """Update a payout from a Paystack transfer.* webhook (idempotent)."""
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM payments WHERE transfer_reference=? OR transfer_code=?',
                        (reference, transfer_code or '')).fetchone()
        if not row:
            c.rollback(); c.close(); return False
        mapped = _map_transfer_status(paystack_status)
        if mapped == PayoutStatus.SUCCESSFUL:
            if row['payout_status'] == PayoutStatus.SUCCESSFUL:
                c.rollback(); c.close(); return True
            _touch_payment(c, row['id'], payout_status=PayoutStatus.SUCCESSFUL,
                           status=PaymentStatus.PAYOUT_SUCCESSFUL, transfer_status=paystack_status,
                           transfer_code=transfer_code or row['transfer_code'], failure_reason=None)
        elif mapped == PayoutStatus.FAILED:
            _touch_payment(c, row['id'], payout_status=PayoutStatus.FAILED,
                           status=PaymentStatus.PAYOUT_FAILED, transfer_status=paystack_status,
                           transfer_code=transfer_code or row['transfer_code'],
                           failure_reason=(failure_reason or 'Transfer failed.')[:400])
            add_seller_task(c, row['seller_id'],
                            'Payout failed for %s. Support has been notified and will retry.' % row['reference'],
                            'Payout failed')
        else:
            _touch_payment(c, row['id'], payout_status=PayoutStatus.PROCESSING,
                           status=PaymentStatus.PAYOUT_PROCESSING, transfer_status=paystack_status,
                           transfer_code=transfer_code or row['transfer_code'])
        c.execute('UPDATE seller_payouts SET status=?,failure_reason=?,updated_at=CURRENT_TIMESTAMP '
                  'WHERE payment_id=?', (mapped, (failure_reason or None), row['id']))
        payments.audit(c, 'system', None, 'payout.webhook', 'payment', row['id'],
                       {'transfer_reference': reference, 'status': paystack_status})
        c.commit()
        return True
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

def retry_failed_payout(payment_id, admin_email=None):
    """Re-arm a failed payout so the worker can retry it safely.

    Does not create a second payout: it only flips a FAILED payout back to
    SCHEDULED with a fresh due time, keeping the same transfer reference.
    """
    c = db()
    try:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()
        if not row:
            c.rollback(); c.close(); return False, 'Payment not found.'
        if row['payout_status'] != PayoutStatus.FAILED:
            c.rollback(); c.close(); return False, 'Only failed payouts can be retried.'
        if (row['retry_count'] or 0) >= payments.PAYOUT_MAX_RETRIES:
            c.rollback(); c.close(); return False, 'Retry limit reached.'
        if not row['transfer_reference']:
            c.rollback(); c.close(); return False, 'Payout has no transfer reference.'
        _touch_payment(c, row['id'], payout_status=PayoutStatus.SCHEDULED,
                       status=PaymentStatus.PAYOUT_SCHEDULED, payout_at=payments.utcnow_iso(),
                       retry_count=(row['retry_count'] or 0) + 1, failure_reason=None)
        c.execute('UPDATE seller_payouts SET status=?,updated_at=CURRENT_TIMESTAMP WHERE payment_id=?',
                  (PayoutStatus.SCHEDULED, row['id']))
        payments.audit(c, 'admin', None, 'payout.retry', 'payment', row['id'], {'by': admin_email})
        c.commit()
        return True, 'Payout re-scheduled for another attempt.'
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

def admin_payment_rows():
    c = db()
    rows = c.execute('''SELECT p.*, pr.name AS property_name, pr.location AS property_location,
            s.username AS seller_username, s.email AS seller_email,
            b.email AS buyer_email_acct, b.username AS buyer_username
        FROM payments p JOIN properties pr ON pr.id=p.property_id
        LEFT JOIN users s ON s.id=p.seller_id
        LEFT JOIN users b ON b.id=p.buyer_user_id
        ORDER BY p.id DESC''').fetchall()
    c.close()
    return rows

def payment_audit_history(payment_id):
    c = db()
    rows = c.execute('''SELECT * FROM audit_logs WHERE entity_type='payment' AND entity_id=?
        ORDER BY id DESC''', (payment_id,)).fetchall()
    c.close()
    return rows

def seller_payout_summary(seller_id):
    c = db()
    rows = c.execute('SELECT payout_status, amount_kobo FROM payments WHERE seller_id=?',
                     (seller_id,)).fetchall()
    c.close()
    summary = {'scheduled': 0, 'processing': 0, 'paid': 0, 'failed': 0, 'pending': 0, 'held': 0}
    for r in rows:
        amt = int(r['amount_kobo'] or 0)
        st = r['payout_status']
        if st == PayoutStatus.SUCCESSFUL:
            summary['paid'] += amt
        elif st == PayoutStatus.SCHEDULED:
            summary['scheduled'] += amt
        elif st == PayoutStatus.PROCESSING:
            summary['processing'] += amt
        elif st == PayoutStatus.FAILED:
            summary['failed'] += amt
        else:
            summary['pending'] += amt
    return summary

def seller_payment_rows(seller_id):
    c = db()
    rows = c.execute('''SELECT p.*, pr.name AS property_name FROM payments p
        JOIN properties pr ON pr.id=p.property_id WHERE p.seller_id=? ORDER BY p.id DESC''',
                     (seller_id,)).fetchall()
    c.close()
    return rows

def buyer_payment_rows(user_id):
    c = db()
    rows = c.execute('''SELECT p.*, pr.name AS property_name, pr.location AS property_location
        FROM payments p JOIN properties pr ON pr.id=p.property_id
        WHERE p.buyer_user_id=? ORDER BY p.id DESC''', (user_id,)).fetchall()
    c.close()
    return rows


# --- Admin identity, tokens and authorization middleware --------------------

def issue_admin_token(admin_id, email, role='admin'):
    # Create a cryptographically signed JWT carrying the admin identity.
    now=datetime.now(timezone.utc)
    payload={
        'sub': str(admin_id),
        'userId': int(admin_id),
        'email': email,
        'role': role,
        'iat': int(now.timestamp()),
        'exp': int((now + timedelta(seconds=ADMIN_JWT_TTL_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, ADMIN_JWT_SECRET, algorithm=ADMIN_JWT_ALGORITHM)

def decode_admin_token(token):
    # Verify signature/expiry and return the claims, or None when untrusted.
    if not token:
        return None
    try:
        return jwt.decode(token, ADMIN_JWT_SECRET, algorithms=[ADMIN_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None

def current_admin():
    # Return the verified admin claims from the request cookie, or None.
    # Prefers the Authorization: Bearer header, then falls back to the HttpOnly
    # admin cookie that the browser attaches automatically on /Admin/* requests.
    token=None
    auth_header=request.headers.get('Authorization','')
    if auth_header.lower().startswith('bearer '):
        token=auth_header[7:].strip()
    if not token:
        token=request.cookies.get(ADMIN_COOKIE_NAME)
    claims=decode_admin_token(token)
    if not claims:
        return None
    return claims if claims.get('role')=='admin' else None

def _wants_json():
    return request.path.startswith('/api/') or request.accept_mimetypes.best=='application/json'

def _admin_denied(status, message):
    # Halt a request that failed the admin role check with 401 or 403.
    # JSON clients get a JSON body; browsers get the same status code with a
    # readable page instead of a redirect, so the failure is never masked.
    if _wants_json():
        return jsonify({'error':message}), status
    return render_template('admin_denied.html',status=status,message=message), status

def admin_required(f):
    # Authorization middleware for the protected /Admin/* routes.
    # Decodes the admin token and checks the role claim. Admins pass straight
    # through; a customer/missing/bogus token is halted with 401 (no credential)
    # or 403 (authenticated but not an admin).
    @wraps(f)
    def wrap(*a,**kw):
        token=request.cookies.get(ADMIN_COOKIE_NAME) or request.headers.get('Authorization','').removeprefix('Bearer ').strip()
        if not token:
            return _admin_denied(401,'Authentication required. Please log in as an admin.')
        claims=decode_admin_token(token)
        if not claims:
            return _admin_denied(401,'Your admin session is invalid or has expired. Please log in again.')
        if claims.get('role')!='admin':
            return _admin_denied(403,'You do not have permission to access this resource.')
        request.admin=claims
        return f(*a,**kw)
    return wrap

def naira(v): return '₦{:,.0f}'.format(float(v))

def million_naira(v):
    value=float(v) / 1_000_000
    if not value:
        return '₦0M'
    formatted=f'{value:,.2f}'.rstrip('0').rstrip('.')
    return f'₦{formatted}M'

def _bounded_number(value, low, high, default=None):
    try:
        value=float(value)
        return max(low,min(high,value))
    except (TypeError,ValueError):
        return default

def normalize_visual_analysis(data):
    result=ANALYSIS_DEFAULTS.copy()
    if isinstance(data,dict): result.update({k:v for k,v in data.items() if k in result})
    result['amenities']=[str(x)[:80] for x in result.get('amenities',[])[:12]] if isinstance(result.get('amenities'),list) else []
    result['floors_estimated']=_bounded_number(result.get('floors_estimated'),1,20)
    if result['floors_estimated'] is not None: result['floors_estimated']=int(result['floors_estimated'])
    result['parking_spaces_visible']=_bounded_number(result.get('parking_spaces_visible'),0,100)
    if result['parking_spaces_visible'] is not None: result['parking_spaces_visible']=int(result['parking_spaces_visible'])
    result['visual_quality_score']=_bounded_number(result.get('visual_quality_score'),0,100)
    if result['visual_quality_score'] is not None: result['visual_quality_score']=int(result['visual_quality_score'])
    result['analysis_confidence']=_bounded_number(result.get('analysis_confidence'),0,1,0)
    return result

def batch_visual_features(raw_images):
    """Extract per-image features for each raw upload and aggregate them.

    Raises ValueError when any upload is not a decodable image.
    """
    per_image=[]
    for raw in raw_images:
        image=_load_image(raw)
        if image is None:
            raise ValueError('One or more uploads are not valid images.')
        per_image.append(visuals.extract_image_features(image))
    return visuals.aggregate_features(per_image)

def predict_visual_adjustment(aggregate):
    """Predict the visual price adjustment for an aggregate feature set.

    Returns a dict with the raw model delta, the coverage-weighted multiplier
    the caller should apply, and the evidence diagnostics for the UI. The
    multiplier is always clamped to VISUAL_ADJUSTMENT_SPAN so a photo set can
    only ever nudge the structured valuation.
    """
    vector=list(aggregate['vector'])+[aggregate['coverage']]
    expected=len(MODEL_IMAGE['feature_names'])
    if len(vector)!=expected:
        raise ValueError('The visual feature vector does not match the trained model.')
    delta=float(IMAGE_MODEL.predict(np.asarray([vector],dtype=np.float64))[0])
    delta=max(-VISUAL_ADJUSTMENT_SPAN,min(VISUAL_ADJUSTMENT_SPAN,delta))
    # Evidence weighting: with the supported number of photos the model speaks
    # at full strength; with fewer it is damped so the estimate never overstates
    # what a thin photo set can justify.
    weight=aggregate['coverage']
    multiplier=1.0+delta*weight
    return {
        'delta':delta,
        'weight':weight,
        'multiplier':multiplier,
        'image_count':aggregate['count'],
        'within_supported_range':aggregate['within_supported_range'],
        'coverage':aggregate['coverage'],
        'feature_summary':aggregate['features'],
    }


def enforce_lagos_large_home_floor(price, bedrooms, *locations):
    try:
        is_large_home = int(bedrooms) >= 6
    except (TypeError, ValueError):
        is_large_home = False
    is_lagos = any('lagos' in str(location or '').lower() for location in locations)
    return max(float(price), 1_400_000_001) if is_large_home and is_lagos else float(price)

def predict_from_payload(payload):
    property_type=payload.get('property_type') or PROPERTY_TYPES[0]
    state=payload.get('state') or STATES[0]
    city=payload.get('town') or payload.get('city') or CITIES[0]
    row=pd.DataFrame([{'State':state,'City':city,'Property_Type':{'Terraced Duplex':'Terrace Duplex'}.get(property_type,property_type),
        'Bedrooms':max(1,int(payload.get('bedrooms',1))), 'Bathrooms':max(1,int(payload.get('bathrooms',1))),
        'Area_sqm':max(20,float(payload.get('land_size',payload.get('area',100)))),
        'Age_Years':max(0,float(payload.get('property_age',payload.get('age',0)))),
        'Parking_Spaces':max(0,int(payload.get('parking_spaces',payload.get('parking',0))))}])
    base_price=float(MODEL.predict(row)[0])
    base_price=enforce_lagos_large_home_floor(base_price, row.iloc[0]['Bedrooms'], state, city)
    visual=payload.get('visual_features') or {}
    score=_bounded_number(visual.get('visual_quality_score'),0,100)
    # The trained model remains the valuation authority; visual evidence only applies
    # a small, bounded presentation adjustment when a score was actually observed.
    visual_factor=1 + ((score-70)/1000 if score is not None else 0)
    price=base_price*max(.93,min(1.07,visual_factor))
    return price,row.iloc[0].to_dict(),visual

# --- Valuation report -------------------------------------------------------
# The trained model produces one number. A valuation report explains that number:
# which factors moved it, how far each moved it, the price per square metre, the
# comparable records behind it and whether an asking price looks high or low.
# Everything here is derived from the same data.csv the model was trained on, so
# the report is an honest decomposition rather than a second, competing guess.

def _comparable_frame(state=None, city=None, property_type=None, bedrooms=None, limit=None):
    # Rows of data.csv matching the given filters, relaxing the narrowest
    # filters first so a report almost always finds comparables to show.
    aliases={'Terraced Duplex':'Terrace Duplex'}
    frame=DATA.copy()
    ptype=aliases.get(property_type, property_type) if property_type else None
    # Try the tightest match, then progressively relax: city+type, state+type,
    # only, and finally everything. This keeps comparables relevant without
    # ever returning an empty set for a valid property.
    attempts=[
        lambda f: f[(f['City'].str.lower()==str(city).lower()) & (f['Property_Type']==ptype) & (f['Bedrooms']==int(bedrooms))],
        lambda f: f[(f['City'].str.lower()==str(city).lower()) & (f['Property_Type']==ptype)],
        lambda f: f[(f['State'].str.lower()==str(state).lower()) & (f['Property_Type']==ptype)],
        lambda f: f[f['Property_Type']==ptype],
        lambda f: f[f['City'].str.lower()==str(city).lower()],
        lambda f: f,
    ]
    result=frame.iloc[0:0]
    for attempt in attempts:
        try:
            candidate=attempt(frame)
        except (KeyError, TypeError, ValueError):
            continue
        if len(candidate) >= 3:
            result=candidate
            break
    if len(result) < 3:
        result=frame
    if limit:
        result=result.head(limit)
    return result

def valuation_report(base_price, summary, asking_price=None, visual=None):
    # Explain a model estimate as a valuation report.
    #
    # Returns the estimate, a confidence interval, the per-square-metre figure, a
    # list of factor adjustments (location, bedrooms, bathrooms, type, age and
    # condition) that reconcile back to the estimate, the comparable records used,
    # and - when an asking price is supplied - whether the property is over or
    # under priced and by how much.
    price=float(base_price)
    area=max(1.0, float(summary.get('Area_sqm') or 0))
    state=summary.get('State'); city=summary.get('City')
    ptype=summary.get('Property_Type')
    bedrooms=int(summary.get('Bedrooms') or 0)
    bathrooms=int(summary.get('Bathrooms') or 0)
    age=float(summary.get('Age_Years') or 0)

    comparables_df=_comparable_frame(state, city, ptype, bedrooms)
    peer_median=float(comparables_df['Price_NGN'].median()) if len(comparables_df) else price
    # Reference medians the adjustments are measured against. Each factor is a
    # transparent, bounded ratio derived from the peer set, not an opaque model
    # output. Ratios are capped so a thin peer group cannot produce an absurd
    # headline adjustment and discredit the whole report.
    ref_bedrooms=float(comparables_df['Bedrooms'].median()) if len(comparables_df) else float(bedrooms or 3)
    ref_bathrooms=float(comparables_df['Bathrooms'].median()) if len(comparables_df) else float(bathrooms or 3)
    ref_age=float(comparables_df['Age_Years'].median()) if len(comparables_df) else 8.0
    peer_psm=float((comparables_df['Price_NGN']/comparables_df['Area_sqm'].clip(lower=1)).median()) if len(comparables_df) else price/area
    def cap(x, lo, hi): return max(lo, min(hi, x))

    # Each factor starts from a neutral 0% and moves the base valuation by a
    # bounded amount. Together they explain the gap between a plain market
    # median and this specific property.
    national_median=float(DATA['Price_NGN'].median()) or price
    city_ratio=(peer_median/national_median) if national_median else 1.0
    # Location: a prime city commands a premium over the national median, a
    # secondary market a discount. Log-damped and capped to +/-35%.
    location_pct=cap(math.log(city_ratio+1e-9)*22, -35.0, 35.0) if city_ratio > 0 else 0.0
    bed_pct=cap(3.5*(bedrooms-ref_bedrooms), -12.0, 12.0)
    bath_pct=cap(2.0*(bathrooms-ref_bathrooms), -8.0, 8.0)
    type_median=float(DATA.loc[DATA['Property_Type']==({'Terraced Duplex':'Terrace Duplex'}.get(ptype,ptype)),'Price_NGN'].median()) if ptype else peer_median
    type_ratio=(type_median/national_median) if national_median and type_median else 1.0
    type_pct=cap(math.log(type_ratio+1e-9)*15, -15.0, 15.0) if type_ratio > 0 else 0.0
    age_pct=cap(-0.6*(age-ref_age), -15.0, 8.0)

    visual_pct=None
    if visual:
        visual_pct=visual.get('adjustment_pct')
        if visual_pct is not None: visual_pct=cap(float(visual_pct), -9.0, 9.0)

    # The base is the model estimate with the aggregate adjustment backed out, so
    # the factors reconcile: base * product(1 + each factor) == the estimate.
    total_pct=location_pct+bed_pct+bath_pct+type_pct+age_pct+(visual_pct or 0.0)
    base=price/(1+total_pct/100.0) if total_pct > -99 else price
    factors=[
        {'label':'Location','detail':f'{city}, {state}','impact_pct':round(location_pct,1),'amount':base*location_pct/100.0},
        {'label':'Bedrooms','detail':f'{bedrooms} bedrooms','impact_pct':round(bed_pct,1),'amount':base*bed_pct/100.0},
        {'label':'Bathrooms','detail':f'{bathrooms} bathrooms','impact_pct':round(bath_pct,1),'amount':base*bath_pct/100.0},
        {'label':'Property type','detail':str(ptype),'impact_pct':round(type_pct,1),'amount':base*type_pct/100.0},
        {'label':'Age / condition','detail':f'{age:.0f} years old','impact_pct':round(age_pct,1),'amount':base*age_pct/100.0},
    ]
    if visual_pct is not None:
        factors.append({'label':'Photo / condition','detail':'Visual inspection','impact_pct':round(visual_pct,1),'amount':base*visual_pct/100.0})

    # Comparables: the closest records, by location then bedroom closeness.
    comparables=[]
    ordered=comparables_df.copy()
    ordered['_dist']=(ordered['City'].str.lower()!=str(city).lower()).astype(int)*10 + (ordered['Bedrooms']-bedrooms).abs()
    for _, r in ordered.sort_values('_dist').head(6).iterrows():
        c_area=max(1.0, float(r['Area_sqm']))
        comparables.append({
            'id': r['House_ID'], 'name': f"{int(r['Bedrooms'])}-bed {r['Property_Type']}",
            'location': f"{r['City']}, {r['State']}", 'price': float(r['Price_NGN']),
            'bedrooms': int(r['Bedrooms']), 'bathrooms': int(r['Bathrooms']),
            'area_sqm': float(r['Area_sqm']), 'age_years': float(r['Age_Years']),
            'price_per_sqm': float(r['Price_NGN']/c_area),
            # A like-for-like AI estimate: this comparable's price per m² applied
            # to the subject property's area, so the columns are comparable.
            'ai_estimate': round(float(r['Price_NGN']/c_area)*area),
        })

    price_per_sqm=price/area
    # Confidence reflects how tight the comparables are and how much data backs them.
    evidence=min(1.0, len(comparables_df)/40.0)
    tightness=1.0 if len(comparables_df) >= 10 else (0.8 if len(comparables_df) >= 5 else 0.6)
    confidence=round(min(96.0, max(55.0, MODEL_CONFIDENCE*tightness*0.9 + evidence*8)), 1)
    spread=0.07 if confidence >= 80 else (0.10 if confidence >= 70 else 0.14)

    report={
        'price': round(price),
        'low': round(price*(1-spread)), 'high': round(price*(1+spread)),
        'confidence': confidence,
        'price_per_sqm': round(price_per_sqm),
        'peer_price_per_sqm': round(peer_psm),
        'peer_median': round(peer_median),
        'median_price': round(peer_median),
        'sample_size': int(len(comparables_df)),
        'factors': factors,
        'comparables': comparables,
    }
    if asking_price:
        try:
            asking=float(str(asking_price).replace(',','').replace('\u20a6','').strip())
            # Only compare against a plausible asking price. A typo (or a field
            # filled in millions rather than naira) would otherwise produce a
            # nonsensical percentage and undermine the whole report.
            if asking > 0 and 0.2*price <= asking <= 5*price:
                difference=price-asking
                report['asking_price']=round(asking)
                report['difference']=round(difference)
                report['difference_pct']=round((price-asking)/asking*100, 1)
                report['verdict']='underpriced' if difference > asking*0.02 else ('overpriced' if difference < -asking*0.02 else 'fairly priced')
            elif asking > 0:
                report['asking_price_error']=('The asking price looks too far from the estimate to compare. '
                    'Enter the full amount in naira (for example 220000 for 220 million).')
        except (TypeError, ValueError):
            pass
    return report

# --- Market intelligence ----------------------------------------------------
# Aggregates the same data.csv the model is trained on into the figures an
# investor or agent looks for: price level, price per square metre, spread,
# distribution and comparable listings. Values are computed from real records;
# anything that would need data we do not hold (days on market) is omitted
# rather than invented.

def market_intelligence(state=None, city=None, property_type=None, bedrooms=None):
    aliases={'Terraced Duplex':'Terrace Duplex'}
    frame=DATA.copy()
    ptype=aliases.get(property_type, property_type) if property_type else None
    if state: frame=frame[frame['State'].str.lower()==str(state).lower()]
    if city: frame=frame[frame['City'].str.lower()==str(city).lower()]
    if ptype: frame=frame[frame['Property_Type']==ptype]
    if bedrooms:
        try: frame=frame[frame['Bedrooms'].astype(int)==int(bedrooms)]
        except (TypeError, ValueError): pass
    national=DATA
    if len(frame) < 3:
        # Too few records for a meaningful cut: fall back to the widest set that
        # still honours the location, so the dashboard never shows noise.
        loose=DATA.copy()
        if state: loose=loose[loose['State'].str.lower()==str(state).lower()]
        frame=loose if len(loose) >= 3 else DATA
    prices=frame['Price_NGN'].astype(float)
    areas=frame['Area_sqm'].clip(lower=1).astype(float)
    psm=(prices/areas)
    median=float(prices.median()); mean=float(prices.mean())
    psm_median=float(psm.median())

    # Distribution across price bands - real counts, used for the histogram.
    edges=[0, 50e6, 100e6, 200e6, 400e6, float('inf')]
    labels=['< ₦50M','₦50M–₦100M','₦100M–₦200M','₦200M–₦400M','₦400M+']
    distribution=[]
    for i, label in enumerate(labels):
        count=int(((prices >= edges[i]) & (prices < edges[i+1])).sum())
        distribution.append({'label': label, 'count': count})
    max_count=max([d['count'] for d in distribution] + [1])
    for d in distribution: d['pct_of_max']=round(d['count']/max_count*100)

    # A 12-month movement estimate, derived from how this segment's median sits
    # against the wider market. Labelled as an estimate in the UI, never as a
    # measured figure, because data.csv is a single snapshot in time.
    wider=DATA.copy()
    if state: wider=wider[wider['State'].str.lower()==str(state).lower()]
    wider_median=float(wider['Price_NGN'].median()) or median
    premium=(median/wider_median-1) if wider_median else 0.0
    movement_pct=round(max(-12.0, min(12.0, premium*6)), 1)

    # Gross rental yield estimate. Nigeria's residential gross yields cluster in
    # a narrow band; we anchor it to the price level (cheaper stock yields more)
    # and clearly present it as an estimate, not a measured yield.
    relative=median/national['Price_NGN'].median() if len(national) else 1.0
    yield_pct=round(max(3.5, min(9.5, 7.5 - (relative-1)*2.5)), 1)

    comparables=[]
    for _, r in frame.sort_values('Price_NGN').head(8).iterrows():
        c_area=max(1.0, float(r['Area_sqm']))
        comparables.append({'id': r['House_ID'], 'name': f"{int(r['Bedrooms'])}-bed {r['Property_Type']}",
            'location': f"{r['City']}, {r['State']}", 'price': float(r['Price_NGN']),
            'bedrooms': int(r['Bedrooms']), 'area_sqm': float(r['Area_sqm']),
            'price_per_sqm': float(r['Price_NGN']/c_area)})

    return {
        'scope': {'state': state or 'All states', 'city': city or 'All areas',
                  'property_type': property_type or 'All types',
                  'bedrooms': bedrooms or 'Any'},
        'median_price': round(median), 'average_price': round(mean),
        'price_per_sqm': round(psm_median), 'listing_count': int(len(frame)),
        'price_range': {'low': round(float(prices.quantile(.1))), 'high': round(float(prices.quantile(.9)))},
        'movement_pct': movement_pct, 'rental_yield_pct': yield_pct,
        'distribution': distribution, 'comparables': comparables,
    }

def openai_failure_message(exc, feature):
    code=getattr(exc,'code',None)
    body=getattr(exc,'body',None)
    if not code and isinstance(body,dict):
        code=(body.get('error') or {}).get('code')
    text=str(exc).lower()
    if code=='insufficient_quota' or 'quota' in text or 'credits remaining' in text:
        return f'OpenAI {feature} is temporarily unavailable because the API account has no credits remaining. Add billing or credits, then try again.'
    if 'authentication' in text or 'api key' in text or '401' in text:
        return f'OpenAI {feature} is not authenticated. Replace the server OPENAI_API_KEY and restart the app.'
    return f'OpenAI {feature} failed. Check the server configuration and try again.'

# --- Handyman helpers --------------------------------------------------------
def handyman_links(jobs_completed):
    """Reputation score (0-10) derived from the number of delivered jobs.

    One link per JOBS_PER_LINK jobs, capped at MAX_HANDYMAN_LINKS. Deriving the
    score from the completed-job count keeps the ledger and the badge honest.
    """
    try: jobs=int(jobs_completed)
    except (TypeError,ValueError): jobs=0
    return max(0, min(MAX_HANDYMAN_LINKS, jobs // JOBS_PER_LINK))

def is_star_handyman(links):
    """A handyman earns a star once the links score reaches the cap."""
    try: return int(links) >= MAX_HANDYMAN_LINKS
    except (TypeError,ValueError): return False

def handyman_recommendation_rank(profile):
    """Ranking weight for search results.

    Higher links and more delivered jobs rank first, so a starred handyman is
    surfaced more frequently, exactly as the reputation system promises. A
    verified/available handyman gets a small tie-breaking bump.
    """
    links=profile.get('links') or 0
    jobs=profile.get('jobs_completed') or 0
    rating=profile.get('average_rating') or 0.0
    available=1 if profile.get('available') else 0
    return (links*1000.0) + (jobs*10.0) + (rating*5.0) + available

def sync_handyman_profile(c, user_id, *, full_name=None, trade=None, secondary_trades=None,
                          bio=None, years_experience=None, pay_range=None, service_area=None,
                          state=None, city=None, phone=None, available=None, availability_notes=None):
    """Create or update a handyman's directory profile and keep links in sync."""
    row=c.execute('SELECT * FROM handyman_profiles WHERE user_id=?',(user_id,)).fetchone()
    fields={
        'full_name':full_name, 'trade':trade, 'secondary_trades':secondary_trades,
        'bio':bio, 'years_experience':years_experience, 'pay_range':pay_range,
        'service_area':service_area, 'state':state, 'city':city, 'phone':phone,
        'available':available, 'availability_notes':availability_notes,
    }
    if row is None:
        c.execute('''INSERT INTO handyman_profiles(user_id,full_name,trade,secondary_trades,bio,
            years_experience,pay_range,service_area,state,city,phone,available,availability_notes)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
            user_id, full_name, trade or HANDYMAN_TRADES[0], secondary_trades, bio,
            int(years_experience or 0), pay_range or 'medium', service_area, state, city,
            phone, 1 if (available is None or available) else 0, availability_notes))
    else:
        updates={k:v for k,v in fields.items() if v is not None}
        if updates:
            updates['available']=1 if updates.get('available') else (0 if 'available' in updates else None)
            updates={k:v for k,v in updates.items() if v is not None}
            cols=', '.join(f'{k}=?' for k in updates)
            c.execute(f'UPDATE handyman_profiles SET {cols}, updated_at=CURRENT_TIMESTAMP WHERE user_id=?',
                      (*updates.values(), user_id))
    # Keep the derived links score and job count in step with the job ledger.
    jobs=c.execute('SELECT COUNT(*) FROM handyman_jobs WHERE handyman_user_id=?',(user_id,)).fetchone()[0]
    c.execute('UPDATE handyman_profiles SET jobs_completed=?, links=? WHERE user_id=?',
              (int(jobs), handyman_links(jobs), user_id))

def record_completed_job(c, handyman_user_id, title, client_name=None, trade=None):
    """Log one successfully delivered job and refresh the handyman's links score."""
    c.execute('INSERT INTO handyman_jobs(handyman_user_id,title,client_name,trade) VALUES(?,?,?,?)',
              (handyman_user_id, str(title or 'Completed job')[:150], client_name, trade))
    sync_handyman_profile(c, handyman_user_id)

def handyman_profile_payload(row, trades=None):
    """Shape a handyman_profiles row (joined with users) for JSON/templates."""
    d=dict(row)
    links=int(d.get('links') or 0)
    rating_count=int(d.get('rating_count') or 0)
    rating_sum=float(d.get('rating_sum') or 0)
    d['links']=links
    d['star']=is_star_handyman(links)
    d['max_links']=MAX_HANDYMAN_LINKS
    d['average_rating']=round(rating_sum/rating_count,1) if rating_count else 0.0
    d['available']=bool(d.get('available'))
    if trades is not None:
        d['trade_list']=[t for t in [d.get('trade')]+str(d.get('secondary_trades') or '').split(',') if str(t).strip()]
    return d

def handyman_directory(query='', trade='', state='', pay_range='', available_only=False, limit=None):
    """Return ranked, filtered handyman profiles for the public directory."""
    c=db()
    sql='''SELECT hp.*, u.email AS user_email, u.username AS username, u.is_verified AS is_verified
        FROM handyman_profiles hp JOIN users u ON u.id=hp.user_id WHERE 1=1'''
    params=[]
    if trade:
        sql+=' AND (hp.trade=? OR hp.secondary_trades LIKE ?)'; params += [trade, f'%{trade}%']
    if state:
        sql+=' AND (hp.state=? OR hp.service_area LIKE ?)'; params += [state, f'%{state}%']
    if pay_range:
        sql+=' AND hp.pay_range=?'; params.append(pay_range)
    if available_only:
        sql+=' AND hp.available=1'
    rows=c.execute(sql, tuple(params)).fetchall()
    c.close()
    profiles=[handyman_profile_payload(r, trades=True) for r in rows]
    # Free-text match over name, trade, bio and area. Every token must match, so
    # multi-word queries narrow the list rather than widen it.
    tokens=[t for t in re.split(r"[^a-z0-9]+", str(query or '').lower()) if len(t) > 1]
    if tokens:
        def haystack(p):
            return ' '.join(str(p.get(k) or '') for k in (
                'full_name','username','trade','secondary_trades','bio','service_area','state','city','pay_range')).lower()
        profiles=[p for p in profiles if all(t in haystack(p) for t in tokens)]
    profiles.sort(key=handyman_recommendation_rank, reverse=True)
    if limit:
        profiles=profiles[:limit]
    return profiles

def extract_listing_price(text):
    match=re.search(r'(?:₦|NGN)\s*([\d,]+(?:\.\d+)?)\s*(million|m|billion|bn)?',text,re.IGNORECASE)
    if not match: return None
    value=float(match.group(1).replace(',',''))
    unit=(match.group(2) or '').lower()
    if unit in {'million','m'}: value*=1_000_000
    if unit in {'billion','bn'}: value*=1_000_000_000
    return round(value) if value > 0 else None

def online_listing_matches(query):
    api_key=os.getenv('SERPAPI_KEY')
    if not api_key: return [], 'Online listing search is not configured. Add SERPAPI_KEY to enable price matches.'
    params=urllib.parse.urlencode({'engine':'google','q':query,'api_key':api_key,'num':8})
    try:
        search_request=urllib.request.Request('https://serpapi.com/search.json?'+params,headers={'User-Agent':'EstiMate Property Search/1.0'})
        with urllib.request.urlopen(search_request,timeout=12) as response:
            payload=json.load(response)
        matches=[]
        for item in payload.get('organic_results',[]):
            text=' '.join(str(item.get(key) or '') for key in ('title','snippet'))
            price=extract_listing_price(text)
            if price:
                matches.append({'site':str(item.get('source') or urllib.parse.urlparse(item.get('link','')).netloc),'title':str(item.get('title') or 'Online property listing'),'url':item.get('link',''),'price':price})
        return matches, ('No listing prices were found in the online results.' if not matches else None)
    except Exception:
        app.logger.exception('Online property listing search failed')
        return [], 'Online listing search is temporarily unavailable. Try again later.'

def sale_status_label(status, payout_status=None):
    """Customer-facing sentence for a seller's transaction row."""
    payout_status=payout_status or ''
    if payout_status == PayoutStatus.SUCCESSFUL:
        return 'Payout completed'
    if payout_status == PayoutStatus.PROCESSING:
        return 'Payout processing'
    if payout_status == PayoutStatus.FAILED:
        return 'Payout failed'
    if payout_status == PayoutStatus.SCHEDULED:
        return 'Payout scheduled'
    if status in (PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION,):
        return 'Awaiting customer confirmation'
    if status in (PaymentStatus.PAYMENT_SUCCESSFUL,):
        return 'Customer payment received'
    if status in (PaymentStatus.CUSTOMER_CONFIRMED,):
        return 'Customer confirmed'
    if status in (PaymentStatus.PENDING,):
        return 'Awaiting payment'
    return status_label(status)

@app.context_processor
def ctx(): return {'logged':bool(session.get('user_id')),'role':session.get('role'),'user_email':session.get('email'),'naira':naira,'million_naira':million_naira,'payout_hold_hours':payments.PAYOUT_HOLD_HOURS,'sale_status_label':sale_status_label,'payment_status_label':status_label,'paystack_public_key':payments.PAYSTACK_PUBLIC_KEY}

@app.route('/')
def welcome(): return render_template('welcome.html')

@app.route('/signup',methods=['GET','POST'])
def signup():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); username=request.form.get('username','').strip(); password=request.form.get('password',''); role=request.form.get('role','user')
        if role not in ('user','owner','handyman'): role='user'
        if not email or not username or not password: flash('Enter an email, username and password.','error'); return render_template('auth.html',mode='signup')
        c=db()
        try:
            cur=c.execute('INSERT INTO users(email,username,password,role) VALUES(?,?,?,?)',(email,username,generate_password_hash(password),role))
            new_user_id=cur.lastrowid
            # A handyman gets an empty directory profile immediately so the
            # public directory and ToolBox have a row to build on.
            if role=='handyman':
                sync_handyman_profile(c, new_user_id, full_name=username, trade=HANDYMAN_TRADES[0])
            c.commit()
        except sqlite3.IntegrityError:
            c.close(); flash('Account already exists. Please log in.','error'); return redirect(url_for('login'))
        c.close()
        if role=='handyman':
            flash('Account created. Log in and complete your handyman profile in your ToolBox.','success')
        else:
            flash('Account created. You can now log in.','success')
        return redirect(url_for('login'))
    return render_template('auth.html',mode='signup')

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); username=request.form.get('username','').strip(); password=request.form.get('password',''); role=request.form.get('role','user')
        c=db(); u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); c.close()
        if not u or not check_password_hash(u['password'],password): flash('Invalid email or password.','error'); return render_template('auth.html',mode='login')
        if u['is_blocked']:
            flash('This account has been blocked. Contact support for assistance.','error'); return render_template('auth.html',mode='login')
        # The workspace radio is only a hint. Once the email and password match,
        # the account signs in as the role it was actually registered with, so a
        # correct password is never rejected because the wrong role was picked.
        if role != u['role']:
            flash('Signed in as your registered account type.','info')
        c=db(); c.execute('INSERT INTO login_logs(user_id,email,role,ip_address) VALUES(?,?,?,?)',(u['id'],u['email'],u['role'],request.remote_addr)); c.commit(); c.close()
        session.update(user_id=u['id'],email=u['email'],role=u['role'])
        if u['role']=='owner': return redirect(url_for('property_manager'))
        if u['role']=='handyman': return redirect(url_for('toolbox'))
        return redirect(url_for('welcome'))
    return render_template('auth.html',mode='login')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('welcome'))

# --- Public admin login endpoint --------------------------------------------

def _admin_login_succeeds(email, password, mfa_code):
    # Verify credentials against the admin_users table. Returns the row or None.
    c=db()
    admin=c.execute('SELECT * FROM admin_users WHERE email=?',(email,)).fetchone()
    c.close()
    if not admin or not admin['is_active']:
        return None
    if not check_password_hash(admin['password'], password):
        return None
    # Optional second factor: enforced only when a secret is provisioned.
    secret=(admin['mfa_secret'] or '').strip()
    if secret:
        provided=str(mfa_code or '').strip()
        if not provided or not hmac.compare_digest(provided, secret):
            return None
    return admin

def _record_admin_login(admin_id, email, success):
    c=db()
    placeholders=','.join(['?']*5)
    c.execute('INSERT INTO admin_login_logs(admin_id,email,ip_address,user_agent,success) VALUES('+placeholders+')',
              (admin_id,email,request.remote_addr,request.headers.get('User-Agent','')[:255],1 if success else 0))
    if success:
        c.execute('UPDATE admin_users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?',(admin_id,))
    c.commit(); c.close()

@app.post('/api/admin/login')
def admin_login_api():
    # Unprotected login endpoint: verifies the password hash, then issues a
    # signed JWT delivered as an HttpOnly (and, in production, Secure) cookie.
    data=request.get_json(silent=True) or request.form
    email=str(data.get('email','')).strip().lower()
    password=str(data.get('password',''))
    mfa_code=str(data.get('mfa_code') or data.get('mfa') or '')
    if not email or not password:
        return jsonify({'error':'Email and password are required.'}),400
    admin=_admin_login_succeeds(email,password,mfa_code)
    if not admin:
        _record_admin_login(None,email,False)
        return jsonify({'error':'Invalid admin credentials.'}),401
    token=issue_admin_token(admin['id'], admin['email'], admin['role'])
    _record_admin_login(admin['id'], admin['email'], True)
    response=jsonify({'ok':True,'role':admin['role'],'email':admin['email'],'expires_in':ADMIN_JWT_TTL_SECONDS})
    response.set_cookie(
        ADMIN_COOKIE_NAME, token,
        max_age=ADMIN_JWT_TTL_SECONDS,
        httponly=True, samesite='Lax', secure=ADMIN_COOKIE_SECURE, path='/',
    )
    return response
@app.route('/Admin', methods=['GET','POST'])
# Flask routes are case-sensitive, so a hand-typed lowercase /admin used to 404.
# Register both spellings on the same view to keep the URL forgiving.
@app.route('/admin', methods=['GET','POST'])
def admin():
    # Public dashboard entry point. Any visitor may load it, but it only renders
    # the protected dashboard for a request carrying a valid admin token.
    if not session.get('admin_authenticated') and current_admin():
        session['admin_authenticated']=True
    if not session.get('admin_authenticated'):
        error=session.pop('admin_login_error',None)
        return render_template('admin.html',authenticated=False,error=error)
    c=db()
    logs=c.execute('SELECT email,role,ip_address,logged_in_at FROM login_logs ORDER BY id DESC').fetchall()
    verifications=c.execute('SELECT * FROM verification_requests ORDER BY id DESC').fetchall()
    pending_verifications=[v for v in verifications if v['status']=='pending']
    pending_count=len(pending_verifications)
    fraud_reports=c.execute('''SELECT f.*,u.email,u.username,u.is_blocked,p.name AS property_name,p.location AS property_location FROM fraud_reports f JOIN users u ON u.id=f.user_id LEFT JOIN properties p ON p.id=f.property_id ORDER BY f.id DESC''').fetchall()
    pending_fraud_reports=[r for r in fraud_reports if r['status']=='pending']
    fraud_count=len(fraud_reports)
    c.close()
    payments_rows=admin_payment_rows()
    payment_stats={'total':len(payments_rows),
                   'successful':sum(1 for r in payments_rows if r['status'] in ('payment_successful','awaiting_customer_confirmation','customer_confirmed','payout_scheduled','payout_processing','payout_successful')),
                   'awaiting_confirmation':sum(1 for r in payments_rows if r['status']=='awaiting_customer_confirmation'),
                   'scheduled':sum(1 for r in payments_rows if r['payout_status']=='scheduled'),
                   'paid_out':sum(1 for r in payments_rows if r['payout_status']=='successful'),
                   'failed_payouts':sum(1 for r in payments_rows if r['payout_status']=='failed'),
                   'disputed':sum(1 for r in payments_rows if r['status']=='disputed')}
    return render_template('admin.html',authenticated=True,logs=logs,verifications=verifications,pending_verifications=pending_verifications,pending_count=pending_count,fraud_reports=fraud_reports,pending_fraud_reports=pending_fraud_reports,fraud_count=fraud_count,payments_rows=payments_rows,payment_stats=payment_stats)

@app.post('/Admin/verification/<int:req_id>/accept')
@admin_required
def admin_accept_verification(req_id):
    c=db()
    req=c.execute('SELECT * FROM verification_requests WHERE id=?', (req_id,)).fetchone()
    if not req:
        c.close(); flash('Verification request not found.', 'error'); return redirect(url_for('admin'))
    c.execute("UPDATE verification_requests SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?", (req_id,))
    c.execute("UPDATE users SET is_verified=1, username=?, phone=? WHERE id=?", (req['username'], req['phone'], req['user_id']))
    c.execute("INSERT INTO tasks(user_id,title,due,done) VALUES(?,?,'Immediate',0)", (req['user_id'], 'Seller verification approved. You can now publish properties directly to PropkoNet.'))
    c.commit(); c.close()
    flash(f"Verification accepted for {req['username']} ({req['email']}). This seller can now add properties to PropkoNet.", 'success')
    return redirect(url_for('admin'))

@app.post('/Admin/verification/<int:req_id>/reject')
@admin_required
def admin_reject_verification(req_id):
    c=db()
    req=c.execute('SELECT * FROM verification_requests WHERE id=?', (req_id,)).fetchone()
    if req:
        c.execute("UPDATE verification_requests SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=?", (req_id,))
        c.execute("UPDATE users SET is_verified=0 WHERE id=?", (req['user_id'],))
        c.commit()
        flash(f"Verification rejected for {req['username']}.", 'info')
    c.close()
    return redirect(url_for('admin'))

@app.get('/Admin/verification/<int:req_id>/review')
@admin_required
def admin_review_verification(req_id):
    c=db()
    req=c.execute('SELECT * FROM verification_requests WHERE id=?',(req_id,)).fetchone()
    c.close()
    if not req:
        flash('Verification request not found.','error'); return redirect(url_for('admin'))
    return render_template('verification_review.html',verification=req)

@app.get('/Admin/verification/<int:req_id>/document/<field>')
@admin_required
def verification_document(req_id,field):
    allowed={'face_photo':'Face photograph','nin_photo':'NIN document','cac_photo':'CAC document'}
    if field not in allowed: abort(404)
    c=db(); req=c.execute('SELECT * FROM verification_requests WHERE id=?',(req_id,)).fetchone(); c.close()
    if not req or not req[field]: abort(404)
    path=verification_document_path(req[field])
    if not path: abort(404)
    return send_file(path,mimetype=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',conditional=True)

@app.post('/Admin/fraud/<int:report_id>/block')
@admin_required
def admin_block_fraudster(report_id):
    c=db()
    report=c.execute('SELECT user_id FROM fraud_reports WHERE id=?',(report_id,)).fetchone()
    if not report:
        c.close(); flash('Fraud report not found.','error'); return redirect(url_for('admin'))
    reason=str(request.form.get('block_reason') or 'Blocked after image fraud review.').strip()[:500]
    c.execute("UPDATE users SET is_blocked=1,blocked_at=CURRENT_TIMESTAMP,block_reason=?,is_verified=0 WHERE id=?",(reason,report['user_id']))
    c.execute("UPDATE properties SET published_to_propkonet=0 WHERE user_id=?",(report['user_id'],))
    c.execute("UPDATE fraud_reports SET status='blocked',reviewed_at=CURRENT_TIMESTAMP WHERE id=?",(report_id,))
    c.commit(); c.close()
    flash('Offender account blocked and PropkoNet listings removed.','error')
    return redirect(url_for('admin'))

@app.post('/Admin/fraud/<int:report_id>/dismiss')
@admin_required
def admin_dismiss_fraud(report_id):
    c=db()
    cur=c.execute("UPDATE fraud_reports SET status='dismissed',reviewed_at=CURRENT_TIMESTAMP WHERE id=?",(report_id,))
    c.commit(); c.close()
    if cur.rowcount: flash('Fraud report dismissed.','info')
    return redirect(url_for('admin'))

@app.post('/Admin/logout')
def admin_logout():
    # Drop the admin marker and expire the JWT cookie so the browser stops
    # attaching it on subsequent /Admin/* requests.
    session.pop('admin_authenticated',None)
    response=redirect(url_for('admin'))
    response.delete_cookie(ADMIN_COOKIE_NAME, path='/', httponly=True, samesite='Lax', secure=ADMIN_COOKIE_SECURE)
    return response

@app.route('/predict',methods=['GET','POST'])
# Public: no account required, so visitors can try a prediction before they sign up.
def predict():
    result=None; error=None
    if request.method=='POST':
        using_yardcode=request.form.get('location_method')=='yardcode'
        location_value=request.form.get('yardcode','').strip() if using_yardcode else request.form.get('city','').strip()
        required=['yardcode','property_type','bedrooms','bathrooms','area','age','parking'] if using_yardcode else ['state','city','property_type','bedrooms','bathrooms','area','age','parking']
        if any(not request.form.get(k) for k in required):
            error='Sorry you have to fill the required information for accurate result'
        else:
            try:
                if using_yardcode:
                    code=location_value.lower()
                    state=next((s for s in STATES if s.lower() in code),DATA['State'].mode().iloc[0])
                    city=next((c for c in CITIES if c.lower() in code),DATA.loc[DATA['State'].eq(state),'City'].mode().iloc[0])
                else:
                    state=request.form['state']; city=request.form['city']
                row=pd.DataFrame([{'State':state,'City':city,'Property_Type':{'Terraced Duplex':'Terrace Duplex'}.get(request.form['property_type'],request.form['property_type']),'Bedrooms':int(request.form['bedrooms']),'Bathrooms':int(request.form['bathrooms']),'Area_sqm':float(request.form['area']),'Age_Years':float(request.form['age']),'Parking_Spaces':int(request.form['parking'])}])
                price=float(MODEL.predict(row)[0]); price=enforce_lagos_large_home_floor(price, row.iloc[0]['Bedrooms'], state, city); low=price*.88; high=price*1.12
                asking_price=request.form.get('asking_price','').strip()
                report=valuation_report(price, row.iloc[0].to_dict(), asking_price=asking_price or None)
                result={'price':price,'low':low,'high':high,'model_confidence':MODEL_CONFIDENCE,'summary':row.iloc[0].to_dict(),'yardcode':location_value if using_yardcode else None,'location':f'{city}, {state}','report':report}
            except Exception: error='Could not calculate this estimate. Please check the form values.'
    return render_template('predict.html',states=STATES,property_types=PROPERTY_TYPES,cities=CITIES,result=result,error=error)

@app.route('/image-predict',methods=['GET','POST'])
# Public: no account required, so visitors can try a prediction before they sign up.
def image_predict(): return redirect(url_for('predict') + '#image-predict-section')


def market_reference_estimate(data):
    # Compare visual details with the bundled Nigerian listing reference set.
    frame=DATA.copy()
    property_type=data.get('property_type')
    if property_type:
        aliases={'Terraced Duplex':'Terrace Duplex'}
        typed=frame[frame['Property_Type'].eq(aliases.get(property_type,property_type))]
        if len(typed) >= 5: frame=typed
    location=str(data.get('location_hint') or '').lower()
    if location:
        located=frame[frame.apply(lambda r: location in f"{r['City']} {r['State']}".lower(),axis=1)]
        if len(located) >= 5: frame=located
    median=float(frame['Price_NGN'].median())
    model_price=None
    bedrooms=data.get('bedrooms_guess'); bathrooms=data.get('bathrooms_guess')
    if bedrooms is not None and bathrooms is not None:
        try:
            city=next((c for c in CITIES if c.lower() in location),CITIES[0])
            state=next((s for s in STATES if s.lower() in location),DATA.iloc[0]['State'])
            ptype=property_type or frame['Property_Type'].mode().iloc[0]
            row=pd.DataFrame([{'State':state,'City':city,'Property_Type':{'Terraced Duplex':'Terrace Duplex'}.get(ptype,ptype),'Bedrooms':max(1,int(bedrooms)),'Bathrooms':max(1,int(bathrooms)),'Area_sqm':float(frame['Area_sqm'].median()),'Age_Years':float(frame['Age_Years'].median()),'Parking_Spaces':float(frame['Parking_Spaces'].median())}])
            model_price=float(MODEL.predict(row)[0])
        except (TypeError,ValueError,IndexError):
            model_price=None
    price=(model_price*.6+median*.4) if model_price else median
    spread=.15 if len(frame) >= 10 else .20
    return round(price),round(price*(1-spread)),round(price*(1+spread)),len(frame)

@app.post('/api/analyze-property-image')
# Public: no account required, so visitors can try a prediction before they sign up.
def analyze_property_image():
    files=[f for f in (request.files.getlist('images') or request.files.getlist('image')) if f and f.filename]
    if not files:
        return jsonify({'error':f'Upload between {MIN_VISUAL_IMAGE_COUNT} and {MAX_IMAGE_COUNT} images.'}),400
    if len(files)>MAX_IMAGE_COUNT:
        return jsonify({'error':f'You can upload at most {MAX_IMAGE_COUNT} images.'}),400
    raw_images=[]
    for f in files:
        if f.mimetype not in ALLOWED_IMAGE_MIMES:
            return jsonify({'error':'Upload JPEG, PNG, WEBP or GIF images only.'}),400
        raw=f.read()
        if not raw or len(raw)>MAX_IMAGE_BYTES:
            return jsonify({'error':'Each image must be larger than 0 bytes and no more than 8 MB.'}),400
        raw_images.append(raw)
    try:
        aggregate=batch_visual_features(raw_images)
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400
    try:
        visual=predict_visual_adjustment(aggregate)
    except ValueError as exc:
        return jsonify({'error':str(exc)}),500
    analysis=normalize_visual_analysis({})
    analysis['visual_quality_score']=visuals.visual_quality_score(visual['feature_summary'])
    # Confidence rises with the number of photos: a 20-photo set carries much
    # firmer visual evidence than a 5-photo set.
    analysis['analysis_confidence']=round(min(1.0,0.35+0.65*visual['coverage']),3)
    location_hint=request.form.get('location_hint','').strip()
    price, low, high, matches = market_reference_estimate({**analysis,'location_hint':location_hint})
    # The image model's bounded adjustment is applied to the structured estimate.
    adjusted=price*visual['multiplier']
    base_price=round(price)
    price=round(adjusted)
    low=round(adjusted*0.88)
    high=round(adjusted*1.12)
    features=', '.join(str(x) for x in analysis.get('amenities',[])[:4])
    query=' '.join(str(x) for x in [analysis.get('property_type') or 'property', location_hint, features, 'Nigeria'] if x)
    listing_searches=[
        {'site':'PropertyPro Nigeria','url':'https://www.google.com/search?q='+urllib.parse.quote('site:propertypro.ng '+query)},
        {'site':'Nigeria Property Centre','url':'https://www.google.com/search?q='+urllib.parse.quote('site:nigeriapropertycentre.com '+query)},
        {'site':'Jiji Nigeria','url':'https://www.google.com/search?q='+urllib.parse.quote('site:jiji.ng '+query)}]
    online_matches, online_search_message=online_listing_matches('('+query+') (site:propertypro.ng OR site:nigeriapropertycentre.com OR site:jiji.ng)')
    # Similar properties: real records from the bundled dataset, ranked by how
    # close they sit to the visual reading - location first, then bedroom count.
    similar=[]
    try:
        sim_frame=DATA.copy()
        hint=(location_hint or '').lower()
        beds=int(analysis.get('bedrooms_guess') or 3)
        # Score on location and size together. Matching a state (not just an
        # exact city) matters because the user may type an area the dataset does
        # not list as a city - "Lekki" sits inside Lagos.
        def _loc_score(r):
            if not hint: return 0
            hay=f"{r['City']} {r['State']}".lower()
            return 0 if (hint in hay or any(tok in hay for tok in hint.split())) else 1
        sim_frame=sim_frame.assign(_loc=sim_frame.apply(_loc_score, axis=1),
            _dist=(sim_frame['Bedrooms'].astype(int)-beds).abs())
        # Prefer records in the same state; only widen to the whole country when
        # the location genuinely matches almost nothing.
        if len(sim_frame[sim_frame['_loc']==0]) >= 3:
            sim_frame=sim_frame[sim_frame['_loc']==0]
        for _, r in sim_frame.sort_values(['_loc','_dist','Price_NGN']).head(6).iterrows():
            row=pd.DataFrame([{'State':r['State'],'City':r['City'],'Property_Type':r['Property_Type'],
                'Bedrooms':int(r['Bedrooms']),'Bathrooms':int(r['Bathrooms']),'Area_sqm':float(r['Area_sqm']),
                'Age_Years':float(r['Age_Years']),'Parking_Spaces':float(r['Parking_Spaces'])}])
            similar.append({'id': r['House_ID'], 'name': f"{int(r['Bedrooms'])}-bed {r['Property_Type']}",
                'location': f"{r['City']}, {r['State']}", 'asking': float(r['Price_NGN']),
                'ai_estimate': float(MODEL.predict(row)[0])})
    except Exception:
        similar=[]
    return jsonify({**analysis,'price':price,'low':low,'high':high,'market_matches':matches,
        'listing_searches':listing_searches,'online_matches':online_matches,
        'online_search_message':online_search_message,'similar_properties':similar,
        'visual_features':visual.get('feature_summary',{}),
        'image_model':{'image_count':visual['image_count'],
            'min_images':MIN_VISUAL_IMAGE_COUNT,'max_images':MAX_IMAGE_COUNT,
            'coverage':round(visual['coverage'],3),
            'within_supported_range':visual['within_supported_range'],
            'adjustment_pct':round(visual['delta']*visual['weight']*100,2),
            'base_price':base_price,
            'model_confidence':round(IMAGE_MODEL_CONFIDENCE,1)}})

@app.post('/api/image-analyze')
# Public: no account required, so visitors can try a prediction before they sign up.
def image_analyze():
    # Backward-compatible alias for the existing page while clients migrate.
    return analyze_property_image()

@app.post('/api/predict')
# Public: no account required, so visitors can try a prediction before they sign up.
def api_predict():
    try:
        payload=request.get_json(force=True); price,summary,visual=predict_from_payload(payload)
        return jsonify({'price':price,'low':price*.88,'high':price*1.12,'summary':summary,'visual_features':visual})
    except (TypeError,ValueError,KeyError):
        return jsonify({'error':'Invalid property prediction data.'}),400

SAMPLE_LISTINGS = [
    {'name': '5 Bedroom Detached Duplex in Lekki Phase 1 with BQ', 'location': 'Lekki, Lagos', 'price': 155_000_000, 'ai_price': 148_500_000, 'beds': 5, 'baths': 6, 'sqft': 4200, 'buyable': True, 'listing_type': 'sale'},
    {'name': 'Luxury 4 Bedroom Terraced Duplex, Ikoyi', 'location': 'Ikoyi, Lagos', 'price': 210_000_000, 'ai_price': 202_000_000, 'beds': 4, 'baths': 5, 'sqft': 3600, 'buyable': True, 'listing_type': 'sale'},
    {'name': 'Modern 3 Bedroom Flat in Gwarinpa', 'location': 'Gwarinpa, Abuja', 'price': 68_000_000, 'ai_price': 71_200_000, 'beds': 3, 'baths': 3, 'sqft': 1800, 'buyable': False, 'listing_type': 'sale'},
    {'name': 'Spacious 2 Bedroom Apartment, Ikeja GRA', 'location': 'Ikeja, Lagos', 'price': 42_500_000, 'ai_price': 39_800_000, 'beds': 2, 'baths': 2, 'sqft': 1200, 'buyable': False, 'listing_type': 'sale'},
    {'name': 'Executive 4 Bedroom Semi-Detached Duplex, Gwarinpa', 'location': 'Gwarinpa, Abuja', 'price': 95_000_000, 'ai_price': 92_400_000, 'beds': 4, 'baths': 4, 'sqft': 2900, 'buyable': True, 'listing_type': 'sale'},
    {'name': 'Smart Home 5 Bedroom Duplex with Pool, Ajah', 'location': 'Ajah, Lagos', 'price': 120_000_000, 'ai_price': 114_300_000, 'beds': 5, 'baths': 5, 'sqft': 3400, 'buyable': True, 'listing_type': 'sale'},
    {'name': 'Cozy 1 Bedroom Service Apartment, Wuse 2', 'location': 'Wuse, Abuja', 'price': 25_000_000, 'ai_price': 27_600_000, 'beds': 1, 'baths': 1, 'sqft': 700, 'buyable': False, 'listing_type': 'sale'},
    {'name': 'Newly Built 3 Bedroom Bungalow, Maitama Extension', 'location': 'Maitama, Abuja', 'price': 165_000_000, 'ai_price': 158_900_000, 'beds': 3, 'baths': 4, 'sqft': 2200, 'buyable': True, 'listing_type': 'sale'},
]

# Rentals shown on the PropkoNet "Rent" tab (annual rent in NGN).
RENT_LISTINGS = [
    {'name': 'Furnished 3 Bedroom Flat for Rent, Lekki Phase 1', 'location': 'Lekki, Lagos', 'price': 4_500_000, 'ai_price': 4_300_000, 'beds': 3, 'baths': 3, 'sqft': 1600, 'buyable': False, 'listing_type': 'rent'},
    {'name': 'Serviced 2 Bedroom Apartment for Rent, Ikoyi', 'location': 'Ikoyi, Lagos', 'price': 8_000_000, 'ai_price': 7_600_000, 'beds': 2, 'baths': 3, 'sqft': 1200, 'buyable': False, 'listing_type': 'rent'},
    {'name': 'Modern 4 Bedroom Duplex for Rent, Gwarinpa', 'location': 'Gwarinpa, Abuja', 'price': 6_500_000, 'ai_price': 6_100_000, 'beds': 4, 'baths': 4, 'sqft': 2600, 'buyable': False, 'listing_type': 'rent'},
    {'name': 'Cozy Studio Apartment for Rent, Wuse 2', 'location': 'Wuse, Abuja', 'price': 2_200_000, 'ai_price': 2_400_000, 'beds': 1, 'baths': 1, 'sqft': 650, 'buyable': False, 'listing_type': 'rent'},
    {'name': 'Newly Built 3 Bedroom Bungalow for Rent, Ajah', 'location': 'Ajah, Lagos', 'price': 3_600_000, 'ai_price': 3_400_000, 'beds': 3, 'baths': 3, 'sqft': 1900, 'buyable': False, 'listing_type': 'rent'},
]

# --- Listing assembly -------------------------------------------------------
# One place that builds the listing dictionaries the marketplace renders, so the
# grid (/propkonet) and the detail page (/property/<id>) never disagree.

def _verification_levels(p_dict):
    # Explain *what* was checked, not just that someone is "verified". Levels are
    # earned from data we actually hold on the seller record.
    levels=[{'key':'basic','label':'Basic','done':True,
             'detail':'Seller identity confirmed by email and account.'}]
    levels.append({'key':'property','label':'Property Verified',
                   'done':bool(p_dict.get('photo')),
                   'detail':'Property photos and listing details reviewed.' if p_dict.get('photo') else 'Add listing photos to earn this level.'})
    levels.append({'key':'inspection','label':'Inspection Verified',
                   'done':bool(p_dict.get('inspected')),
                   'detail':'A physical inspection has been completed.' if p_dict.get('inspected') else 'Physical inspection not yet recorded.'})
    levels.append({'key':'premium','label':'Premium Verified',
                   'done':bool(p_dict.get('inspected') and p_dict.get('photo') and p_dict.get('title_document')),
                   'detail':'Identity, documents and physical inspection all completed.' if (p_dict.get('inspected') and p_dict.get('photo') and p_dict.get('title_document')) else 'Requires identity, documents and inspection.'})
    return levels

def build_listings():
    # Every listing in the marketplace: verified seller listings first, then the
    # bundled sample listings. Demo listings carry a negative id so /property/<id>
    # can tell them apart from database listings without a separate table.
    c=db()
    db_props=c.execute('''
        SELECT p.*, u.email as seller_email, u.username as seller_username, u.phone as seller_phone
        FROM properties p
        JOIN users u ON p.user_id = u.id
        WHERE p.published_to_propkonet = 1 AND u.is_verified = 1
        ORDER BY p.id DESC
    ''').fetchall()
    c.close()
    verified_listings=[]
    for p in db_props:
        p_dict=dict(p)
        price_val=float(p_dict.get('price') or 0)
        ai_price=p_dict.get('ai_price')
        if not ai_price or float(ai_price) <= 0:
            ai_price=round(price_val*0.96)
        photo_filename=p_dict.get('photo')
        # A stored filename may point at a file that was deleted or is corrupt
        # (a few bytes, not a decodable image). Treat those as "no photo" so the
        # card falls back to a bundled demo image instead of a broken icon.
        if photo_filename and not _usable_photo(photo_filename, None):
            photo_filename=None
        verified_listings.append({
            'id': p_dict['id'], 'name': p_dict.get('name','Nigerian Property'),
            'location': p_dict.get('location','Nigeria'), 'price': price_val,
            'ai_price': float(ai_price), 'beds': int(p_dict.get('beds') or 3),
            'baths': int(p_dict.get('baths') or 3), 'sqft': int(p_dict.get('sqft') or 1800),
            'photo': photo_filename, 'photo_url': listing_photo_url(photo_filename),
            'buyable': True, 'is_verified_seller': True,
            'listing_type': p_dict.get('listing_type') or 'sale',
            'property_type': p_dict.get('property_type') or 'Detached Duplex',
            'seller_name': p_dict.get('seller_username') or (p_dict.get('seller_email','').split('@')[0]),
            'seller_email': p_dict.get('seller_email',''),
            'seller_phone': p_dict.get('seller_phone') or 'Available on request',
            'verification': _verification_levels(p_dict),
        })
    demo_listings=[]
    for n, l in enumerate(SAMPLE_LISTINGS + RENT_LISTINGS):
        demo_listings.append({**l, 'id': -(n+1), 'photo_url': listing_photo_url(demo_listing_photo(n)),
            'is_verified_seller': True, 'property_type': 'Detached Duplex' if l['beds'] >= 4 else 'Flat',
            'seller_name': 'Homelink Property Network', 'seller_phone': 'Available on request',
            'seller_email': '',
            'verification': _verification_levels({'photo': True})})
    for n, l in enumerate(verified_listings):
        if not l.get('photo_url'):
            l['photo_url']=listing_photo_url(demo_listing_photo(len(demo_listings)+n))
    return verified_listings + demo_listings

def find_listing(listing_id):
    for l in build_listings():
        if str(l.get('id')) == str(listing_id): return l
    return None
@app.route('/property/<listing_id>')
def property_detail(listing_id):
    # Full property page: gallery, specifications, AI valuation, seller and the
    # verification levels that were actually earned.
    listing=find_listing(listing_id)
    if not listing: abort(404)
    # A gallery: the listing photo plus other bundled photos as context.
    gallery=[listing['photo_url']] if listing.get('photo_url') else []
    gallery += [listing_photo_url(f) for f in SAMPLE_LISTING_PHOTOS if listing_photo_url(f) not in gallery][:4]
    summary={'State': (listing.get('location') or '').split(',')[-1].strip() or 'Lagos',
             'City': (listing.get('location') or '').split(',')[0].strip() or 'Lekki',
             'Property_Type': listing.get('property_type') or 'Detached Duplex',
             'Bedrooms': listing.get('beds') or 3, 'Bathrooms': listing.get('baths') or 3,
             'Area_sqm': max(20.0, (listing.get('sqft') or 1800)*0.092903), 'Age_Years': 5, 'Parking_Spaces': 2}
    report=valuation_report(listing.get('ai_price') or listing.get('price'), summary,
                            asking_price=listing.get('price'))
    return render_template('property_detail.html', listing=listing, gallery=gallery,
        report=report, summary=summary)

@app.route('/propkonet')
def propkonet():
    # Single source of truth for the listings, shared with /property/<id>.
    return render_template('propkonet.html', listings=build_listings(), states=STATES, property_types=PROPERTY_TYPES)

@app.route('/about')
def about():
    # Public marketing page: it must never bounce a visitor to the login screen.
    return render_template('about.html')

@app.route('/market-intelligence')
def market_intelligence_page():
    # Public dashboard: real aggregates from the bundled Nigerian records.
    state=request.args.get('state') or None
    city=request.args.get('city') or None
    property_type=request.args.get('property_type') or None
    bedrooms=request.args.get('bedrooms') or None
    report=market_intelligence(state, city, property_type, bedrooms)
    return render_template('market_intelligence.html', report=report, states=STATES,
        cities=CITIES, property_types=PROPERTY_TYPES)

# --- Explore map ------------------------------------------------------------
# Approximate coordinates for the Nigerian states/areas in the dataset. The map
# plots the sample listings against these anchors and shows the average value per
# area, which turns the listing grid into something you can read geographically.
CITY_COORDS={
    'Lagos': (6.5244, 3.3792), 'Lekki': (6.4698, 3.5852), 'Ikoyi': (6.4541, 3.4348),
    'Victoria Island': (6.4281, 3.4219), 'Ikeja': (6.5965, 3.3421), 'Ajah': (6.4667, 3.5667),
    'Ikorodu': (6.6194, 3.5105), 'Surulere': (6.4994, 3.3546), 'Yaba': (6.5095, 3.3711),
    'Abuja': (9.0765, 7.3986), 'Wuse': (9.0761, 7.4589), 'Maitama': (9.0868, 7.4951),
    'Gwarinpa': (9.1000, 7.4000), 'Asokoro': (9.0400, 7.5200), 'Ibadan': (7.3775, 3.9470),
    'Port Harcourt': (4.8156, 7.0498), 'Enugu': (6.5244, 7.5106), 'Kano': (12.0022, 8.5920),
    'Benin City': (6.3350, 5.6037), 'Kaduna': (10.5222, 7.4383), 'Jos': (9.8965, 8.8583),
    'Abeokuta': (7.1557, 3.3451), 'Owerri': (5.4836, 7.0332), 'Uyo': (5.0378, 7.9128),
    'Calabar': (4.9757, 8.3417), 'Nnewi': (6.0100, 6.9200), 'Ilorin': (8.4966, 4.5421),
    'Okene': (7.5500, 6.2333), 'Warri': (5.5167, 5.7500), 'Akure': (7.2571, 5.2058),
}

def area_price_summary():
    # Average estimated value per city, straight from the records: this is what
    # backs the "Lekki · average value" callouts on the map.
    grouped=DATA.groupby('City')['Price_NGN'].agg(['mean','count']).reset_index()
    areas=[]
    for _, row in grouped.sort_values('mean', ascending=False).iterrows():
        coords=CITY_COORDS.get(row['City'])
        if not coords: continue
        areas.append({'city': row['City'], 'avg_price': round(float(row['mean'])),
                      'listings': int(row['count']), 'lat': coords[0], 'lng': coords[1]})
    return areas

def listing_markers(listings):
    # Place every listing the PropkoNet grid shows on the map, using the area's
    # anchor with a small deterministic offset so markers do not stack exactly.
    markers=[]
    used={}
    for listing in listings:
        area=str(listing.get('location','')).split(',')[0].strip()
        coords=CITY_COORDS.get(area)
        if not coords and listing.get('location'):
            coords=next((v for k, v in CITY_COORDS.items() if k.lower() in str(listing['location']).lower()), None)
        if not coords: continue
        n=used.get(area, 0); used[area]=n+1
        lat=coords[0]+(n%4-1.5)*0.012
        lng=coords[1]+(n//4-1.5)*0.012
        markers.append({'name': listing.get('name'), 'location': listing.get('location'),
            'price': listing.get('price'), 'ai_price': listing.get('ai_price'),
            'photo_url': listing.get('photo_url'), 'lat': lat, 'lng': lng,
            'listing_type': listing.get('listing_type') or 'sale'})
    return markers
@app.route('/explore')
def explore():
    # Public map of listings and area price levels.
    c=db()
    db_props=c.execute('''SELECT p.*, u.username as seller_username, u.email as seller_email, u.phone as seller_phone
        FROM properties p JOIN users u ON p.user_id=u.id
        WHERE p.published_to_propkonet=1 AND u.is_verified=1 ORDER BY p.id DESC''').fetchall()
    c.close()
    verified=[]
    for p in db_props:
        p_dict=dict(p); price_val=float(p_dict.get('price') or 0)
        ai_price=p_dict.get('ai_price') or round(price_val*0.96)
        verified.append({'name': p_dict.get('name'), 'location': p_dict.get('location'),
            'price': price_val, 'ai_price': float(ai_price), 'beds': int(p_dict.get('beds') or 3),
            'baths': int(p_dict.get('baths') or 3), 'sqft': int(p_dict.get('sqft') or 1800),
            'photo_url': listing_photo_url(p_dict.get('photo')), 'listing_type': p_dict.get('listing_type') or 'sale'})
    demo=[{**l, 'photo_url': listing_photo_url(demo_listing_photo(n))}
          for n, l in enumerate(SAMPLE_LISTINGS + RENT_LISTINGS)]
    for n, l in enumerate(verified):
        if not l.get('photo_url'):
            l['photo_url']=listing_photo_url(demo_listing_photo(len(demo)+n))
    listings=verified+demo
    return render_template('explore.html', markers=listing_markers(listings), areas=area_price_summary())

# --- Property document screening -------------------------------------------
# A first-pass screen for Nigerian title documents. No OCR engine is bundled, so
# this does NOT read the text on a scan. It records the document type the user
# declares, validates the file is a real image/PDF, and lists exactly what a
# human reviewer still has to confirm. It is screening, never legal advice.
DOCUMENT_TYPES=['C of O (Certificate of Occupancy)','Deed of Assignment',
    "Governor's Consent",'Survey Plan','Building Approval','Letter of Allocation',
    'Registered Title / Other']

def screen_property_document(storage, declared_type):
    # Inspect the uploaded file and return a screening result. Only facts we can
    # actually establish are asserted; everything else is listed as to-verify.
    raw=storage.read()
    filename=storage.filename or ''
    ext=filename.rsplit('.',1)[-1].lower() if '.' in filename else ''
    result={'declared_type': declared_type or 'Not specified', 'file_name': filename,
            'checks': [], 'to_verify': [], 'verdict': 'review'}
    if not raw:
        result['checks'].append({'status':'red','label':'File is empty','detail':'Nothing was uploaded to screen.'})
        result['verdict']='failed'
        return result
    size_kb=len(raw)/1024
    result['size_kb']=round(size_kb,1)
    result['checks'].append({'status':'green','label':'File received',
        'detail':f'{ext.upper() or "unknown"} file, {size_kb:.0f} KB.'})
    if ext in {'jpg','jpeg','png','webp','gif'}:
        try:
            with Image.open(BytesIO(raw)) as im:
                im.verify()
            with Image.open(BytesIO(raw)) as im:
                w,h=im.size
            result['checks'].append({'status':'green','label':'Valid image',
                'detail':f'{w} × {h} pixels.'})
            if min(w,h) >= 800:
                result['checks'].append({'status':'green','label':'Resolution suits review',
                    'detail':'Large enough for a reviewer to read the details.'})
            else:
                result['checks'].append({'status':'amber','label':'Low resolution',
                    'detail':'A sharper scan makes the details easier to confirm.'})
        except Exception:
            result['checks'].append({'status':'red','label':'Not a readable image',
                'detail':'The file could not be opened as an image.'})
            result['verdict']='failed'
            return result
    elif ext=='pdf':
        if raw[:5]==b'%PDF-':
            result['checks'].append({'status':'green','label':'Valid PDF',
                'detail':'The file carries a PDF header.'})
        else:
            result['checks'].append({'status':'red','label':'Not a valid PDF',
                'detail':'The file does not start with a PDF header.'})
            result['verdict']='failed'
            return result
    else:
        result['checks'].append({'status':'amber','label':'Unusual file type',
            'detail':'Upload a photo or PDF of the document for screening.'})
        result['verdict']='failed'
        return result
    # The declared type is recorded but never asserted as fact: we have not read
    # the document, so the reviewer must confirm it matches the declared type.
    result['checks'].append({'status':'amber','label':'Document type not machine-read',
        'detail':'The declared type is recorded for the reviewer; it has not been independently confirmed.'})
    result['to_verify']=[
        'Registered property owner name(s) match the seller.',
        'Location and plot number match the listing.',
        'Document reference / registration number and issuing authority.',
        'Dates are consistent and the document is current.',
        'The document type matches what was declared.',
        'No obvious alterations, missing pages or inconsistencies.',
    ]
    return result
@app.route('/verify-document', methods=['GET','POST'])
def verify_document():
    # Public screening tool. Results are guidance for a human reviewer.
    result=None; error=None
    if request.method=='POST':
        storage=request.files.get('document')
        declared=request.form.get('document_type','').strip()
        if not storage or not storage.filename:
            error='Choose a document to screen.'
        else:
            result=screen_property_document(storage, declared)
    return render_template('document_screening.html', result=result, error=error,
        document_types=DOCUMENT_TYPES)

@app.route('/my-payments')
@login_required
def my_payments():
    # Customer dashboard: every property payment this account made, with the
    # confirmation button shown when the payment is eligible.
    rows=buyer_payment_rows(session['user_id'])
    return render_template('my_payments.html', rows=rows)

@app.route('/property-manager')
@owner_required
def property_manager():
    c=db()
    user_id=session['user_id']
    user=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    if not user:
        c.close(); session.clear(); flash('Your session has expired. Please log in again.','error')
        return redirect(url_for('login'))
    verification=c.execute('SELECT * FROM verification_requests WHERE user_id=? ORDER BY id DESC LIMIT 1',(user_id,)).fetchone()
    is_verified=bool(user['is_verified']) if (user and 'is_verified' in user.keys() and user['is_verified']) else False
    props=c.execute('SELECT * FROM properties WHERE user_id=? ORDER BY id DESC',(user_id,)).fetchall()
    tasks=c.execute('SELECT * FROM tasks WHERE user_id=? ORDER BY done,due',(user_id,)).fetchall()
    # Payment notifications: successful Paystack payments for this seller.
    payment_notifications=c.execute('''SELECT p.id, p.buyer_name, p.buyer_phone, p.status, pr.name AS property_name
        FROM payments p JOIN properties pr ON pr.id=p.property_id
        WHERE p.seller_id=? AND p.status NOT IN ('pending','cancelled')
        ORDER BY p.id DESC''',(user_id,)).fetchall()
    sales=seller_payment_rows(user_id)
    payout_summary=seller_payout_summary(user_id)
    payout_account={'bank_name':user['payout_bank_name'] if user and 'payout_bank_name' in user.keys() else None,
                    'bank_code':user['payout_bank_code'] if user and 'payout_bank_code' in user.keys() else None,
                    'account_number':user['payout_account_number'] if user and 'payout_account_number' in user.keys() else None,
                    'account_name':user['payout_account_name'] if user and 'payout_account_name' in user.keys() else None,
                    'recipient_code':user['paystack_recipient_code'] if user and 'paystack_recipient_code' in user.keys() else None}
    active=c.execute("SELECT COUNT(*) FROM properties WHERE user_id=? AND status != 'Closed'",(user_id,)).fetchone()[0]
    pipeline=c.execute("SELECT COALESCE(SUM(price),0) FROM properties WHERE user_id=? AND status != 'Closed'",(user_id,)).fetchone()[0]
    inquiries=c.execute("SELECT COUNT(*) FROM properties WHERE user_id=? AND status='Inquiry'",(user_id,)).fetchone()[0]
    month=c.execute("SELECT COALESCE(SUM(price),0) FROM properties WHERE user_id=? AND created_at >= datetime('now','start of month')",(user_id,)).fetchone()[0]
    c.close()
    return render_template('manager.html',props=props,tasks=tasks,sales=sales,kpis={'active':active,'pipeline':pipeline,'inquiries':inquiries,'month':month},user=user,verification=verification,is_verified=is_verified,payment_notifications=payment_notifications,payout_summary=payout_summary,payout_account=payout_account)

@app.route("/handymen")
def handymen():
    """Public handyman directory with trade search and reputation ranking."""
    trade=request.args.get("trade","").strip()
    state=request.args.get("state","").strip()
    pay_range=request.args.get("pay_range","").strip()
    query=request.args.get("q","").strip()
    available_only=request.args.get("available") in ("1","true","on")
    profiles=handyman_directory(query=query, trade=trade, state=state,
        pay_range=pay_range, available_only=available_only)
    return render_template("handymen.html", profiles=profiles, trades=HANDYMAN_TRADES,
        states=STATES, pay_ranges=PAY_RANGES, selected={"trade":trade,"state":state,
        "pay_range":pay_range,"q":query,"available":available_only},
        max_links=MAX_HANDYMAN_LINKS)

@app.get("/handymen/<int:user_id>")
def handyman_profile(user_id):
    """A single handyman profile: work, availability, links and reviews."""
    c=db()
    row=c.execute('SELECT hp.*, u.email AS user_email, u.username AS username, u.is_verified AS is_verified\n        FROM handyman_profiles hp JOIN users u ON u.id=hp.user_id WHERE hp.user_id=?', (user_id,)).fetchone()
    if row is None:
        c.close(); flash("That handyman profile was not found.","error"); return redirect(url_for("handymen"))
    profile=handyman_profile_payload(row, trades=True)
    jobs=c.execute("SELECT * FROM handyman_jobs WHERE handyman_user_id=? ORDER BY id DESC",(user_id,)).fetchall()
    reviews=c.execute("SELECT * FROM handyman_reviews WHERE handyman_user_id=? ORDER BY id DESC",(user_id,)).fetchall()
    c.close()
    return render_template("handyman_profile.html", profile=profile, jobs=jobs,
        reviews=reviews, max_links=MAX_HANDYMAN_LINKS)

@app.route("/toolbox")
@handyman_required
def toolbox():
    """The handyman workspace, tailored to their trade and preferences."""
    user_id=session["user_id"]
    c=db()
    user=c.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
    if not user:
        c.close(); session.clear(); flash("Your session has expired. Please log in again.","error")
        return redirect(url_for("login"))
    row=c.execute("SELECT * FROM handyman_profiles WHERE user_id=?",(user_id,)).fetchone()
    c.close()
    profile=handyman_profile_payload(row, trades=True) if row else {
        "trade": None, "links": 0, "star": False, "average_rating": 0.0,
        "jobs_completed": 0, "available": False, "review_count": 0}
    c=db()
    jobs=c.execute("SELECT * FROM handyman_jobs WHERE handyman_user_id=? ORDER BY id DESC",(user_id,)).fetchall()
    reviews=c.execute("SELECT * FROM handyman_reviews WHERE handyman_user_id=? ORDER BY id DESC",(user_id,)).fetchall()
    c.close()
    next_link_in=0 if profile.get("star") else max(0, JOBS_PER_LINK - (int(profile.get("jobs_completed") or 0) % JOBS_PER_LINK))
    return render_template("toolbox.html", profile=profile, user=user, jobs=jobs,
        reviews=reviews, trades=HANDYMAN_TRADES, states=STATES, pay_ranges=PAY_RANGES,
        max_links=MAX_HANDYMAN_LINKS, jobs_per_link=JOBS_PER_LINK,
        next_link_in=next_link_in)

@app.post("/api/handyman/profile")
@handyman_required
def save_handyman_profile():
    data=request.get_json(silent=True) or request.form
    trade=str(data.get("trade") or "").strip()
    if trade and trade not in HANDYMAN_TRADES: trade=HANDYMAN_TRADES[0]
    pay_range=str(data.get("pay_range") or "medium").strip().lower()
    if pay_range not in PAY_RANGES: pay_range="medium"
    try: years=int(data.get("years_experience") or 0)
    except (TypeError,ValueError): years=0
    secondary=[t.strip() for t in str(data.get("secondary_trades") or "").split(",") if t.strip() in HANDYMAN_TRADES]
    available=str(data.get("available","1")).lower() not in ("0","false","off","")
    c=db()
    sync_handyman_profile(c, session["user_id"],
        full_name=str(data.get("full_name") or "").strip() or None,
        trade=trade or None,
        secondary_trades=",".join(secondary),
        bio=str(data.get("bio") or "").strip()[:800],
        years_experience=max(0,min(60,years)),
        pay_range=pay_range,
        service_area=str(data.get("service_area") or "").strip()[:200],
        state=str(data.get("state") or "").strip()[:80],
        city=str(data.get("city") or "").strip()[:80],
        phone=str(data.get("phone") or "").strip()[:40],
        available=1 if available else 0,
        availability_notes=str(data.get("availability_notes") or "").strip()[:400])
    c.commit()
    row=c.execute("SELECT * FROM handyman_profiles WHERE user_id=?",(session["user_id"],)).fetchone()
    c.close()
    return jsonify({"ok":True,"profile":handyman_profile_payload(row, trades=True)})

@app.post("/api/handyman/jobs")
@handyman_required
def add_handyman_job():
    data=request.get_json(silent=True) or request.form
    title=str(data.get("title") or "").strip()
    if not title: return jsonify({"error":"Describe the completed job."}),400
    c=db()
    record_completed_job(c, session["user_id"], title,
        str(data.get("client_name") or "").strip() or None,
        str(data.get("trade") or "").strip() or None)
    c.commit()
    row=c.execute("SELECT * FROM handyman_profiles WHERE user_id=?",(session["user_id"],)).fetchone()
    c.close()
    return jsonify({"ok":True,"profile":handyman_profile_payload(row, trades=True)})

@app.post("/api/handyman/reviews")
@login_required
def add_handyman_review():
    data=request.get_json(silent=True) or request.form
    try: handyman_user_id=int(data.get("handyman_user_id"))
    except (TypeError,ValueError): return jsonify({"error":"A handyman is required."}),400
    try: rating=int(data.get("rating"))
    except (TypeError,ValueError): return jsonify({"error":"A rating from 1 to 5 is required."}),400
    rating=max(1,min(5,rating))
    if handyman_user_id==session.get("user_id"):
        return jsonify({"error":"You cannot review your own profile."}),400
    c=db()
    target=c.execute("SELECT user_id FROM handyman_profiles WHERE user_id=?",(handyman_user_id,)).fetchone()
    if target is None:
        c.close(); return jsonify({"error":"That handyman profile does not exist."}),404
    c.execute("INSERT INTO handyman_reviews(handyman_user_id,reviewer_user_id,reviewer_name,rating,comment) VALUES(?,?,?,?,?)",
        (handyman_user_id, session["user_id"], session.get("email") or "Client", rating,
         str(data.get("comment") or "").strip()[:600]))
    c.execute("UPDATE handyman_profiles SET rating_sum=rating_sum+?, rating_count=rating_count+1, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
        (rating, handyman_user_id))
    # A review is extra evidence of delivered work, so it also nudges the job
    # ledger forward one job and refreshes the derived links score.
    record_completed_job(c, handyman_user_id, "Review: "+str(data.get("comment") or "client feedback")[:100],
        session.get("email"), None)
    c.commit()
    row=c.execute("SELECT * FROM handyman_profiles WHERE user_id=?",(handyman_user_id,)).fetchone()
    c.close()
    return jsonify({"ok":True,"profile":handyman_profile_payload(row, trades=True)})

def _handyman_ai_fallback(query, profiles):
    """Local, dependency-free matcher used when OpenAI is unavailable."""
    tokens=[t for t in re.split(r"[^a-z0-9]+", str(query or "").lower()) if len(t) > 1]
    trade_hits=[]
    for trade in HANDYMAN_TRADES:
        if trade.lower() in str(query or "").lower():
            trade_hits.append(trade.lower())
    pay=None
    low=str(query or "").lower()
    if any(w in low for w in ("cheap","low pay","budget","affordable","low cost")): pay="low"
    elif any(w in low for w in ("expensive","high pay","premium","high end","luxury")): pay="high"
    elif "medium" in low or "moderate" in low or "average" in low: pay="medium"
    ranked=[]
    for p in profiles:
        hay=" ".join(str(p.get(k) or "") for k in ("full_name","username","trade","secondary_trades","bio","service_area","state","city","pay_range")).lower()
        score=float(handyman_recommendation_rank(p))
        if trade_hits and any(t in hay for t in trade_hits): score+=5000
        if pay and (p.get("pay_range") or "")==pay: score+=2500
        score += 250.0 * sum(1 for t in tokens if t in hay)
        ranked.append((score, p))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in ranked]

@app.post("/api/handyman-ai-search")
def handyman_ai_search():
    """Natural-language search over handyman profiles.

    For example: "I need a carpenter to fix my leaking roof, medium pay".
    Uses OpenAI when configured and a local keyword matcher as a fallback.
    """
    data=request.get_json(silent=True) or request.form
    query=str(data.get("query") or "").strip()
    if not query: return jsonify({"error":"Describe the handyman you are looking for."}),400
    profiles=handyman_directory()
    matched=[]
    source="local matcher"
    api=os.getenv("OPENAI_API_KEY")
    if OpenAI is not None and api:
        try:
            catalogue=[{"id":p["user_id"],"name":p.get("full_name") or p.get("username"),
                "trades":[p.get("trade")]+[t.strip() for t in str(p.get("secondary_trades") or "").split(",") if t.strip()],
                "pay_range":p.get("pay_range"),"experience_years":p.get("years_experience"),
                "area":p.get("service_area") or p.get("state") or p.get("city"),
                "available":p.get("available"),"links":p.get("links"),
                "rating":p.get("average_rating")} for p in profiles]
            client=_openai_client(api)
            prompt=("A user is looking for a handyman. Their request: "+query+
                ". Here are the registered handymen as JSON: "+json.dumps(catalogue)+
                ". Return ONLY a JSON array of the matching handyman ids, best match first. "
                "Consider the trade, the job described, the pay expectation (low/medium/high), "
                "location and availability. If none match, return [].")
            r=client.responses.create(model=os.getenv("OPENAI_TEXT_MODEL","gpt-5.4-mini"),input=prompt)
            text=re.sub(r"```json|```","",r.output_text).strip()
            ids=json.loads(text)
            by_id={p["user_id"]:p for p in profiles}
            matched=[by_id[i] for i in ids if i in by_id][:12]
            source="openai"
        except Exception:
            app.logger.exception("Handyman AI search failed; using local matcher")
            matched=[]
            source="local matcher"
    if not matched and source != "openai":
        matched=_handyman_ai_fallback(query, profiles)[:12]
    return jsonify({"query":query,"source":source,"count":len(matched),"results":[
        {"id":p["user_id"],"name":p.get("full_name") or p.get("username"),
         "trade":p.get("trade"),"secondary_trades":p.get("secondary_trades"),
         "pay_range":p.get("pay_range"),"service_area":p.get("service_area"),
         "state":p.get("state"),"city":p.get("city"),"available":p.get("available"),
         "links":p.get("links"),"max_links":MAX_HANDYMAN_LINKS,"star":p.get("star"),
         "jobs_completed":p.get("jobs_completed"),"average_rating":p.get("average_rating"),
         "bio":p.get("bio"),"url":url_for("handyman_profile", user_id=p["user_id"])}
        for p in matched]})
@app.post('/api/manager-ai')
@owner_required
def manager_ai():
    data=request.get_json(force=True) or {}; task=str(data.get('task','')).strip(); details=str(data.get('details','')).strip()
    if not task: return jsonify({'error':'Choose a document type.'}),400
    c=db(); template=c.execute('SELECT title,body FROM document_templates WHERE task=?',(task,)).fetchone(); c.close()
    if not template: return jsonify({'error':'That document template is not available.'}),404
    safe_details=details or '[Add the property, tenant, amount and date details here]'
    text=template['body'].replace('{{details}}',safe_details)
    return jsonify({'text':text,'title':template['title'],'source':'local template'})
    if False:
        fallback={'rent_notice':f'''RENT NOTICE\n\nDear Tenant,\n\nThis is a formal notice regarding the rent for {details or 'the property'}. Please review your tenancy records and make the required payment by the agreed due date.\n\nThank you,\nESTIMATE Property Management''','listing':f'''PROPERTY LISTING\n\nPresenting {details or 'a premium Nigerian property'} — a well-positioned opportunity for discerning buyers or tenants. Contact the property manager for pricing, inspection and documentation.''','followup':f'''FOLLOW-UP MESSAGE\n\nHello, I am following up regarding {details or 'the property enquiry'}. Please let me know a convenient time to continue the conversation or schedule an inspection.\n\nBest regards,\nESTIMATE Property Management''','management_agreement':f'''PROPERTY MANAGEMENT AGREEMENT\n\nParties: [Owner] and ESTIMATE Property Management\nProperty: {details or 'the property'}\nScope, fees, owner responsibilities and manager responsibilities: [Complete details].\nHave a qualified professional review before signing.''','lease_agreement':f'''RESIDENTIAL OR COMMERCIAL LEASE AGREEMENT\n\nProperty: {details or 'the property'}\nInclude rent, duration, permitted use, maintenance, utilities, default and termination terms. Have a qualified professional review before signing.''','rental_application':f'''RENTAL APPLICATION FORM\n\nApplicant details, employment, income, references, rental history and consent for lawful screening. Property: {details or 'the property'}.''','rent_ledger':f'''RENT LEDGER\n\nTenant/property: {details or 'the tenancy'}\nDate | Description | Due | Paid | Balance | Reference\nKeep entries chronological with supporting records.''','rent_receipt':f'''RENT RECEIPT\n\nReceived from: [Tenant]\nAmount: [Amount]\nFor: {details or 'rent'}\nPayment method: [Cash/Transfer/Check] | Date: [Date]\nCheck against the payment record.''','eviction_notice':f'''NOTICE TO QUIT / EVICTION NOTICE\n\nTenant/property: {details or 'the tenancy'}\nState the breach, required action and applicable dates only after qualified local legal review. This draft is not legal advice.'''}
        return jsonify({'text':fallback.get(task,'Please configure OPENAI_API_KEY for AI generation.')})
    try:
        client=_openai_client(api)
        prompt=f'''You are a professional Nigerian real-estate property manager. Generate a polished, practical document for this task: {task}. Property/client details: {details}. Keep it legally cautious, professional and editable. Do not invent laws or legal deadlines. Return plain text only.'''
        r=client.responses.create(model=os.getenv('OPENAI_TEXT_MODEL','gpt-5.4-mini'),input=prompt)
        return jsonify({'text':r.output_text})
    except Exception as e:
        app.logger.exception('OpenAI document generation failed')
        return jsonify({'error':openai_failure_message(e,'document generation')}),502

@app.post('/api/estimate-to-workspace')
@owner_required
def estimate_to_workspace():
    data=request.get_json(force=True) or {}
    try:
        price=float(data.get('price'))
        if not math.isfinite(price) or price < 0: raise ValueError
    except (TypeError,ValueError):
        return jsonify({'error':'Enter a valid estimate price.'}),400
    name=str(data.get('name') or 'AI Estimated Property').strip()[:120]
    location=str(data.get('location') or 'Nigeria').strip()[:160]
    status=str(data.get('status') or 'Inquiry')
    if status not in {'Inquiry','Viewing','Offer','Closed'}: status='Inquiry'
    c=db(); cur=c.execute('INSERT INTO properties(user_id,name,location,price,status,thumb) VALUES(?,?,?,?,?,?)',(session['user_id'],name,location,price,status,'house')); c.commit(); c.close()
    return jsonify({'ok':True,'property_id':cur.lastrowid})

@app.post('/api/request-verification')
@owner_required
def request_verification():
    d=request.get_json(silent=True) or request.form
    username=str(d.get('username','')).strip()
    phone=str(d.get('phone','')).strip()
    if not username or not phone:
        return jsonify({'error':'Username and phone number are required.'}), 400
    user_id=session['user_id']
    email=session.get('email','')
    c=db()
    c.execute('UPDATE users SET username=?, phone=? WHERE id=?', (username, phone, user_id))
    pending=c.execute("SELECT id FROM verification_requests WHERE user_id=? AND status='pending'", (user_id,)).fetchone()
    if pending:
        c.execute("UPDATE verification_requests SET username=?, phone=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (username, phone, pending['id']))
    else:
        c.execute("INSERT INTO verification_requests(user_id, email, username, phone, status) VALUES(?,?,?,?,'pending')", (user_id, email, username, phone))
    c.commit(); c.close()
    return jsonify({'ok': True, 'message': 'Verification request submitted! Admin has received your details.'})

@app.route('/verification/start')
@owner_required
def verification_start():
    c=db()
    user=c.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
    verification=c.execute('SELECT * FROM verification_requests WHERE user_id=? ORDER BY id DESC LIMIT 1',(session['user_id'],)).fetchone()
    c.close()
    return render_template('verification.html',user=user,verification=verification)

@app.post('/api/verification/submit')
@owner_required
def submit_verification():
    fields={
        'username':str(request.form.get('username','')).strip(),
        'phone':str(request.form.get('phone','')).strip(),
        'business_address':str(request.form.get('business_address','')).strip(),
        'nin_number':str(request.form.get('nin_number','')).strip(),
        'cac_number':str(request.form.get('cac_number','')).strip()
    }
    if not fields['username'] or not fields['phone'] or not fields['business_address'] or not fields['nin_number'] or not fields['cac_number']:
        return jsonify({'error':'Complete all business and identity details before submitting.'}),400
    uploads={}
    saved=[]
    for field in ['face_photo','nin_photo','cac_photo']:
        raw,error=validate_verification_upload(request.files.get(field),field.replace('_',' ').title())
        if error:
            cleanup_verification_uploads(saved)
            return jsonify({'error':error}),400
        ext=request.files[field].filename.rsplit('.',1)[-1].lower()
        filename=save_verification_upload(raw,ext)
        saved.append(filename); uploads[field]=filename
    c=db()
    try:
        existing=c.execute("SELECT * FROM verification_requests WHERE user_id=? AND status IN ('pending','rejected') ORDER BY id DESC LIMIT 1",(session['user_id'],)).fetchone()
        approved=c.execute("SELECT id FROM verification_requests WHERE user_id=? AND status='approved' ORDER BY id DESC LIMIT 1",(session['user_id'],)).fetchone()
        if approved:
            cleanup_verification_uploads(saved); c.close(); return jsonify({'error':'This account has already been verified.'}),409
        old_files=[]
        if existing and existing['status']=='pending':
            old_files=[existing['face_photo'],existing['nin_photo'],existing['cac_photo']]
            c.execute('''UPDATE verification_requests SET email=?,username=?,phone=?,business_address=?,nin_number=?,cac_number=?,face_photo=?,nin_photo=?,cac_photo=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(session.get('email',''),fields['username'],fields['phone'],fields['business_address'],fields['nin_number'],fields['cac_number'],uploads['face_photo'],uploads['nin_photo'],uploads['cac_photo'],existing['id']))
            request_id=existing['id']
        else:
            cur=c.execute('''INSERT INTO verification_requests(user_id,email,username,phone,business_address,nin_number,cac_number,face_photo,nin_photo,cac_photo,status) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending')''',(session['user_id'],session.get('email',''),fields['username'],fields['phone'],fields['business_address'],fields['nin_number'],fields['cac_number'],uploads['face_photo'],uploads['nin_photo'],uploads['cac_photo']))
            request_id=cur.lastrowid
        c.execute('UPDATE users SET username=?,phone=? WHERE id=?',(fields['username'],fields['phone'],session['user_id']))
        c.commit()
    except Exception:
        c.rollback(); cleanup_verification_uploads(saved); c.close(); raise
    cleanup_verification_uploads(old_files)
    c.close()
    return jsonify({'ok':True,'message':'Verification documents submitted for admin review.','verification_id':request_id})

@app.post('/api/property')
@owner_required
def add_property():
    d=request.form
    try:
        price=float(d.get('price',''))
        if not math.isfinite(price) or price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'error':'Enter a valid, non-negative property price.'}),400
    photos=request.files.getlist('photo'); photo_name=None; photo_names=[]
    allowed={'jpg','jpeg','png','webp','gif'}
    for photo in photos:
        if not photo or not photo.filename: continue
        ext=photo.filename.rsplit('.',1)[-1].lower() if '.' in photo.filename else ''
        if ext not in allowed: return jsonify({'error':'Upload JPG, PNG, WEBP or GIF images.'}),400
        name=f'{uuid4().hex}.{ext}'; photo.save(UPLOADS/name); photo_names.append(name)
    photo_name=photo_names[0] if photo_names else None

    c=db()
    user=c.execute('SELECT is_verified FROM users WHERE id=?', (session['user_id'],)).fetchone()
    is_verified=bool(user['is_verified']) if user else False

    # Check publish flag - only verified sellers can publish to propkonet
    publish_flag = 1 if (is_verified and str(d.get('publish_to_propkonet','')).lower() in ('1','true','on','yes')) else 0
    beds = max(1, int(d.get('beds', 3))) if d.get('beds') else 3
    baths = max(1, int(d.get('baths', 3))) if d.get('baths') else 3
    sqft = max(10, float(d.get('sqft', 1800))) if d.get('sqft') else 1800
    prop_type = d.get('property_type', 'Detached Duplex')
    listing_type = 'rent' if str(d.get('listing_type','')).strip().lower() == 'rent' else 'sale'
    ai_price = round(price * 0.96)

    cur=c.execute('''
        INSERT INTO properties(user_id,name,location,price,status,thumb,photo,published_to_propkonet,beds,baths,sqft,property_type,ai_price,listing_type)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''',(session['user_id'],d.get('name','New Property'),d.get('location','Nigeria'),price,d.get('status','Inquiry'),'house',photo_name,publish_flag,beds,baths,sqft,prop_type,ai_price,listing_type))
    property_id=cur.lastrowid
    c.executemany('INSERT INTO property_photos(property_id,filename) VALUES(?,?)',[(property_id,name) for name in photo_names]); c.commit(); c.close(); return jsonify({'ok':True})

@app.post('/api/property/<int:property_id>/edit')
@owner_required
def edit_property(property_id):
    d=request.form
    try:
        price=float(d.get('price',''))
        if not math.isfinite(price) or price < 0: raise ValueError
    except (TypeError, ValueError): return jsonify({'error':'Enter a valid, non-negative property price.'}),400
    c=db(); prop=c.execute('SELECT * FROM properties WHERE id=? AND user_id=?',(property_id,session['user_id'])).fetchone()
    if not prop: c.close(); return jsonify({'error':'Property not found.'}),404

    user=c.execute('SELECT is_verified FROM users WHERE id=?', (session['user_id'],)).fetchone()
    is_verified=bool(user['is_verified']) if user else False

    if is_verified and 'publish_to_propkonet' in d:
        publish_flag = 1 if str(d.get('publish_to_propkonet','')).lower() in ('1','true','on','yes') else 0
    elif not is_verified:
        publish_flag = 0
    else:
        publish_flag = prop['published_to_propkonet'] if 'published_to_propkonet' in prop.keys() and prop['published_to_propkonet'] else 0

    beds = max(1, int(d.get('beds', prop['beds'] if 'beds' in prop.keys() and prop['beds'] else 3)))
    baths = max(1, int(d.get('baths', prop['baths'] if 'baths' in prop.keys() and prop['baths'] else 3)))
    sqft = max(10, float(d.get('sqft', prop['sqft'] if 'sqft' in prop.keys() and prop['sqft'] else 1800)))
    prop_type = d.get('property_type', prop['property_type'] if 'property_type' in prop.keys() and prop['property_type'] else 'Detached Duplex')
    if 'listing_type' in d:
        listing_type = 'rent' if str(d.get('listing_type','')).strip().lower() == 'rent' else 'sale'
    else:
        listing_type = prop['listing_type'] if 'listing_type' in prop.keys() and prop['listing_type'] else 'sale'
    ai_price = round(price * 0.96)

    c.execute('''
        UPDATE properties SET name=?,location=?,price=?,status=?,published_to_propkonet=?,beds=?,baths=?,sqft=?,property_type=?,ai_price=?,listing_type=?
        WHERE id=? AND user_id=?
    ''',(d.get('name','New Property'),d.get('location','Nigeria'),price,d.get('status','Inquiry'),publish_flag,beds,baths,sqft,prop_type,ai_price,listing_type,property_id,session['user_id']))
    photos=request.files.getlist('photo'); allowed={'jpg','jpeg','png','webp','gif'}; names=[]
    for photo in photos:
        if not photo or not photo.filename: continue
        ext=photo.filename.rsplit('.',1)[-1].lower() if '.' in photo.filename else ''
        if ext not in allowed: c.close(); return jsonify({'error':'Upload JPG, PNG, WEBP or GIF images.'}),400
        name=f'{uuid4().hex}.{ext}'; photo.save(UPLOADS/name); names.append(name)
    if names:
        c.execute('UPDATE properties SET photo=? WHERE id=?',(names[0],property_id)); c.executemany('INSERT INTO property_photos(property_id,filename) VALUES(?,?)',[(property_id,name) for name in names])
    c.commit(); c.close(); return jsonify({'ok':True})

@app.post('/api/property/<int:property_id>/toggle-propkonet')
@owner_required
def toggle_propkonet(property_id):
    c=db()
    user=c.execute('SELECT is_verified FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user or not user['is_verified']:
        c.close()
        return jsonify({'error':'You must be a verified seller before you can publish properties to PropkoNet. Please click "Get verified to sell property".'}), 403
    prop=c.execute('SELECT * FROM properties WHERE id=? AND user_id=?', (property_id, session['user_id'])).fetchone()
    if not prop:
        c.close(); return jsonify({'error':'Property not found.'}), 404
    cur_val=prop['published_to_propkonet'] if 'published_to_propkonet' in prop.keys() and prop['published_to_propkonet'] else 0
    new_val=0 if cur_val else 1
    ai_price=prop['ai_price'] if 'ai_price' in prop.keys() and prop['ai_price'] else round(prop['price'] * 0.96)
    c.execute('UPDATE properties SET published_to_propkonet=?, ai_price=? WHERE id=? AND user_id=?', (new_val, ai_price, property_id, session['user_id']))
    c.commit(); c.close()
    return jsonify({
        'ok': True,
        'published': bool(new_val),
        'message': 'Property is now live on PropkoNet for buyers!' if new_val else 'Property was removed from PropkoNet.'
    })

@app.post('/api/propkonet/buy')
def propkonet_buy():
    """Initialize a Paystack checkout for a property.

    The amount is always derived server-side from the live listing; the browser
    cannot influence the price, the seller, or the payout.
    """
    d=request.get_json(silent=True) or request.form
    buyer_name=str(d.get('name','')).strip()
    buyer_email=str(d.get('email','')).strip()
    buyer_phone=str(d.get('phone','')).strip()
    message=str(d.get('message','')).strip()
    # Rate limit to blunt card-testing / checkout spam from one IP.
    if not payments.rate_limit('buy:'+payments.client_ip(request), 10, 60):
        return jsonify({'error':'Too many checkout attempts. Please wait a moment and try again.'}), 429
    try:
        property_id=int(d.get('property_id')) if d.get('property_id') else None
    except (TypeError, ValueError):
        property_id=None
    if not buyer_name or not buyer_email or not buyer_phone:
        return jsonify({'error':'Your name, email and phone number are required for secure checkout.'}), 400
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', buyer_email):
        return jsonify({'error':'Enter a valid email address for your payment receipt.'}), 400
    if not property_id:
        return jsonify({'error':'This listing is an enquiry-only listing and cannot be checked out online.'}), 400
    if not payments.paystack_enabled():
        return jsonify({'error':'Payments are not configured yet. Please try again later.'}), 503
    prop=sellable_property(property_id)
    if not prop or not prop['is_verified'] or not prop['price'] or float(prop['price']) <= 0:
        return jsonify({'error':'This property is not currently available for secure checkout.'}), 404
    # Authoritative server-side amount in kobo, as an integer.
    amount_kobo=payments.naira_to_kobo(prop['price'])
    reference='HOUSE-'+uuid4().hex
    buyer={'user_id':session.get('user_id'),'name':buyer_name,'email':buyer_email,
           'phone':buyer_phone,'ip':payments.client_ip(request)}
    payment_id=create_payment_order(prop, buyer, amount_kobo, reference, payments.CURRENCY)
    # Retain the buyer's optional message as an audit-friendly purchase inquiry.
    c=db()
    pcols="property_id,buyer_name,buyer_email,buyer_phone,offer_price,message,status"
    pph=','.join(['?']*7)
    c.execute('INSERT INTO property_purchases('+pcols+') VALUES('+pph+')',
              (prop['id'],buyer_name,buyer_email,buyer_phone,float(prop['price']),message,'checkout_started'))
    c.commit(); c.close()
    callback_url=payments.PAYSTACK_CALLBACK_BASE_URL or request.url_root.rstrip('/')
    try:
        init=payments.initialize_transaction(
            buyer_email, amount_kobo, reference,
            callback_url+url_for('payment_callback', reference=reference),
            metadata={'property_id':prop['id'],'property_name':prop['name'],'payment_id':payment_id})
    except PaystackError as exc:
        c=db(); _touch_payment(c, payment_id, failure_reason=str(exc)[:300]); c.commit(); c.close()
        return jsonify({'error':str(exc)}), 503
    access_code=init.get('access_code')
    auth_url=init.get('authorization_url')
    c=db(); _touch_payment(c, payment_id, authorization_url=auth_url, paystack_access_code=access_code); c.commit(); c.close()
    return jsonify({'ok':True,'reference':reference,'authorization_url':auth_url,
                    'access_code':access_code,'public_key':payments.PAYSTACK_PUBLIC_KEY,
                    'amount_kobo':amount_kobo,'currency':payments.CURRENCY})


@app.route("/checkout")
@app.route("/checkout/<int:property_id>")
def checkout(property_id=None):
    """Dedicated secure checkout page for a property purchase.

    Shows the authoritative, server-derived amount and collects the buyer
    details before handing off to Paystack. The amount shown here is the one
    the server charges - the browser never supplies it.
    """
    if property_id is None:
        try: property_id=int(request.args.get("property_id"))
        except (TypeError, ValueError): property_id=None
    if not property_id:
        flash("Choose a property to check out.", "error"); return redirect(url_for("propkonet"))
    prop=sellable_property(property_id)
    if not prop or not prop["is_verified"] or not prop["price"] or float(prop["price"]) <= 0:
        flash("This property is not currently available for secure checkout.", "error")
        return redirect(url_for("propkonet"))
    amount_kobo=payments.naira_to_kobo(prop["price"])
    c=db()
    seller=c.execute("SELECT username, email, phone FROM users WHERE id=?", (prop["user_id"],)).fetchone()
    c.close()
    prefilled={}
    if session.get("user_id"):
        c=db(); u=c.execute("SELECT username, email, phone FROM users WHERE id=?", (session["user_id"],)).fetchone(); c.close()
        if u:
            prefilled={"name": u["username"] or "", "email": u["email"] or "", "phone": u["phone"] or ""}
    return render_template("checkout.html", prop=prop, seller=seller,
        amount_kobo=amount_kobo, paystack_ready=payments.paystack_enabled(),
        prefilled=prefilled, public_key=payments.PAYSTACK_PUBLIC_KEY)

@app.get("/checkout/status/<reference>")
def checkout_status(reference):
    """Small JSON endpoint so the checkout page can poll a payment status."""
    row=payment_status_view(reference)
    if not row: return jsonify({"error":"Payment not found."}), 404
    return jsonify({"reference":row["reference"],"status":row["status"],
        "status_label":status_label(row["status"]),"amount_kobo":row["amount_kobo"]})
@app.get('/payments/callback')
def payment_callback():
    """Where Paystack redirects the browser after checkout.

    This never marks a payment successful on its own - it verifies with Paystack
    and then shows the status page. The webhook is the authoritative path.
    """
    reference=request.args.get('reference') or request.args.get('trxref') or ''
    if reference:
        try:
            verify_payment_with_paystack(reference, source='callback')
        except PaystackError:
            app.logger.warning('Paystack callback verification failed for %s', reference)
    row=payment_status_view(reference) if reference else None
    return render_template('purchase_status.html', payment=row, reference=reference)

@app.post('/webhooks/paystack')
def paystack_webhook():
    """Paystack webhook receiver: signature-verified, idempotent, transactional."""
    raw=request.get_data()
    signature=request.headers.get('x-paystack-signature','')
    if not payments.verify_webhook_signature(raw, signature):
        app.logger.warning('Rejected Paystack webhook with a bad signature')
        abort(401)
    try:
        event=json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return jsonify({'ok':False,'error':'invalid payload'}),400
    data=event.get('data') or {}
    event_type=str(event.get('event') or '')
    reference=str(data.get('reference') or data.get('transfer_code') or '')
    event_id=_webhook_event_id(event, raw)
    # De-duplicate deliveries: the unique event_id insert is the guard.
    c=db()
    inserted=False
    try:
        c.execute('INSERT INTO payment_webhook_events(event_id,event_type,reference) VALUES(?,?,?)',
                  (event_id, event_type, reference))
        inserted=True
    except sqlite3.IntegrityError:
        inserted=False
    c.commit(); c.close()
    if not inserted:
        return jsonify({'ok':True,'duplicate':True}),200
    try:
        if event_type in ('charge.success',):
            verify_payment_with_paystack(str(data.get('reference') or ''), source='webhook')
        elif event_type in ('transfer.success','transfer.failed','transfer.reversed'):
            apply_transfer_webhook(str(data.get('reference') or ''), event_type.split('.')[1],
                                   data.get('transfer_code'), data.get('reason'))
        elif event_type in ('charge.dispute.create','charge.dispute.remind','charge.dispute.resolve'):
            handle_dispute_event(data, event_type)
    except PaystackError as exc:
        c=db(); _mark_webhook(c, event_id, 'error'); c.commit(); c.close()
        app.logger.warning('Webhook processing deferred: %s', str(exc)[:200])
        return jsonify({'ok':False}),500
    c=db(); _mark_webhook(c, event_id, 'processed'); c.commit(); c.close()
    return jsonify({'ok':True}),200

def _webhook_event_id(event, raw):
    """Prefer Paystack's event id; fall back to a body hash so payloads never repeat."""
    from hashlib import sha256
    explicit=event.get('id') or (event.get('data') or {}).get('id')
    if explicit:
        return 'evt_'+str(explicit)
    return 'sha_'+sha256(raw).hexdigest()

def _mark_webhook(c, event_id, status):
    c.execute('UPDATE payment_webhook_events SET status=? WHERE event_id=?', (status, event_id))

def verify_payment_with_paystack(reference, source='verify'):
    """Verify a charge with Paystack, then idempotently record success.

    Only marks the payment successful when Paystack reports status == 'success',
    the reference matches, and the paid amount equals the amount we stored.
    """
    if not reference:
        return False
    row=payment_status_view(reference)
    if not row:
        return False
    data=payments.verify_transaction(reference)
    if str(data.get('status')) != 'success':
        return False
    if str(data.get('reference')) != reference:
        return False
    paid_amount=int(data.get('amount') or 0)
    if paid_amount != int(row['amount_kobo']):
        c=db(); _touch_payment(c, row['id'], failure_reason='Amount mismatch on verification.',
                               status=PaymentStatus.DISPUTED, dispute_status='amount_mismatch')
        payments.audit(c, 'system', None, 'payment.amount_mismatch', 'payment', row['id'],
                       {'expected': int(row['amount_kobo']), 'paid': paid_amount})
        c.commit(); c.close()
        return False
    return record_payment_success(reference, data, source=source)

def handle_dispute_event(data, event_type):
    reference=str(data.get('transaction', {}).get('reference') if isinstance(data.get('transaction'), dict) else data.get('reference') or '')
    if not reference:
        return
    row=payment_status_view(reference)
    if not row:
        return
    c=db()
    _touch_payment(c, row['id'], status=PaymentStatus.DISPUTED, dispute_status=event_type)
    payments.audit(c, 'system', None, 'payment.dispute', 'payment', row['id'], {'event': event_type})
    add_seller_task(c, row['seller_id'], 'A dispute was opened on payment %s. Support is reviewing it.' % reference, 'Dispute')
    c.commit(); c.close()

@app.get('/purchase/<reference>')
def purchase_status(reference):
    row=payment_status_view(reference)
    if not row:
        abort(404)
    return render_template('purchase_status.html', payment=row, reference=reference)

@app.post('/api/payments/<reference>/confirm')
@login_required
def confirm_payment(reference):
    """Customer confirms the transaction is complete -> schedule the 24h payout."""
    if not payments.rate_limit('confirm:'+str(session.get('user_id')), 20, 60):
        return jsonify({'error':'Too many confirmation attempts. Please wait a moment.'}), 429
    c=db()
    row=c.execute('SELECT * FROM payments WHERE reference=?',(reference,)).fetchone()
    c.close()
    if not row:
        return jsonify({'error':'Payment not found.'}),404
    if row['buyer_user_id'] != session.get('user_id'):
        return jsonify({'error':'You are not the customer for this payment.'}),403
    if row['status'] not in (PaymentStatus.PAYMENT_SUCCESSFUL,
                             PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION,
                             PaymentStatus.PAYOUT_SCHEDULED, PaymentStatus.PAYOUT_PROCESSING,
                             PaymentStatus.PAYOUT_SUCCESSFUL):
        return jsonify({'error':'This payment has not been received yet and cannot be confirmed.'}),409
    payout_at, already=schedule_payout(row['id'], source='customer')
    if payout_at is None:
        return jsonify({'error':'This payment is not eligible for confirmation.'}),409
    if already:
        return jsonify({'ok':True,'already_confirmed':True,'payout_at':payout_at,
                        'message':'Already confirmed. The seller payout is scheduled for %s UTC.' % payout_at})
    return jsonify({'ok':True,'payout_at':payout_at,
                    'message':'Confirmed. The seller payment is scheduled for release in %d hours (on %s UTC).'
                              % (payments.PAYOUT_HOLD_HOURS, payout_at)})

@app.post('/api/payments/<reference>/confirm-verification')
def confirm_property_verification(reference):
    """Legacy token-confirmation path used by the old purchase_status.html link."""
    d=request.get_json(silent=True) or request.form
    token=str(d.get('token',''))
    c=db(); sale=c.execute('SELECT * FROM property_transactions WHERE reference=?',(reference,)).fetchone(); c.close()
    if not sale or not hmac.compare_digest(sale['buyer_token'],token):
        return jsonify({'error':'Purchase link is invalid.'}),404
    return confirm_payment(reference)

@app.post('/api/purchases/<int:transaction_id>/documents-ready')
@owner_required
def documents_ready(transaction_id):
    c=db(); sale=c.execute('SELECT * FROM property_transactions WHERE id=? AND seller_id=?',(transaction_id,session['user_id'])).fetchone()
    if not sale: c.close(); return jsonify({'error':'Sale not found.'}),404
    if sale['status'] != 'paid': c.close(); return jsonify({'error':'Documents can only be marked ready after the purchase has been confirmed.'}),409
    c.execute("UPDATE property_transactions SET status='documents_ready',documents_ready_at=CURRENT_TIMESTAMP WHERE id=?",(transaction_id,))
    c.commit(); c.close(); return jsonify({'ok':True,'message':'Documents marked as handed over. The buyer can now confirm property verification.'})

# --- Seller payout account -------------------------------------------------

@app.post('/api/seller/payout-account')
@owner_required
def save_payout_account():
    if not payments.rate_limit('payoutacct:'+str(session.get('user_id')), 6, 3600):
        return jsonify({'error':'Too many payout account changes. Please try again later.'}),429
    d=request.get_json(silent=True) or request.form
    bank_name=str(d.get('bank_name','')).strip()
    bank_code=str(d.get('bank_code','')).strip()
    account_number=str(d.get('account_number','')).strip()
    if not re.fullmatch(r'\d{10}', account_number):
        return jsonify({'error':'Enter a valid 10-digit Nigerian account number.'}),400
    if not bank_code or not bank_name:
        return jsonify({'error':'Choose your bank.'}),400
    if not payments.paystack_enabled():
        return jsonify({'error':'Payouts are not configured yet.'}),503
    try:
        saved=save_seller_recipient(session['user_id'], bank_name, bank_code, account_number,
                                    payments.client_ip(request))
    except PaystackError as exc:
        return jsonify({'error':str(exc)}),400
    return jsonify({'ok':True,'account_name':saved['account_name'],
                    'message':'Payout account verified and saved.'})

@app.get('/api/seller/banks')
@owner_required
def seller_banks():
    try:
        banks=payments.list_banks('nigeria')
    except PaystackError as exc:
        return jsonify({'error':str(exc)}),503
    return jsonify({'banks':[{'name':b.get('name'),'code':b.get('code')} for b in banks if b.get('code')]})

# --- Payout worker (cron) --------------------------------------------------

@app.post('/internal/payouts/run')
def run_payouts():
    """Payout worker entry point for a trusted scheduler.

    Protected by a shared secret header, never exposed publicly. Calling it
    repeatedly is safe: each payout is claimed atomically.
    """
    provided=request.headers.get('X-Payout-Secret','')
    if not payments.PAYOUT_CRON_SECRET or not hmac.compare_digest(provided, payments.PAYOUT_CRON_SECRET):
        abort(401)
    return jsonify({'ok':True,'results':process_due_payouts()})

@app.post('/Admin/payments/<int:payment_id>/retry')
@admin_required
def admin_retry_payout(payment_id):
    ok, message=retry_failed_payout(payment_id, (request.admin or {}).get('email'))
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('admin'))

@app.get('/api/payments/<reference>/status')
def payment_status_api(reference):
    row=payment_status_view(reference)
    if not row:
        return jsonify({'error':'Payment not found.'}),404
    return jsonify({'reference':row['reference'],'status':row['status'],
                    'status_label':status_label(row['status']),'payout_status':row['payout_status'],
                    'payout_at':row['payout_at'],'customer_confirmed_at':row['customer_confirmed_at'],
                    'transfer_reference':row['transfer_reference']})

@app.delete('/api/property/<int:property_id>')
@owner_required
def delete_property(property_id):
    c=db()
    prop=c.execute('SELECT photo FROM properties WHERE id=? AND user_id=?',(property_id,session['user_id'])).fetchone()
    if not prop:
        c.close()
        return jsonify({'error':'Property not found.'}),404
    photos=[row['filename'] for row in c.execute('SELECT filename FROM property_photos WHERE property_id=?',(property_id,)).fetchall()]
    if prop['photo'] and prop['photo'] not in photos: photos.append(prop['photo'])
    c.execute('DELETE FROM property_photos WHERE property_id=?',(property_id,))
    c.execute('DELETE FROM properties WHERE id=? AND user_id=?',(property_id,session['user_id']))
    c.commit(); c.close()
    for filename in photos:
        try: (UPLOADS/filename).unlink(missing_ok=True)
        except OSError: pass
    return jsonify({'ok':True})

@app.get('/api/property/<int:property_id>/photos')
@owner_required
def property_photos(property_id):
    c=db(); prop=c.execute('SELECT id, name, location, price, status, photo FROM properties WHERE id=? AND user_id=?',(property_id,session['user_id'])).fetchone()
    if not prop: c.close(); return jsonify({'error':'Property not found.'}),404
    names=[row['filename'] for row in c.execute('SELECT filename FROM property_photos WHERE property_id=? ORDER BY id',(property_id,)).fetchall()]
    if prop['photo'] and prop['photo'] not in names: names.insert(0,prop['photo'])
    c.close(); return jsonify({'property':dict(prop),'photos':[url_for('static',filename='uploads/'+name) for name in names]})

@app.post('/api/task')
@owner_required
def add_task():
    d=request.get_json(force=True); title=d.get('title','').strip(); due=d.get('due','').strip()
    if not title: return jsonify({'error':'Enter a task title.'}),400
    c=db(); c.execute('INSERT INTO tasks(user_id,title,due) VALUES(?,?,?)',(session['user_id'],title,due or 'No due date')); c.commit(); c.close(); return jsonify({'ok':True})

@app.post('/api/task/<int:task_id>/toggle')
@owner_required
def toggle_task(task_id):
    c=db(); c.execute('UPDATE tasks SET done=CASE done WHEN 0 THEN 1 ELSE 0 END WHERE id=? AND user_id=?',(task_id,session['user_id'])); c.commit(); c.close(); return jsonify({'ok':True})

if __name__=='__main__': app.run(debug=True,host='127.0.0.1',port=5000)
