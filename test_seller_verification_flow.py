import unittest
import os
from app import app, db
import json

class TestSellerVerificationFlow(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.app_ctx = app.app_context()
        self.app_ctx.push()

    def tearDown(self):
        self.app_ctx.pop()

    def test_full_seller_verification_and_propkonet_flow(self):
        c = db()
        # Clean up any test users and the properties left behind by earlier runs
        # (deleting the user alone would leave stale properties that break the
        # name lookup below with a 404).
        stale = [row['id'] for row in
                 c.execute("SELECT id FROM users WHERE email='test_seller@estimate.ng'")]
        for user_id in stale:
            c.execute('DELETE FROM properties WHERE user_id=?', (user_id,))
        c.execute("DELETE FROM users WHERE email='test_seller@estimate.ng'")
        c.execute("DELETE FROM verification_requests WHERE email='test_seller@estimate.ng'")
        c.commit()
        c.close()

        # 1. Sign up a new owner (signup now requires a username too)
        res = self.client.post('/signup', data={
            'email': 'test_seller@estimate.ng',
            'username': 'Samuel Okon Realty',
            'password': 'password123',
            'role': 'owner'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # 2. Log in as the owner
        res = self.client.post('/login', data={
            'email': 'test_seller@estimate.ng',
            'password': 'password123',
            'role': 'owner'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # 3. Check property manager initially shows "Get verified to sell property"
        res = self.client.get('/property-manager')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Get verified to sell property', res.data)
        self.assertNotIn(b'Verified Seller', res.data)

        # 4. Attempting to toggle PropkoNet while unverified should be rejected with 403
        # First add a test property
        res = self.client.post('/api/property', data={
            'name': 'Test Unverified Villa',
            'location': 'Lekki Phase 1, Lagos',
            'price': '85000000',
            'status': 'Listed'
        })
        self.assertEqual(res.status_code, 200)

        c = db()
        prop = c.execute(
            "SELECT id FROM properties WHERE name='Test Unverified Villa' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        c.close()
        prop_id = prop['id']

        # Try to toggle to propkonet while unverified
        res = self.client.post(f'/api/property/{prop_id}/toggle-propkonet')
        self.assertEqual(res.status_code, 403)

        # 5. Owner requests verification
        res = self.client.post('/api/request-verification', json={
            'username': 'Samuel Okon Realty',
            'phone': '+234 803 555 1234'
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))

        # Check DB has pending request
        c = db()
        v_req = c.execute("SELECT * FROM verification_requests WHERE email='test_seller@estimate.ng'").fetchone()
        self.assertIsNotNone(v_req)
        self.assertEqual(v_req['status'], 'pending')
        self.assertEqual(v_req['username'], 'Samuel Okon Realty')
        self.assertEqual(v_req['phone'], '+234 803 555 1234')
        req_id = v_req['id']
        c.close()

        # Check manager page now shows pending badge
        res = self.client.get('/property-manager')
        self.assertIn(b'Verification Pending', res.data)

        # 6. Admin checks admin endpoint
        # First log in as admin: POST credentials to the public login endpoint,
        # which returns the signed JWT in an HttpOnly cookie on the client.
        admin_client = app.test_client()
        login = admin_client.post('/api/admin/login',
                                  json={'email': os.getenv('ADMIN_EMAIL', 'admin@estimate.ng'),
                                        'password': os.getenv('ADMIN_PASSWORD', 'Mmz1810')})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.get_json()['ok'])
        res = admin_client.get('/Admin')
        self.assertEqual(res.status_code, 200)
        # Check that the admin sees notification showing user's email, username and phone
        self.assertIn(b'test_seller@estimate.ng', res.data)
        self.assertIn(b'Samuel Okon Realty', res.data)
        self.assertIn(b'+234 803 555 1234', res.data)
        self.assertIn(b'Seller Verification Alert', res.data)
        self.assertIn(b'Accept Verification', res.data)

        # 7. Admin accepts verification
        res = admin_client.post(f'/Admin/verification/{req_id}/accept', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Verification accepted', res.data)

        # Verify DB is updated
        c = db()
        user = c.execute("SELECT is_verified FROM users WHERE email='test_seller@estimate.ng'").fetchone()
        self.assertEqual(user['is_verified'], 1)
        v_req = c.execute("SELECT status FROM verification_requests WHERE id=?", (req_id,)).fetchone()
        self.assertEqual(v_req['status'], 'approved')
        c.close()

        # 8. Check owner Property Manager now reflects verified status
        res = self.client.get('/property-manager')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Verified Seller', res.data)
        self.assertIn(b'Add to PropkoNet', res.data)

        # 9. Verified owner publishes property to PropkoNet
        res = self.client.post(f'/api/property/{prop_id}/toggle-propkonet')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('published'))

        # Also add a new property directly published to PropkoNet
        res = self.client.post('/api/property', data={
            'name': 'Luxury Waterfront Mansion',
            'location': 'Banana Island, Lagos',
            'price': '450000000',
            'status': 'Listed',
            'publish_to_propkonet': '1',
            'beds': '6',
            'baths': '7',
            'sqft': '5500',
            'property_type': 'Mansion'
        })
        self.assertEqual(res.status_code, 200)

        # 10. Check PropkoNet displays both verified listings
        public_client = app.test_client()
        res = public_client.get('/propkonet')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Luxury Waterfront Mansion', res.data)
        self.assertIn(b'Test Unverified Villa', res.data)
        self.assertIn(b'Banana Island, Lagos', res.data)
        self.assertIn(b'Samuel Okon Realty', res.data)
        self.assertIn(b'Verified Seller', res.data)

        # 11. Buyer starts a Paystack checkout via PropkoNet. The amount is
        # calculated server-side and a pending payment row is created; the seller
        # is only notified once Paystack confirms the charge (step 12).
        from unittest import mock
        import payments as flow_payments
        flow_payments.PAYSTACK_SECRET_KEY = 'sk_test_flow'
        flow_payments.PAYSTACK_WEBHOOK_SECRET = 'sk_test_flow'
        with mock.patch('payments.paystack_request', return_value={
                'authorization_url': 'https://checkout.paystack.com/test',
                'access_code': 'acc_test'}):
            res = public_client.post('/api/propkonet/buy', json={
                'property_id': prop_id,
                'property_name': 'Test Unverified Villa',
                'name': 'Chidi Obi',
                'phone': '+234 802 111 2222',
                'email': 'chidi@example.com',
                'offer_price': 84000000,
                'message': 'Ready to make immediate downpayment.'
            })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('authorization_url', data)
        reference = data.get('reference')

        # No seller notification yet - payment has not been verified.
        c = db()
        tasks_before = len(c.execute("SELECT * FROM tasks WHERE title LIKE '%Chidi Obi%'").fetchall())
        pending = c.execute("SELECT status FROM payments WHERE reference=?", (reference,)).fetchone()
        self.assertEqual(pending['status'], 'pending')
        c.close()

        # 12. Paystack confirms the charge: the seller is then notified.
        import payments as payments_mod
        from hashlib import sha512
        import hmac as hmaclib
        import json as jsonlib
        payments_mod.PAYSTACK_SECRET_KEY = 'sk_test_flow'
        payments_mod.PAYSTACK_WEBHOOK_SECRET = 'sk_test_flow'
        # The authoritative amount is the listing price (Test Unverified Villa = 85,000,000).
        amount_kobo = int(85000000) * 100
        body = jsonlib.dumps({'event': 'charge.success', 'id': 'flow-evt-' + reference,
                              'data': {'reference': reference, 'amount': amount_kobo}}).encode()
        sig = hmaclib.new(b'sk_test_flow', body, sha512).hexdigest()
        with mock.patch('payments.paystack_request', return_value={
                'status': 'success', 'reference': reference, 'amount': amount_kobo}):
            hook = public_client.post('/webhooks/paystack', data=body,
                                      headers={'x-paystack-signature': sig})
        self.assertEqual(hook.status_code, 200)

        c = db()
        tasks_after = len(c.execute("SELECT * FROM tasks WHERE title LIKE '%Chidi Obi%'").fetchall())
        self.assertGreater(tasks_after, tasks_before)
        paid = c.execute("SELECT status FROM payments WHERE reference=?", (reference,)).fetchone()
        self.assertEqual(paid['status'], 'awaiting_customer_confirmation')
        c.close()

        # Clean up test data
        c = db()
        c.execute("DELETE FROM payments WHERE reference LIKE 'HOUSE-%'")
        c.execute("DELETE FROM payment_webhook_events WHERE event_id LIKE 'evt_flow-evt-%'")
        c.execute("DELETE FROM users WHERE email='test_seller@estimate.ng'")
        c.execute("DELETE FROM verification_requests WHERE email='test_seller@estimate.ng'")
        c.execute("DELETE FROM properties WHERE name IN ('Test Unverified Villa', 'Luxury Waterfront Mansion')")
        c.commit()
        c.close()

if __name__ == '__main__':
    unittest.main()
