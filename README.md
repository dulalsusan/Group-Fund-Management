# Group Savings Fund

A private Django application for managing a member savings fund, monthly contributions, loans, group votes, and shared financial documents. The workflows are based on the supplied Group Savings Fund Rule Book and the accompanying requirements.

The rule book specifies Bikram Sambat. Contribution periods, due dates, grace periods, loan-month calculations, and displayed dates use B.S.; reporting periods are entered as `YYYY-MM` in B.S. Dates are stored as Gregorian dates in the database for reliable Django persistence.

## Run locally

1. Install Python 3.10 or later.
2. From this folder, install Django:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Create the local database and an administrator:

   ```powershell
   python manage.py migrate
   python manage.py createsuperuser
   ```

4. Start the development server:

   ```powershell
   python manage.py runserver
   ```

5. Open <http://127.0.0.1:8000/admin/> and sign in with the superuser. In **Member profiles**, create a profile for the superuser and choose **Admin** to enable the full fund workspace. Approve member sign-ups by setting their profile status to **Active** and filling in their joining date. To create a Client, add a user under **Users**, then create a linked **Member profile** and choose **Client**. Clients can sign in to view the Group Savings Fund contact information; they cannot access member balances or records. Create a `Fund settings` record to enter any verified opening fund and bank cash balance. Do not enter the same starting balance as both an opening adjustment and ledger transactions.

Members may register at `/register/`. Their membership remains pending until an administrator activates it. Client accounts are created by an administrator.

## Roles

- **Admin:** Django superuser with full administration access, including creating/deleting users and approving Member profiles. Use `/admin/`.
- **Member:** signs up for administrator approval, sees the fund dashboard and member services, and can submit their own payment proof, vote, request loans, and manage their exit request. Financial verification and recording actions are restricted to Admin.
- **Client:** administrator-created account that can view the general Group Savings Fund information and contact number only. Clients cannot see member fund balances, records, or documents.

## Monthly operation

The dashboard creates contribution schedules for active members and applies each overdue NPR 50 monthly contribution penalty once. Run this task daily from Windows Task Scheduler so missed payments and penalties are recorded even when nobody opens the site:

```powershell
python manage.py refresh_monthly_ledger
```

Run the reminder task daily as well. It emails members on the 10th and 20th of each B.S. month, once per date. Local development prints email messages to the console; configure `DJANGO_EMAIL_BACKEND`, `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_PORT`, `DJANGO_EMAIL_USE_TLS`, `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD`, and `DJANGO_DEFAULT_FROM_EMAIL` for your mail provider.

```powershell
python manage.py send_contribution_reminders
```

Administrators use the fund workspace to review contributions, record repayments and disbursements, and upload official statements and summaries. Members can print an HTML receipt after their contribution is verified. The supplied rule book is available in the authenticated **Documents** area.

## Financial rules implemented

- NPR 5,000 monthly contribution, due on the 10th of each B.S. month with a grace period through the 20th.
- Total fund and bank-cash balances are tracked separately. Loans are assets: disbursement and principal repayment move cash without incorrectly changing fund equity. Interest, penalties, and loan charges increase the fund; payouts and administrative expenses reduce it.
- The lendable pool is capped at 80% of total fund. Free cash is the lesser of unused lendable capacity and bank cash above the 20% reserve.
- Loan requests require one active loan maximum, a NPR 5,000 minimum, a 50% normal / 60% emergency pool limit, and a two-thirds member vote excluding the applicant. The vote also meets the two-thirds transfer-consent threshold before an administrator can disburse.
- A second designated bank signatory must independently confirm withdrawals and loan transfers. Loan disbursements may be partial, deduct NPR 100 service charge and (when applicable) NPR 10 transaction charge, and accrue 0.5% monthly interest against outstanding principal.
- Past-term loan interest follows the rule book's 8% rate after the one-year term and grace period, increasing one percentage point for each additional year.
- Emergency payouts and investment proposals need unanimous approval. Administrators approve new Member accounts. Vote thresholds use eligible active members, not meeting attendance.
- Catch-up contributions, first-six-month loan restrictions, exit notices, and exit payout estimates are recorded.

## Security and deployment

Local development uses SQLite and Django's development server. Before deployment, set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, and `DJANGO_ALLOWED_HOSTS`; configure HTTPS, a production database, backups, and private durable file storage. Do not expose the `media/` directory directly: uploaded proofs and statements are intentionally served through the authenticated file view. Restrict administrator access and keep uploaded bank statements confidential.

## Tests

```powershell
python manage.py test
python manage.py check
```