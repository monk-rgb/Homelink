# Homelink — Nigerian Property Intelligence Webapp

Full Flask webapp generated from the supplied 1,000-row Nigerian house-price dataset. The trained model is included as `model.joblib`.

## Features
- Gold/dark responsive UI using Fraunces + Outfit.
- Welcome, Form Predict, Image Predict, About, PropkoNet and Property Manager core pages.
- Signup/login with role selection: looking for property, real-estate business, or handyman.
- Owner-only Property Manager dashboard with KPIs, deal pipeline, properties, tasks and AI document generator.
- Handymen directory: search by trade/state/pay, a links reputation score (0–10, a star at 10) and
  an AI natural-language search. Each handyman gets a **ToolBox** workspace tailored to their trade.
- Secure checkout page (`/checkout/<property_id>`) that hands off to Paystack, with a purchase-status page.
- Form prediction uses the supplied dataset and trained CatBoost regression pipeline.
- Image prediction uses a trained visual model (5–20 photos; more photos = firmer estimate) integrated
  into the Visual Analysis panel, with optional OpenAI vision enrichment.
- Camera capture uses browser `getUserMedia`.
- Required-field validation shows the requested message in the estimate area.

## Dataset
Columns used: State, City, Property_Type, Bedrooms, Bathrooms, Area_sqm, Age_Years, Parking_Spaces -> Price_NGN.
Training split: 80/20. Holdout R²: 0.8866; MAE: ₦11,151,719.

## Run
1. Install Python 3.10+
2. Create and activate a virtual environment.
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and add your OpenAI API key. Add a SerpAPI key as `SERPAPI_KEY` to fetch online listing results and prices; without it, the page still provides the local estimate and manual search links.
5. Run `python app.py`
6. Open http://127.0.0.1:5000

## Important
The dataset contains 37 state labels including the Federal Capital Territory. The requested nine property title types are supported in the UI; the trained model has learned from the five property types actually present in the supplied dataset. For unsupported form property types, the pipeline's one-hot encoder handles them as unseen categories, so those predictions should be treated cautiously.

For production: use HTTPS, a production WSGI server, a strong secret key, CSRF protection, rate limiting, persistent database hosting, object storage for uploads, and proper legal/privacy controls.

## Paystack property payments (escrow-style)

The PropkoNet **Buy Now** flow opens a dedicated checkout page (`/checkout/<property_id>`) that takes
payment through Paystack and holds the money until the buyer confirms the transaction, then releases it
to the seller 24 hours later via Paystack Transfers.

### Where the keys go
All secrets live in a single `.env` file in the project root (never committed). Copy the template and
fill it in:

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

Then open `.env` and set the Paystack values. Get both keys from
**Paystack Dashboard → Settings → API Keys & Webhooks**:

| Variable | Where to get it | Notes |
|---|---|---|
| `PAYSTACK_SECRET_KEY` | Paystack Dashboard → API Keys | **Server-side only.** Used to initialize/verify transactions and sign transfers. Never expose or commit it. |
| `PAYSTACK_PUBLIC_KEY` | Paystack Dashboard → API Keys | Safe for the browser (only needed for inline checkout). |
| `PAYSTACK_WEBHOOK_SECRET` | Optional | Leave blank to reuse `PAYSTACK_SECRET_KEY`. Set only if Paystack issues a dedicated webhook secret. |
| `PAYSTACK_CALLBACK_BASE_URL` | Your public HTTPS URL | e.g. `https://your-domain`. Leave blank locally to use the request host. |
| `PAYOUT_HOLD_HOURS` | Your policy | Hours funds are held after buyer confirmation before seller payout (default 24). |
| `PAYOUT_CRON_SECRET` | Generate it | Shared secret for `POST /internal/payouts/run`. `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `FLASK_SECRET_KEY` | Generate it | Session signing. Use a long random string in production. |

Also set the webhook URL in **Paystack Dashboard → Settings → API Keys & Webhooks → Webhook URL** to
`https://your-domain/webhooks/paystack`.

When `PAYSTACK_SECRET_KEY` is empty, `payments.paystack_enabled()` returns `False` and the checkout page
shows a clear "payments are not configured yet" notice instead of failing silently.

### Flow
1. Buyer clicks **Buy Now** → `POST /api/propkonet/buy` validates the buyer and the listing,
   calculates the amount **server-side** from the live price, creates a `payments` row and returns a
   Paystack `authorization_url`. The browser never supplies an amount, seller id or payout value.
2. Buyer pays on Paystack's hosted checkout.
3. Paystack calls `POST /webhooks/paystack`. The handler verifies the `x-paystack-signature`
   (HMAC-SHA512 of the raw body), de-duplicates by event id, re-verifies the charge with Paystack,
   checks the amount matches, and records the payment as `awaiting_customer_confirmation`.
   The frontend callback (`GET /payments/callback`) only *verifies*; it never marks success on its own.
4. Buyer confirms → `POST /api/payments/<reference>/confirm` sets `customer_confirmed_at` and
   `payout_at = now + PAYOUT_HOLD_HOURS` (server clock only).
5. The payout worker (`POST /internal/payouts/run`, or `python payout_worker.py`) finds payouts where
   `payout_status = scheduled` and `payout_at <= now`, atomically claims each one, and initiates a
   Paystack transfer to the seller's saved recipient code.
6. `transfer.success` / `transfer.failed` webhooks update the payout to successful/failed.

### Payment states
`pending → payment_successful/awaiting_customer_confirmation → payout_scheduled → payout_processing →
payout_successful`, plus `payout_failed`, `cancelled`, `refunded`, `disputed`.

### Safety
All amounts are integers in **kobo**, no floating point. Payouts are protected by `BEGIN IMMEDIATE`
claims and deterministic transfer references, so a payout can never be sent twice. Webhook events are
stored in `payment_webhook_events` and processed once. Money-affecting actions are written to
`audit_logs`. The Paystack **secret key is server-side only** and never logged or exposed.

## Scheduler
Run the payout worker every few minutes so a payout scheduled for 24 hours is released shortly after
becoming eligible. It never depends on a browser being open.

```bash
# one pass
python payout_worker.py
# continuous, every 5 minutes
python payout_worker.py --loop 300
# or, from a hosted cron service, with the shared secret
curl -X POST -H "X-Payout-Secret: $PAYOUT_CRON_SECRET" https://your-domain/internal/payouts/run
```

Real-estate transactions can require licensing, escrow/custody arrangements, AML/KYC screening and
legal review in your jurisdiction; confirm your process is approved before enabling live payments.
