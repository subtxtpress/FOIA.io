# FOIA.io

FOIA Request Management SaaS — built for investigative journalists.

---

## Quick Start (Local Development)

### Prerequisites
- Python 3.9+
- Node.js 18+ (for Word doc generation)
- pip

### Install & Run

```bash
# 1. Clone / navigate to project folder
cd foia_io

# 2. Install Python dependencies
pip3 install --break-system-packages -r requirements.txt

# 3. Install Node.js dependencies (for Word doc export)
npm install docx

# 4. Copy env template
cp .env.example .env
# Edit .env with your SECRET_KEY and Stripe keys

# 5. Run
python3 app.py

# 6. Open
open http://localhost:5000
```

---

## File Structure

```
foia_io/
├── app.py                 # Flask backend — all API routes
├── index.html             # Single-file frontend
├── generate_letter.js     # Word doc generator (Node.js / docx)
├── requirements.txt       # Python dependencies
├── package.json           # Node dependencies (after npm install)
├── .env.example           # Environment variable template
├── foia_io.db             # SQLite database (auto-created on first run)
└── README.md
```

---

## API Reference

### Auth
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Sign in |
| POST | `/api/auth/logout` | Sign out |
| GET  | `/api/auth/me` | Current session |

### Requests
| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/requests/new-number` | Preview next FOIA number |
| POST | `/api/requests` | Create new request |
| GET  | `/api/requests` | List all (filter: `?status=active`) |
| GET  | `/api/requests/:id` | Get single request |
| DELETE | `/api/requests/:id` | Delete request |
| GET  | `/api/requests/:id/generate-letter` | Auto-generate letter text |
| POST | `/api/requests/:id/save-letter` | Save letter → move to Active |
| POST | `/api/requests/:id/update` | Update active request |
| POST | `/api/requests/:id/close` | Close request |
| GET  | `/api/requests/:id/log` | Get action log |
| GET  | `/api/requests/:id/download-docx` | Download Word doc |

### Agencies
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agencies` | List all agencies |
| GET | `/api/agencies/:id` | Get agency with full contact info |

### Stripe
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/stripe/create-checkout` | Create Stripe checkout session |
| POST | `/api/stripe/webhook` | Stripe webhook handler |
| POST | `/api/dev/activate` | DEV ONLY — activate without Stripe |

---

## Stripe Setup (Production)

1. Create a product in your Stripe dashboard (e.g., "FOIA.io Monthly")
2. Get the Price ID (`price_xxx`) from the product
3. Add to `.env`:
   ```
   STRIPE_SECRET_KEY=sk_live_...
   STRIPE_PRICE_ID=price_...
   STRIPE_WEBHOOK_SECRET=whsec_...
   ```
4. In `app.py`, uncomment the Stripe SDK code in `create_checkout()`
5. Set your Stripe webhook endpoint to `https://yourdomain.com/api/stripe/webhook`
6. Listen for: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`

### Stripe checkout code (uncomment in app.py):
```python
import stripe
stripe.api_key = STRIPE_SECRET

session_obj = stripe.checkout.Session.create(
    mode="subscription",
    payment_method_types=["card"],
    line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
    customer_email=<user_email>,
    success_url="https://yourdomain.com/?subscribed=1",
    cancel_url="https://yourdomain.com/subscribe",
)
return jsonify({"url": session_obj.url})
# Then redirect frontend to session_obj.url
```

---

## Deploying to Railway

```bash
# Install Railway CLI
npm i -g @railway/cli

# Login & deploy
railway login
railway init
railway up

# Set environment variables in Railway dashboard
# Add: SECRET_KEY, STRIPE_SECRET_KEY, STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET
```

Add a `Procfile`:
```
web: gunicorn app:app --bind 0.0.0.0:$PORT
```

---

## FOIA Numbering

- Format: `FOIA-YYYY-NNN` (e.g., `FOIA-2026-001`)
- Sequence resets to 001 each calendar year
- Numbers are consumed atomically — no duplicates possible
- Year tracked in `foia_sequence` table

---

## Deadline Calculation

- Federal standard: **20 business days** from creation date
- Excludes weekends and federal holidays (2026–2027 pre-loaded)
- Users can manually override the deadline date after the fact
- Dashboard shows: days remaining / overdue count

---

## Adding More Federal Agencies

Edit the `AGENCIES` list in `app.py`. Each agency needs:
```python
{
    "name": "Full Agency Name",
    "abbreviation": "ABBR",
    "foia_officer_title": "FOIA Officer",
    "foia_email": "foia@agency.gov",
    "foia_address": "Street\nCity, State ZIP",
    "foia_phone": "(xxx) xxx-xxxx",
    "foia_fax": "(xxx) xxx-xxxx",
    "response_days": 20,
    "portal_url": "https://..."
}
```
Then delete `foia_io.db` and restart to re-seed, or INSERT directly into the `federal_agencies` table.

---

## State / Local / University (Roadmap)

Currently disabled in the jurisdiction dropdown. To enable:
1. Create a `state_agencies` table with same schema as `federal_agencies`
2. Add state-specific statute language to `build_letter_text()`
3. Update the frontend dropdown options
4. Seed with state agency contact data from state FOIA directories

---

## Accessibility

- High contrast dark mode (WCAG AAA compliant)
- System font stack (dyslexia-friendly)
- Full keyboard navigation (Esc closes modals, Ctrl+Enter saves)
- Mobile responsive from 320px up

---

## License

MIT
# FOIA.io
# FOIA.io
