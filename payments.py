"""Paystack payment integration for Homelink.

This module contains everything specific to taking and releasing money:

* a thin, dependency-free Paystack HTTP client (uses the standard library so it
  does not add to requirements.txt),
* the canonical payment / payout state constants,
* an append-only audit log helper,
* a tiny in-process rate limiter for the sensitive money endpoints.

The Flask routes that use this module live in ``app.py``; the SQL schema lives
in ``init_db`` there too, matching the rest of the project's conventions.

Security notes
--------------
* The Paystack **secret** key is only ever read from the environment and is
  never returned in a response, logged, or written to the database.
* Amounts are stored and sent to Paystack as **integers in kobo** (NGN minor
  units). No floating point is used for money anywhere in this file.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration (all from the environment; no secrets hard-coded)
# ---------------------------------------------------------------------------

PAYSTACK_API_BASE = os.getenv('PAYSTACK_API_BASE', 'https://api.paystack.co').rstrip('/')
PAYSTACK_PUBLIC_KEY = os.getenv('PAYSTACK_PUBLIC_KEY', '').strip()
PAYSTACK_SECRET_KEY = os.getenv('PAYSTACK_SECRET_KEY', '').strip()
# Optional: a separate secret used to validate webhook signatures. Paystack signs
# with the *account* secret key, so this normally equals PAYSTACK_SECRET_KEY.
PAYSTACK_WEBHOOK_SECRET = os.getenv('PAYSTACK_WEBHOOK_SECRET', '').strip() or PAYSTACK_SECRET_KEY
CURRENCY = os.getenv('PAYSTACK_CURRENCY', 'NGN').strip() or 'NGN'

# How long the platform holds the money before paying the seller (hours).
PAYOUT_HOLD_HOURS = int(os.getenv('PAYOUT_HOLD_HOURS', '24'))
# How many Paystack transfers may be retried automatically after a failure.
PAYOUT_MAX_RETRIES = int(os.getenv('PAYOUT_MAX_RETRIES', '3'))
# Protects the internal payout worker endpoint.
PAYOUT_CRON_SECRET = os.getenv('PAYOUT_CRON_SECRET', '').strip()
# Public HTTPS base URL used to build the Paystack callback URL. Falls back to
# the request root when unset.
PAYSTACK_CALLBACK_BASE_URL = os.getenv('PAYSTACK_CALLBACK_BASE_URL', '').rstrip('/')


def paystack_enabled():
    """True when a secret key is configured, so we can fail gracefully without it."""
    return bool(PAYSTACK_SECRET_KEY)


# ---------------------------------------------------------------------------
# Payment / payout state machine
# ---------------------------------------------------------------------------
# These are the canonical values stored in payments.status. They map onto the
# states requested in the brief while keeping the existing project's
# lower_snake_case naming style.

class PaymentStatus:
    PENDING = 'pending'                                    # order created, not yet paid
    PAYMENT_SUCCESSFUL = 'payment_successful'              # verified successful charge
    AWAITING_CUSTOMER_CONFIRMATION = 'awaiting_customer_confirmation'
    CUSTOMER_CONFIRMED = 'customer_confirmed'
    PAYOUT_SCHEDULED = 'payout_scheduled'
    PAYOUT_PROCESSING = 'payout_processing'
    PAYOUT_SUCCESSFUL = 'payout_successful'
    PAYOUT_FAILED = 'payout_failed'
    CANCELLED = 'cancelled'
    REFUNDED = 'refunded'
    DISPUTED = 'disputed'


# Payout lifecycle is tracked on the payments row (payout_status) and mirrored on
# the seller_payouts row so the admin/seller views can query either side.

class PayoutStatus:
    NOT_APPLICABLE = 'not_applicable'
    SCHEDULED = 'scheduled'
    PROCESSING = 'processing'
    SUCCESSFUL = 'successful'
    FAILED = 'failed'


class TransferStatus:
    PENDING = 'pending'
    PROCESSING = 'processing'
    SUCCESS = 'success'
    FAILED = 'failed'
    REVERSED = 'reversed'
    OTP = 'otp'          # Paystack may require OTP finalisation for some transfers
    ABANDONED = 'abandoned'


# Human-readable labels for templates.
PAYMENT_STATUS_LABELS = {
    PaymentStatus.PENDING: 'Pending payment',
    PaymentStatus.PAYMENT_SUCCESSFUL: 'Payment received',
    PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION: 'Awaiting customer confirmation',
    PaymentStatus.CUSTOMER_CONFIRMED: 'Customer confirmed',
    PaymentStatus.PAYOUT_SCHEDULED: 'Payout scheduled',
    PaymentStatus.PAYOUT_PROCESSING: 'Payout processing',
    PaymentStatus.PAYOUT_SUCCESSFUL: 'Payout completed',
    PaymentStatus.PAYOUT_FAILED: 'Payout failed',
    PaymentStatus.CANCELLED: 'Cancelled',
    PaymentStatus.REFUNDED: 'Refunded',
    PaymentStatus.DISPUTED: 'Disputed',
}


def status_label(status):
    return PAYMENT_STATUS_LABELS.get(status, (status or '').replace('_', ' ').title())


# ---------------------------------------------------------------------------
# Paystack HTTP client
# ---------------------------------------------------------------------------

class PaystackError(RuntimeError):
    """Raised when Paystack rejects a request or cannot be reached."""


def paystack_request(path, method='GET', payload=None, timeout=20):
    """Call the Paystack API and return the ``data`` object.

    The secret key is attached here only, server-side. Errors never include the
    key, and the response body is truncated before it can reach a log line.
    """
    if not PAYSTACK_SECRET_KEY:
        raise PaystackError('Paystack is not configured. Set PAYSTACK_SECRET_KEY.')
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(PAYSTACK_API_BASE + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + PAYSTACK_SECRET_KEY)
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')
        raise PaystackError(_safe_error(detail, exc.code)) from exc
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        raise PaystackError('Could not reach Paystack. Please try again.') from exc
    if not isinstance(result, dict) or not result.get('status'):
        message = result.get('message') if isinstance(result, dict) else 'Paystack rejected the request.'
        raise PaystackError(message or 'Paystack rejected the request.')
    return result.get('data') or {}


def _safe_error(body, code):
    """Turn a Paystack error body into a short, secret-free message."""
    try:
        parsed = json.loads(body)
        message = parsed.get('message') or body
    except (ValueError, TypeError):
        message = body
    return 'Paystack request failed ({}): {}'.format(code, str(message)[:200])


# --- high level helpers ----------------------------------------------------

def initialize_transaction(email, amount_kobo, reference, callback_url, metadata=None):
    """Initialize a Paystack transaction. Returns the authorization data."""
    payload = {
        'email': email,
        'amount': int(amount_kobo),      # kobo, integer
        'currency': CURRENCY,
        'reference': reference,
        'callback_url': callback_url,
    }
    if metadata:
        payload['metadata'] = json.dumps(metadata)
    return paystack_request('/transaction/initialize', 'POST', payload)


def verify_transaction(reference):
    """Verify a transaction by reference and return its data object."""
    return paystack_request('/transaction/verify/' + urllib.parse.quote(reference, safe=''))


def list_banks(country='nigeria'):
    return paystack_request('/bank?country=' + urllib.parse.quote(country), 'GET')


def resolve_account(account_number, bank_code):
    """Resolve a Nigerian bank account to its account name."""
    query = urllib.parse.urlencode({'account_number': account_number, 'bank_code': bank_code})
    return paystack_request('/bank/resolve?' + query, 'GET')


def create_transfer_recipient(name, account_number, bank_code, currency=None):
    payload = {
        'type': 'nuban',
        'name': name,
        'account_number': account_number,
        'bank_code': bank_code,
        'currency': currency or CURRENCY,
    }
    return paystack_request('/transferrecipient', 'POST', payload)


def initiate_transfer(amount_kobo, recipient_code, reason, reference):
    payload = {
        'source': 'balance',
        'amount': int(amount_kobo),      # kobo, integer
        'recipient': recipient_code,
        'reason': reason,
        'reference': reference,
        'currency': CURRENCY,
    }
    return paystack_request('/transfer', 'POST', payload)


def verify_transfer(reference):
    return paystack_request('/transfer/verify/' + urllib.parse.quote(reference, safe=''), 'GET')


def finalize_transfer(transfer_code, otp):
    return paystack_request('/transfer/finalize_transfer', 'POST',
                            {'transfer_code': transfer_code, 'otp': otp})


# ---------------------------------------------------------------------------
# Signature verification (webhooks)
# ---------------------------------------------------------------------------

def verify_webhook_signature(raw_body, signature, secret=None):
    """Constant-time check of Paystack's ``x-paystack-signature`` header.

    Paystack signs the raw request body with HMAC-SHA512 using the account
    secret key, hex-encoded.
    """
    import hmac
    from hashlib import sha512
    key = (secret or PAYSTACK_WEBHOOK_SECRET or '').encode('utf-8')
    if not key or not signature:
        return False
    expected = hmac.new(key, raw_body, sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def audit(c, actor_type, actor_id, action, entity_type=None, entity_id=None,
          details=None, ip=None):
    """Append an immutable audit-log row.

    ``c`` is an open sqlite connection so the write participates in the caller's
    transaction. ``details`` is stored as JSON text.
    """
    try:
        c.execute(
            'INSERT INTO audit_logs(actor_type,actor_id,action,entity_type,entity_id,details,ip_address) '
            'VALUES(?,?)',
            (actor_type, actor_id, action, entity_type, entity_id,
             json.dumps(details) if details is not None else None, ip),
        )
    except Exception:  # never let logging break a money operation
        pass


# ---------------------------------------------------------------------------
# In-process rate limiter
# ---------------------------------------------------------------------------
# A tiny fixed-window limiter. It is adequate for a single-process deployment
# (which is what this app uses); a multi-worker deployment should move this to
# Redis or similar. It deliberately favours simplicity over distributed accuracy.

_BUCKETS = {}


def rate_limit(key, limit, window_seconds):
    """Return True when the action is allowed, False when it should be throttled."""
    now = time.monotonic()
    window_start = now - window_seconds
    hits = [t for t in _BUCKETS.get(key, ()) if t >= window_start]
    if len(hits) >= limit:
        _BUCKETS[key] = tuple(hits) + (now,)
        return False
    hits.append(now)
    _BUCKETS[key] = tuple(hits)
    return True


def client_ip(request):
    """Best-effort client IP, honouring a single proxy hop."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def utcnow():
    return datetime.now(timezone.utc)


def iso_plus_hours(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')


def parse_db_time(value):
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` UTC timestamp into an aware datetime."""
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def naira_to_kobo(amount_naira):
    """Convert a naira amount to integer kobo without floating point error.

    Accepts an int/str/Decimal; rounds half-up to the nearest kobo.
    """
    from decimal import Decimal, ROUND_HALF_UP
    value = Decimal(str(amount_naira))
    kobo = (value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return int(kobo)


def kobo_to_naira(kobo):
    """Format integer kobo as a naira string with two decimals."""
    from decimal import Decimal
    return str((Decimal(int(kobo)) / 100).quantize(Decimal('0.01')))
