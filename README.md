# Medico App — Cloud Run + Cloud SQL (PostgreSQL)

This is the **recommended package** to run your Flask medical app on Google Cloud:

- **Cloud Run**: hosts the Flask web app (works on desktop & mobile via browser).
- **Cloud SQL (PostgreSQL)**: hosted database.

## 1) Project structure
Put your files like this:

```
medico-cloudrun-cloudsql/
  app.py
  Dockerfile
  requirements.txt
  templates/
    login.html
    dashboard.html
    company_form.html
    patient_form.html
    medical_record_form.html
    select_companies_send.html
  static/
    css/style.css
    img/header.jpg
    img/Footer.jpg
```

> Add your real images to `static/img/` (header.jpg + Footer.jpg).  
> Replace `static/css/style.css` with your real CSS if you already have one.

## 2) Required environment variables (Cloud Run)
**Security**
- `SECRET_KEY`  (long random string)

**SMTP (Gmail with App Password)**
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- Optional: `SMTP_SERVER` (default smtp.gmail.com)
- Optional: `SMTP_PORT` (default 587)
- Optional: `EMAIL_SENDER` (defaults to SMTP_USERNAME)

**Cloud SQL Postgres**
- `INSTANCE_UNIX_SOCKET=/cloudsql/<INSTANCE_CONNECTION_NAME>`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- Optional: `DB_PORT` (default 5432)

Optional app init flags:
- `INIT_DB_ON_STARTUP=1` (default)
- `DEFAULT_ADMIN_USER`
- `DEFAULT_ADMIN_PASSWORD`

## 3) Create Cloud SQL (Postgres) in São Paulo (southamerica-east1)
```bash
gcloud sql instances create medico-db \
  --database-version=POSTGRES_15 \
  --region=southamerica-east1 \
  --cpu=1 --memory=3840MB \
  --storage-size=10GB

gcloud sql databases create clinicdb --instance=medico-db
gcloud sql users create clinicuser --instance=medico-db --password="YOUR_DB_PASSWORD"

gcloud sql instances describe medico-db --format="value(connectionName)"
```

## 4) Deploy to Cloud Run (recommended: deploy from source)
```bash
gcloud config set run/region southamerica-east1

INSTANCE="YOUR_PROJECT:southamerica-east1:medico-db"

gcloud run deploy medico-app \
  --source . \
  --region southamerica-east1 \
  --add-cloudsql-instances $INSTANCE \
  --set-env-vars INSTANCE_UNIX_SOCKET=/cloudsql/$INSTANCE \
  --set-env-vars DB_NAME=clinicdb,DB_USER=clinicuser,DB_PASSWORD=YOUR_DB_PASSWORD \
  --set-env-vars SECRET_KEY="YOUR_LONG_SECRET" \
  --set-env-vars SMTP_USERNAME="YOUR_GMAIL",SMTP_PASSWORD="YOUR_APP_PASSWORD",SMTP_SERVER="smtp.gmail.com",SMTP_PORT="587" \
  --allow-unauthenticated
```

## 5) Notes
- Postgres tables are **lowercase**: `companies`, `patients`, `medical_records`, `users`.
- First run creates tables automatically.
- Your app URL from Cloud Run works on desktop and mobile.
