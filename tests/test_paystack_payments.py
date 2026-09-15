"""End-to-end tests for the Paystack property-payment integration.

Paystack's HTTP layer is monkey-patched so the suite runs offline and
deterministically. Every test exercises the real routes, database writes and
state machine - only the outbound HTTP calls are faked.
"""

import json
import os
import sqlite3
import unittest
from hashlib import sha512
import hmac as hmaclib

import app as appmod
import payments


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

BUYER_EMAIL = 'pay_buyer@homelink.ng'
SELLER_EMAIL = 'pay_seller@homelink.ng'
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@estimate.ng')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'Mmz1810')


class FakePaystack:
    """Records calls and returns canned Paystack responses."""

    def __init__(self):
        self.calls = []
        self.verify_response = {}
        self.resolve_response = {'account_name': 'JOHN DOE'}
        self.recipient_response = {'recipient_code': 'RCP_test123'}
        self.transfer_response = {'transfer_code': 'TRF_test123', 'status': 'pending'}
        self.raise_on_transfer = None

    def install(self):
        def fake_request(path, method='GET', payload=None, timeout=20):
            self.calls.append({'path': path, 'method': method, 'payload': payload})
            if path.startswith('/transaction/initialize'):
                return {'authorization_url': 'https://checkout.paystack.com/abc',
                        'access_code': 'acc_test', 'reference': payload.get('reference')}
            if path.startswith('/transaction/verify/'):
                return self.verify_response
            if path.startswith('/bank/resolve'):
                return self.resolve_response
            if path.startswith('/bank'):
                return [{'name': 'Test Bank', 'code': '058'}]
            if path.startswith('/transferrecipient'):
                return self.recipient_response
            if path.startswith('/transfer/verify/'):
                return self.transfer_response
            if path == '/transfer':
                if self.raise_on_transfer:
                    raise payments.PaystackError(self.raise_on_transfer)
                return self.transfer_response
            return {}

        self._orig = payments.paystack_request
        payments.paystack_request = fake_request

    def restore(self):
        payments.paystack_request = self._orig


def make_signature(raw: bytes, secret=None):
    secret = (secret or payments.PAYSTACK_WEBHOOK_SECRET or 'sk_test').encode()
    return hmaclib.new(secret, raw, sha512).hexdigest()


