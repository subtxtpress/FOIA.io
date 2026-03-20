# FOIA.io

FOIA Request Management — built for investigative journalists.

A fully client-side web app powered by [Supabase](https://supabase.com) and hosted on [GitHub Pages](https://pages.github.com). No server required.

---

## Architecture

```
index.html          → Single-file frontend (HTML + CSS + JS)
Supabase Postgres   → Database (agencies, requests, state laws, etc.)
Supabase Auth       → Email/password authentication
Supabase Storage    → File attachments (20MB limit)
Supabase Edge Fn    → Stripe webhook for subscription processing
GitHub Pages        → Static file hosting (free)
```

## Tiers

| Plan | Price | Features |
|------|-------|----------|
| **Free** | $0/mo | Up to 5 active requests, deadline tracking, notes, attachments, action history |
| **Pro** | $5/mo | Unlimited requests, professional appeal letters, priority support |

---

## Local Development

Just open `index.html` in a browser. It connects directly to Supabase — no local server needed.

For a local dev server (to avoid CORS issues with file://):
```bash
python3 -m http.server 5000
open http://localhost:5000
```

---

## Supabase Setup

The app connects to Supabase project `raqonwahukpejuftbqav`. Configuration is in `index.html`:
- `SUPABASE_URL` — Project API URL
- `SUPABASE_ANON_KEY` — Publishable anon key

### Database Tables
| Table | Rows | Description |
|-------|------|-------------|
| `profiles` | — | User profiles (linked to auth.users) |
| `requests` | — | FOIA request tracking |
| `action_log` | — | Request activity history |
| `request_attachments` | — | File attachment metadata |
| `federal_agencies` | 611 | Federal agency FOIA contacts |
| `state_local_agencies` | 17,979 | State/local law enforcement agencies |
| `state_laws` | 50 | State public records laws |
| `holidays` | 22 | Federal holidays (for deadline calculation) |
| `invite_codes` | — | Beta access invite codes |

### Edge Functions
| Function | Purpose |
|----------|---------|
| `stripe-webhook` | Processes Stripe subscription events |

### RLS (Row Level Security)
All tables have RLS enabled. Users can only read/write their own data. Reference tables (agencies, state laws, holidays) are read-only for all authenticated users.

---

## Stripe Setup

1. Create a product + price in your Stripe dashboard
2. Set the webhook URL to: `https://raqonwahukpejuftbqav.supabase.co/functions/v1/stripe-webhook`
3. Add `STRIPE_WEBHOOK_SECRET` to Supabase Edge Function secrets
4. Listen for: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`

---

## Deploying to GitHub Pages

1. Push to `main` branch
2. Go to repo Settings → Pages → Source: Deploy from branch → `main` / `/ (root)`
3. (Optional) Add custom domain in the Pages settings and create a `CNAME` file

---

## Archive

The `archive/` folder contains the original Flask/Python backend (`app.py`, `db.py`, etc.) from before the Supabase migration. These files are kept for reference but are not used.

---

## License

MIT
