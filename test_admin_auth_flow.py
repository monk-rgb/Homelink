"""End-to-end checks for the admin JWT / HttpOnly-cookie authorization flow.

Run with:  python test_admin_auth_flow.py
"""
import os
import unittest

import jwt

os.environ.setdefault('ADMIN_EMAIL', 'admin@estimate.ng')
os.environ.setdefault('ADMIN_PASSWORD', 'Mmz1810')

import app as app_module


class AdminAuthFlowTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def _login(self, client=None, email='admin@estimate.ng', password='Mmz1810'):
        client = client or self.client
        return client.post('/api/admin/login', json={'email': email, 'password': password})

    def test_login_endpoint_is_public_and_returns_201(self):
        res = self._login()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['ok'])

    def test_login_sets_httponly_admin_cookie(self):
        res = self._login()
        cookies = res.headers.getlist('Set-Cookie')
        cookie = next((c for c in cookies if c.startswith(app_module.ADMIN_COOKIE_NAME + '=')), None)
        self.assertIsNotNone(cookie, 'admin cookie was not issued')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)

    def test_issued_token_is_a_signed_jwt_with_admin_role(self):
        res = self._login()
        cookie = next(c for c in res.headers.getlist('Set-Cookie')
                      if c.startswith(app_module.ADMIN_COOKIE_NAME + '='))
        raw = cookie.split(';', 1)[0].split('=', 1)[1]
        claims = jwt.decode(raw, app_module.ADMIN_JWT_SECRET, algorithms=['HS256'])
        self.assertEqual(claims['role'], 'admin')
        self.assertEqual(claims['email'], 'admin@estimate.ng')
        self.assertIn('exp', claims)

    def test_bad_password_is_rejected(self):
        res = self._login(password='wrong-password')
        self.assertEqual(res.status_code, 401)
        self.assertNotIn(app_module.ADMIN_COOKIE_NAME + '=', ''.join(res.headers.getlist('Set-Cookie')))

    def test_protected_action_without_token_returns_401(self):
        res = self.client.post('/Admin/verification/1/accept')
        self.assertEqual(res.status_code, 401)

    def test_protected_action_with_customer_token_returns_403(self):
        # A correctly-signed but non-admin (customer) token must be forbidden.
        customer = jwt.encode({'userId': 123, 'role': 'customer'},
                              app_module.ADMIN_JWT_SECRET, algorithm='HS256')
        self.client.set_cookie(app_module.ADMIN_COOKIE_NAME, customer, domain='localhost')
        res = self.client.post('/Admin/verification/1/accept')
        self.assertEqual(res.status_code, 403)

    def test_protected_action_with_forged_signature_returns_401(self):
        forged = jwt.encode({'userId': 1, 'role': 'admin'}, 'attacker-secret', algorithm='HS256')
        self.client.set_cookie(app_module.ADMIN_COOKIE_NAME, forged, domain='localhost')
        res = self.client.post('/Admin/verification/1/accept')
        self.assertEqual(res.status_code, 401)

    def test_api_protected_route_returns_json_401_without_token(self):
        # JSON clients (e.g. /api/admin/*) get a JSON body, not an HTML page.
        res = self.client.get('/Admin/verification/1/document/face_photo',
                              headers={'Accept': 'application/json'})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()['error'], 'Authentication required. Please log in as an admin.')

    def test_admin_cookie_grants_access_and_dashboard_renders(self):
        self._login()
        res = self.client.get('/Admin')
        self.assertEqual(res.status_code, 200)
        # The dashboard (not the login card) should be rendered for a valid token.
        self.assertIn(b'Admin dashboard', res.data)

    def test_bearer_header_is_accepted(self):
        res = self._login()
        raw = res.headers.getlist('Set-Cookie')[0].split(';', 1)[0].split('=', 1)[1]
        api = app_module.app.test_client()
        out = api.get('/Admin', headers={'Authorization': f'Bearer {raw}'})
        self.assertEqual(out.status_code, 200)

    def test_mfa_code_is_enforced_when_configured(self):
        c = app_module.db()
        c.execute("UPDATE admin_users SET mfa_secret='654321' WHERE email=?", (app_module.ADMIN_EMAIL,))
        c.commit(); c.close()
        try:
            self.assertEqual(self._login().status_code, 401)
            res = self.client.post('/api/admin/login',
                                   json={'email': app_module.ADMIN_EMAIL, 'password': 'Mmz1810',
                                         'mfa_code': '000'})
            self.assertEqual(res.status_code, 401)
            res = self.client.post('/api/admin/login',
                                   json={'email': app_module.ADMIN_EMAIL, 'password': 'Mmz1810',
                                         'mfa_code': '654321'})
            self.assertEqual(res.status_code, 200)
        finally:
            c = app_module.db()
            c.execute("UPDATE admin_users SET mfa_secret=NULL WHERE email=?", (app_module.ADMIN_EMAIL,))
            c.commit(); c.close()

    def test_logout_clears_the_cookie(self):
        self._login()
        res = self.client.post('/Admin/logout')
        cleared = ''.join(res.headers.getlist('Set-Cookie'))
        self.assertIn(app_module.ADMIN_COOKIE_NAME + '=;', cleared.replace(' ', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