class PaymentTestBase(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        self.ctx = appmod.app.app_context()
        self.ctx.push()
        self.fake = FakePaystack()
        self.fake.install()
        # Ensure a secret key exists so paystack_enabled() is True during tests.
        self._orig_secret = payments.PAYSTACK_SECRET_KEY
        self._orig_webhook_secret = payments.PAYSTACK_WEBHOOK_SECRET
        payments.PAYSTACK_SECRET_KEY = 'sk_test_unit'
        payments.PAYSTACK_WEBHOOK_SECRET = 'sk_test_unit'
        self.cleanup()

    def tearDown(self):
        self.cleanup()
        self.fake.restore()
        payments.PAYSTACK_SECRET_KEY = self._orig_secret
        payments.PAYSTACK_WEBHOOK_SECRET = self._orig_webhook_secret
        self.ctx.pop()

    def cleanup(self):
        c = appmod.db()
        ids = [r['id'] for r in c.execute('SELECT id FROM users WHERE email IN (?,?)',
                                          (BUYER_EMAIL, SELLER_EMAIL))]
        for uid in ids:
            c.execute('DELETE FROM payments WHERE seller_id=? OR buyer_user_id=?', (uid, uid))
            c.execute('DELETE FROM transfer_recipients WHERE user_id=?', (uid,))
            c.execute('DELETE FROM properties WHERE user_id=?', (uid,))
        c.execute('DELETE FROM payments WHERE reference LIKE "HOUSE-test%"')
        c.execute('DELETE FROM payment_webhook_events WHERE event_id LIKE "evt_%" OR reference LIKE "HOUSE-test%" OR reference LIKE "TRF-HOUSE-test%"')
        c.execute('DELETE FROM users WHERE email IN (?,?)', (BUYER_EMAIL, SELLER_EMAIL))
        c.commit()
        c.close()

    # -- fixtures -----------------------------------------------------------

    def make_seller_and_property(self, price=30_000_000, verified=1):
        c = appmod.db()
        from werkzeug.security import generate_password_hash
        c.execute('INSERT INTO users(email,username,password,role,is_verified) VALUES(?,?,?,?,?)',
                  (SELLER_EMAIL, 'PaySeller', generate_password_hash('pw'), 'owner', verified))
        seller_id = c.execute('SELECT id FROM users WHERE email=?', (SELLER_EMAIL,)).fetchone()['id']
        c.execute('INSERT INTO properties(user_id,name,location,price,status,thumb,'
                  'published_to_propkonet,listing_type,ai_price) VALUES(?,?,?,?,?,?,?,?,?)',
                  (seller_id, 'Paystack Test Villa', 'Lekki, Lagos', price, 'Listed', 'house', 1, 'sale', price))
        prop_id = c.execute('SELECT id FROM properties WHERE user_id=? ORDER BY id DESC LIMIT 1',
                            (seller_id,)).fetchone()['id']
        c.commit()
        c.close()
        return seller_id, prop_id

    def add_recipient_for_seller(self, seller_id, code='RCP_test123'):
        c = appmod.db()
        c.execute('UPDATE users SET paystack_recipient_code=?, payout_bank_name=?, payout_bank_code=?, '
                  'payout_account_number=?, payout_account_name=? WHERE id=?',
                  (code, 'Test Bank', '058', '0123456789', 'John Doe', seller_id))
        c.commit()
        c.close()

    def start_checkout(self, prop_id, email=BUYER_EMAIL):
        return self.client.post('/api/propkonet/buy', json={
            'property_id': prop_id, 'name': 'Chidi Buyer', 'email': email,
            'phone': '+234 802 000 0000', 'message': 'Buying'})

    def create_payment_row(self, reference, seller_id, prop_id, amount_kobo):
        c = appmod.db()
        c.execute('INSERT INTO payments(reference,property_id,seller_id,buyer_name,buyer_email,'
                  'buyer_phone,amount_kobo,status) VALUES(?,?,?,?,?,?,?,?)',
                  (reference, prop_id, seller_id, 'Chidi Buyer', BUYER_EMAIL,
                   '+234 802 000 0000', amount_kobo, payments.PaymentStatus.PENDING))
        c.commit()
        c.close()

    def get_payment(self, reference):
        c = appmod.db()
        r = c.execute('SELECT * FROM payments WHERE reference=?', (reference,)).fetchone()
        c.close()
        return r


# ---------------------------------------------------------------------------
# 1. Payment initialization
# ---------------------------------------------------------------------------

class TestPaymentInitialization(PaymentTestBase):
    def test_successful_checkout_initialization(self):
        _, prop_id = self.make_seller_and_property()
        res = self.start_checkout(prop_id)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('authorization_url', data)
        # Server-side amount must be the property price in kobo.
        self.assertEqual(data['amount_kobo'], 30_000_000 * 100)

    def test_incorrect_amount_is_ignored_from_client(self):
        """A client-supplied amount must never influence the charge."""
        _, prop_id = self.make_seller_and_property(price=30_000_000)
        res = self.client.post('/api/propkonet/buy', json={
            'property_id': prop_id, 'name': 'Chidi', 'email': BUYER_EMAIL,
            'phone': '+2348020000', 'amount': 1, 'amount_kobo': 1, 'price': 1})
        data = res.get_json()
        self.assertEqual(data['amount_kobo'], 30_000_000 * 100)
        # The initialize call used the server amount.
        init_calls = [c for c in self.fake.calls if c['path'].startswith('/transaction/initialize')]
        self.assertEqual(init_calls[0]['payload']['amount'], 30_000_000 * 100)

        def test_buy_without_paystack_configured(self):
            payments.PAYSTACK_SECRET_KEY = ''
            _, prop_id = self.make_seller_and_property()
            res = self.start_checkout(prop_id)
            self.assertEqual(res.status_code, 503)

# ---------------------------------------------------------------------------
# 1b. Dedicated checkout page
# ---------------------------------------------------------------------------

class TestCheckoutPage(PaymentTestBase):
    def test_checkout_page_renders_for_a_listable_property(self):
        _, prop_id = self.make_seller_and_property(price=45_000_000)
        res = self.client.get(f'/checkout/{prop_id}')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        # The server-derived amount must be shown, formatted with the Naira sign.
        self.assertIn('₦45,000,000', html)
        self.assertIn('checkoutForm', html)

    def test_checkout_without_property_redirects(self):
        res = self.client.get('/checkout')
        self.assertEqual(res.status_code, 302)

    def test_checkout_for_unknown_property_redirects(self):
        res = self.client.get('/checkout/99999')
        self.assertEqual(res.status_code, 302)

    def test_checkout_status_endpoint(self):
        res = self.client.get('/checkout/status/HOUSE-does-not-exist')
        self.assertEqual(res.status_code, 404)

# ---------------------------------------------------------------------------
# 2. Webhook handling
# ---------------------------------------------------------------------------

class TestWebhook(PaymentTestBase):
    def _webhook(self, event_type, data, secret=None, event_id=None):
        body = {'event': event_type, 'data': data}
        if event_id:
            body['id'] = event_id
        raw = json.dumps(body).encode()
        sig = make_signature(raw, secret)
        return self.client.post('/webhooks/paystack', data=raw,
                                headers={'x-paystack-signature': sig, 'Content-Type': 'application/json'})

    def test_successful_payment_webhook(self):
        seller_id, prop_id = self.make_seller_and_property()
        ref = 'HOUSE-testok1'
        self.create_payment_row(ref, seller_id, prop_id, 30_000_000 * 100)
        self.fake.verify_response = {'status': 'success', 'reference': ref, 'amount': 30_000_000 * 100}
        res = self._webhook('charge.success', {'reference': ref, 'id': 111})
        self.assertEqual(res.status_code, 200)
        row = self.get_payment(ref)
        self.assertEqual(row['status'], payments.PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION)
        self.assertIsNotNone(row['paid_at'])
        self.assertEqual(row['paystack_reference'], ref)

    def test_duplicate_payment_webhook_is_idempotent(self):
        seller_id, prop_id = self.make_seller_and_property()
        ref = 'HOUSE-testdup1'
        self.create_payment_row(ref, seller_id, prop_id, 30_000_000 * 100)
        self.fake.verify_response = {'status': 'success', 'reference': ref, 'amount': 30_000_000 * 100}
        self._webhook('charge.success', {'reference': ref, 'id': 222})
        first = self.get_payment(ref)
        # Re-deliver the exact same event id.
        res = self._webhook('charge.success', {'reference': ref, 'id': 222})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get('duplicate'))
        second = self.get_payment(ref)
        self.assertEqual(first['paid_at'], second['paid_at'])
        self.assertEqual(second['status'], payments.PaymentStatus.AWAITING_CUSTOMER_CONFIRMATION)

    def test_invalid_webhook_signature_is_rejected(self):
        raw = json.dumps({'event': 'charge.success', 'data': {'reference': 'x'}}).encode()
        res = self.client.post('/webhooks/paystack', data=raw,
                               headers={'x-paystack-signature': 'deadbeef'})
        self.assertEqual(res.status_code, 401)

    def test_webhook_amount_mismatch_marked_disputed(self):
        seller_id, prop_id = self.make_seller_and_property()
        ref = 'HOUSE-testmm1'
        self.create_payment_row(ref, seller_id, prop_id, 30_000_000 * 100)
        self.fake.verify_response = {'status': 'success', 'reference': ref, 'amount': 5}
        self._webhook('charge.success', {'reference': ref, 'id': 333})
        row = self.get_payment(ref)
        self.assertEqual(row['status'], payments.PaymentStatus.DISPUTED)

    def test_failed_charge_does_not_mark_successful(self):
        seller_id, prop_id = self.make_seller_and_property()
        ref = 'HOUSE-testfail1'
        self.create_payment_row(ref, seller_id, prop_id, 30_000_000 * 100)
        self.fake.verify_response = {'status': 'failed', 'reference': ref, 'amount': 30_000_000 * 100}
        self._webhook('charge.success', {'reference': ref, 'id': 334})
        row = self.get_payment(ref)
        self.assertEqual(row['status'], payments.PaymentStatus.PENDING)


# ---------------------------------------------------------------------------
# 3. Customer confirmation
# ---------------------------------------------------------------------------

class TestCustomerConfirmation(PaymentTestBase):
    def _login_buyer(self):
        c = appmod.db()
        from werkzeug.security import generate_password_hash
        c.execute('INSERT OR IGNORE INTO users(email,username,password,role) VALUES(?,?,?,?)',
                  (BUYER_EMAIL, 'Buyer', generate_password_hash('pw'), 'user'))
        buyer_id = c.execute('SELECT id FROM users WHERE email=?', (BUYER_EMAIL,)).fetchone()['id']
        c.commit()
        c.close()
        self.client.post('/login', data={'email': BUYER_EMAIL, 'password': 'pw', 'role': 'user'},
                         follow_redirects=True)
        return buyer_id

    def _paid_payment(self, buyer_id):
        seller_id, prop_id = self.make_seller_and_property()
        ref = 'HOUSE-testconf1'
        c = appmod.db()
        c.execute('INSERT INTO payments(reference,property_id,buyer_user_id,seller_id,buyer_name,'
                  'buyer_email,buyer_phone,amount_kobo,status) VALUES(?,?,?,?,?,?,?,?,?)',
                  (ref, prop_id, buyer_id, seller_id, 'Chidi', BUYER_EMAIL, '+2348020000',
                   30_000_000 * 100, payments.PaymentStatus.PAYMENT_SUCCESSFUL))
        c.commit(); c.close()
        return ref

    def test_customer_confirmation_schedules_24h_payout(self):
        buyer_id = self._login_buyer()
        ref = self._paid_payment(buyer_id)
        res = self.client.post('/api/payments/%s/confirm' % ref)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['ok'])
        row = self.get_payment(ref)
        self.assertEqual(row['status'], payments.PaymentStatus.PAYOUT_SCHEDULED)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.SCHEDULED)
        self.assertIsNotNone(row['customer_confirmed_at'])
        self.assertIsNotNone(row['payout_at'])
        # payout_at is ~24h after confirmation, computed server-side.
        confirmed = payments.parse_db_time(row['customer_confirmed_at'])
        payout_at = payments.parse_db_time(row['payout_at'])
        delta_hours = (payout_at - confirmed).total_seconds() / 3600
        self.assertAlmostEqual(delta_hours, 24, delta=0.02)

    def test_duplicate_confirmation_does_not_move_schedule(self):
        buyer_id = self._login_buyer()
        ref = self._paid_payment(buyer_id)
        self.client.post('/api/payments/%s/confirm' % ref)
        first = self.get_payment(ref)['payout_at']
        res = self.client.post('/api/payments/%s/confirm' % ref)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get('already_confirmed'))
        self.assertEqual(self.get_payment(ref)['payout_at'], first)

    def test_unauthorized_confirmation_is_rejected(self):
        buyer_id = self._login_buyer()
        ref = self._paid_payment(buyer_id)
        # A different logged-in user must not be able to confirm.
        other = appmod.app.test_client()
        c = appmod.db()
        from werkzeug.security import generate_password_hash
        c.execute('INSERT OR IGNORE INTO users(email,username,password,role) VALUES(?,?,?,?)',
                  ('intruder@homelink.ng', 'X', generate_password_hash('pw'), 'user'))
        c.commit(); c.close()
        other.post('/login', data={'email': 'intruder@homelink.ng', 'password': 'pw', 'role': 'user'},
                   follow_redirects=True)
        res = other.post('/api/payments/%s/confirm' % ref)
        self.assertEqual(res.status_code, 403)
        c = appmod.db(); c.execute('DELETE FROM users WHERE email=?', ('intruder@homelink.ng',)); c.commit(); c.close()

    def test_confirmation_requires_authentication(self):
        seller_id, prop_id = self.make_seller_and_property()
        ref = 'HOUSE-testanon1'
        self.create_payment_row(ref, seller_id, prop_id, 30_000_000 * 100)
        res = self.client.post('/api/payments/%s/confirm' % ref)
        # login_required redirects anonymous users to the login page.
        self.assertIn(res.status_code, (302, 401))

    def test_cannot_confirm_unpaid_payment(self):
        buyer_id = self._login_buyer()
        seller_id, prop_id = self.make_seller_and_property()
        ref = 'HOUSE-testpend1'
        c = appmod.db()
        c.execute('INSERT INTO payments(reference,property_id,buyer_user_id,seller_id,amount_kobo,status) '
                  'VALUES(?,?,?,?,?,?)', (ref, prop_id, buyer_id, seller_id, 100, payments.PaymentStatus.PENDING))
        c.commit(); c.close()
        res = self.client.post('/api/payments/%s/confirm' % ref)
        self.assertEqual(res.status_code, 409)


# ---------------------------------------------------------------------------
# 4. Payout execution
# ---------------------------------------------------------------------------

class TestPayouts(PaymentTestBase):
    def _scheduled_payment(self, due_hours=-1, with_recipient=True):
        seller_id, prop_id = self.make_seller_and_property()
        if with_recipient:
            self.add_recipient_for_seller(seller_id)
        ref = 'HOUSE-testpay1'
        payout_at = (payments.utcnow() + __import__('datetime').timedelta(hours=due_hours)
                     ).strftime('%Y-%m-%d %H:%M:%S')
        c = appmod.db()
        c.execute('''INSERT INTO payments(reference,property_id,seller_id,buyer_name,buyer_email,
                amount_kobo,status,customer_confirmed_at,payout_status,payout_at,payout_amount_kobo,transfer_reference)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (ref, prop_id, seller_id, 'Chidi', BUYER_EMAIL, 30_000_000 * 100,
                   payments.PaymentStatus.PAYOUT_SCHEDULED, payments.utcnow_iso(),
                   payments.PayoutStatus.SCHEDULED, payout_at, 30_000_000 * 100, 'TRF-' + ref))
        c.commit(); c.close()
        return ref, seller_id

    def test_payout_execution(self):
        ref, _ = self._scheduled_payment()
        results = appmod.process_due_payouts()
        self.assertTrue(any(r.get('action') == 'processing' for r in results))
        row = self.get_payment(ref)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.PROCESSING)
        self.assertEqual(row['transfer_code'], 'TRF_test123')
        # Transfer sent with the correct amount in kobo and recipient.
        transfers = [c for c in self.fake.calls if c['path'] == '/transfer']
        self.assertEqual(transfers[0]['payload']['amount'], 30_000_000 * 100)
        self.assertEqual(transfers[0]['payload']['recipient'], 'RCP_test123')

    def test_duplicate_payout_prevention(self):
        ref, _ = self._scheduled_payment()
        appmod.process_due_payouts()
        # A second run must not send another transfer.
        appmod.process_due_payouts()
        transfers = [c for c in self.fake.calls if c['path'] == '/transfer']
        self.assertEqual(len(transfers), 1)

    def test_payout_not_run_before_due(self):
        ref, _ = self._scheduled_payment(due_hours=5)
        results = appmod.process_due_payouts()
        self.assertEqual(results, [])
        row = self.get_payment(ref)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.SCHEDULED)

    def test_missing_recipient_fails_payout(self):
        ref, _ = self._scheduled_payment(with_recipient=False)
        appmod.process_due_payouts()
        row = self.get_payment(ref)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.FAILED)
        self.assertIn('recipient', (row['failure_reason'] or '').lower())

    def test_failed_transfer_and_safe_retry(self):
        ref, _ = self._scheduled_payment()
        self.fake.raise_on_transfer = 'Paystack request failed (400): Insufficient balance'
        appmod.process_due_payouts()
        row = self.get_payment(ref)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.FAILED)
        self.assertIn('Insufficient', row['failure_reason'])

        # Safe retry: re-arm and process again with a working transfer.
        self.fake.raise_on_transfer = None
        ok, message = appmod.retry_failed_payout(row['id'], 'admin@test')
        self.assertTrue(ok, message)
        appmod.process_due_payouts()
        row = self.get_payment(ref)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.PROCESSING)
        transfers = [c for c in self.fake.calls if c['path'] == '/transfer']
        self.assertEqual(len(transfers), 2)  # original failure + one retry, never a double-pay on success

    def test_transfer_success_webhook(self):
        ref, _ = self._scheduled_payment()
        appmod.process_due_payouts()
        raw = json.dumps({'event': 'transfer.success',
                          'data': {'reference': 'TRF-' + ref, 'transfer_code': 'TRF_test123', 'id': 900}}).encode()
        sig = make_signature(raw)
        self.client.post('/webhooks/paystack', data=raw, headers={'x-paystack-signature': sig})
        row = self.get_payment(ref)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.SUCCESSFUL)
        self.assertEqual(row['status'], payments.PaymentStatus.PAYOUT_SUCCESSFUL)

    def test_transfer_failed_webhook(self):
        ref, _ = self._scheduled_payment()
        appmod.process_due_payouts()
        raw = json.dumps({'event': 'transfer.failed',
                          'data': {'reference': 'TRF-' + ref, 'transfer_code': 'TRF_test123',
                                   'reason': 'Account closed', 'id': 901}}).encode()
        sig = make_signature(raw)
        self.client.post('/webhooks/paystack', data=raw, headers={'x-paystack-signature': sig})
        row = self.get_payment(ref)
        self.assertEqual(row['payout_status'], payments.PayoutStatus.FAILED)
        self.assertEqual(row['failure_reason'], 'Account closed')


# ---------------------------------------------------------------------------
# 5. Seller payout account
# ---------------------------------------------------------------------------

class TestSellerPayoutAccount(PaymentTestBase):
    def _login_seller(self, verified=1):
        seller_id, _ = self.make_seller_and_property(verified=verified)
        c = appmod.db()
        from werkzeug.security import generate_password_hash
        c.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash('pw'), seller_id))
        c.commit(); c.close()
        self.client.post('/login', data={'email': SELLER_EMAIL, 'password': 'pw', 'role': 'owner'},
                         follow_redirects=True)
        return seller_id

    def test_valid_account_creates_recipient(self):
        seller_id = self._login_seller()
        res = self.client.post('/api/seller/payout-account', json={
            'bank_name': 'Test Bank', 'bank_code': '058', 'account_number': '0123456789'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['account_name'], 'JOHN DOE')
        c = appmod.db()
        row = c.execute('SELECT paystack_recipient_code, payout_account_name FROM users WHERE id=?',
                        (seller_id,)).fetchone()
        c.close()
        self.assertEqual(row['paystack_recipient_code'], 'RCP_test123')
        self.assertEqual(row['payout_account_name'], 'JOHN DOE')

    def test_invalid_account_number_rejected(self):
        self._login_seller()
        res = self.client.post('/api/seller/payout-account', json={
            'bank_name': 'Test Bank', 'bank_code': '058', 'account_number': '123'})
        self.assertEqual(res.status_code, 400)


# ---------------------------------------------------------------------------
# 6. Admin access
# ---------------------------------------------------------------------------

class TestAdminAccess(PaymentTestBase):
    def test_unauthorized_admin_payment_retry(self):
        res = self.client.post('/Admin/payments/1/retry')
        self.assertIn(res.status_code, (401, 403))

    def test_admin_sees_payment_management(self):
        admin = appmod.app.test_client()
        login = admin.post('/api/admin/login', json={'email': ADMIN_EMAIL, 'password': ADMIN_PASSWORD})
        if login.status_code != 200:
            self.skipTest('admin credentials not available in this environment')
        res = admin.get('/Admin')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Payment', res.data)

    def test_worker_requires_secret(self):
        res = self.client.post('/internal/payouts/run')
        self.assertEqual(res.status_code, 401)


if __name__ == '__main__':
    unittest.main()
