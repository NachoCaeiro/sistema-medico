import os
import socket
import ssl
import datetime
from io import BytesIO
from flask import send_file

from datetime import date
from collections import defaultdict
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import IntegrityError

from flask import Flask, render_template, request, redirect, url_for, session, make_response, flash
from werkzeug.security import generate_password_hash, check_password_hash
from fpdf import FPDF

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication


# ----------------------------
# App + Config (Cloud Run)
# ----------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", SMTP_USERNAME)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def get_today_iso():
    return date.today().isoformat()


# ----------------------------
# Neon (PostgreSQL)
# ----------------------------
def get_db_connection():
    return psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=RealDictCursor,
        sslmode="require"
    )



def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS companies (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        address TEXT,
        phone TEXT,
        email TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS patients (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        surname TEXT NOT NULL,
        document_number TEXT UNIQUE NOT NULL,
        phone TEXT,
        email TEXT,
        age INTEGER,
        company_id INTEGER REFERENCES companies(id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS medical_records (
        id SERIAL PRIMARY KEY,
        patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        diagnosis TEXT NOT NULL,
        date DATE NOT NULL,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        license_type TEXT,
        justified_days INTEGER,
        license_start DATE,
        license_end DATE,
        return_date DATE,
        observations TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    );
    """)

    conn.commit()
    cur.close()
    conn.close()


def add_default_user():
    """
    Crea el usuario admin una sola vez usando variables de entorno.
    Recomendado en Cloud Run: NO hardcodear.
    """

    username = os.environ.get("ADMIN_USER")
    password = os.environ.get("ADMIN_PASSWORD")

    # Si no están seteadas, NO crear nada (evita crear con defaults inseguros)
    if not username or not password:
        print("[INFO] ADMIN_USER / ADMIN_PASSWORD no seteadas. No se crea usuario por defecto.")
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    user = cur.fetchone()

    if user is None:
        password_hash = generate_password_hash(password)
        try:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                (username, password_hash),
            )
            conn.commit()
            print(f"Usuario '{username}' creado exitosamente.")
        except IntegrityError:
            conn.rollback()
            print(f"Usuario '{username}' ya existe (IntegrityError).")
    else:
        print(f"Usuario '{username}' ya existe.")

    cur.close()
    conn.close()



# Inicializar DB al arrancar (CREATE TABLE IF NOT EXISTS es seguro)
# En Cloud Run puede ejecutarse por instancia/worker, pero no rompe.
if os.environ.get("INIT_DB_ON_STARTUP", "1") == "1":
    try:
        init_db()
        add_default_user()
    except Exception as e:
        print(f"[WARN] init_db/add_default_user falló al inicio: {e}")


# ----------------------------
# Helpers
# ----------------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Por favor, inicie sesión para acceder a esta página.", "info")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def parse_date(value):
    """Convierte 'YYYY-MM-DD' a date, o devuelve None."""
    if not value:
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.date() if isinstance(value, datetime.datetime) else value
    try:
        return datetime.date.fromisoformat(str(value))
    except Exception:
        return None


def fmt_date_ddmmyyyy(value):
    """Formatea date/datetime/str a dd/mm/yyyy."""
    d = parse_date(value)
    if not d:
        return ""
    return d.strftime("%d/%m/%Y")


def static_path(*parts):
    return os.path.join(BASE_DIR, "static", *parts)


# ----------------------------
# Auth
# ----------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash("Inicio de sesión exitoso.", "success")
            return redirect(url_for("home"))
        error = "Usuario o contraseña incorrectos. Por favor, intente de nuevo."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada exitosamente.", "info")
    return redirect(url_for("login"))


# ----------------------------
# Dashboard
# ----------------------------
@app.route("/")
@login_required
def home():
    conn = get_db_connection()
    cur = conn.cursor()

    search_company_name = request.args.get("search_company_name", "").strip()
    search_patient_document = request.args.get("search_patient_document", "").strip()

    selected_patient = None
    medical_history = []

    # Companies
    company_query = "SELECT id, name, address, phone, email FROM companies"
    company_params = []
    if search_company_name:
        company_query += " WHERE name ILIKE %s"
        company_params.append(f"%{search_company_name}%")
    company_query += " ORDER BY name"

    cur.execute(company_query, company_params)
    companies = cur.fetchall()

    # Selected patient + history
    if search_patient_document:
        cur.execute(
            """
            SELECT p.*, c.name AS company_name
            FROM patients p
            LEFT JOIN companies c ON p.company_id = c.id
            WHERE p.document_number = %s
            """,
            (search_patient_document,),
        )
        selected_patient = cur.fetchone()

        if selected_patient:
            cur.execute(
                """
                SELECT mr.*, c.name AS company_name
                FROM medical_records mr
                JOIN companies c ON mr.company_id = c.id
                WHERE mr.patient_id = %s
                ORDER BY mr.date DESC, mr.id DESC
                """,
                (selected_patient["id"],),
            )
            medical_history = cur.fetchall()
        else:
            flash(f"Paciente con documento '{search_patient_document}' no encontrado.", "warning")

    # Patients list (optionally filtered by company ids)
    patient_query = """
        SELECT p.*, c.name AS company_name
        FROM patients p
        LEFT JOIN companies c ON p.company_id = c.id
    """
    patient_params = []

    if search_company_name:
        matching_company_ids = [c["id"] for c in companies]
        if matching_company_ids:
            placeholders = ",".join(["%s"] * len(matching_company_ids))
            patient_query += f" WHERE p.company_id IN ({placeholders})"
            patient_params.extend(matching_company_ids)
        else:
            patient_query += " WHERE 1=0"

    patient_query += " ORDER BY p.surname, p.name"
    cur.execute(patient_query, patient_params)
    patients = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "dashboard.html",
        companies=companies,
        patients=patients,
        selected_patient=selected_patient,
        medical_history=medical_history,
        search_company_name_value=search_company_name,
        search_patient_document_value=search_patient_document,
    )


# ----------------------------
# Companies
# ----------------------------
@app.route("/add_company", methods=["GET", "POST"])
@login_required
def add_company():
    if request.method == "POST":
        name = request.form["name"]
        address = request.form["address"]
        phone = request.form["phone"]
        email = request.form["email"]

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO companies (name, address, phone, email) VALUES (%s, %s, %s, %s)",
                (name, address, phone, email),
            )
            conn.commit()
            flash("Empresa agregada exitosamente.", "success")
        except IntegrityError:
            conn.rollback()
            flash("Error: El correo electrónico ya existe para otra empresa.", "danger")
            return (
                render_template(
                    "company_form.html",
                    company=request.form,
                    form_action_url=url_for("add_company"),
                    error="El correo electrónico ya existe.",
                ),
                400,
            )
        finally:
            cur.close()
            conn.close()

        return redirect(url_for("home"))

    return render_template("company_form.html", form_action_url=url_for("add_company"))


@app.route("/edit_company/<int:company_id>", methods=["GET", "POST"])
@login_required
def edit_company(company_id):
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form["name"]
        address = request.form["address"]
        phone = request.form["phone"]
        email = request.form["email"]

        try:
            cur.execute(
                """
                UPDATE companies
                SET name = %s, address = %s, phone = %s, email = %s
                WHERE id = %s
                """,
                (name, address, phone, email, company_id),
            )
            conn.commit()
            flash("Empresa actualizada exitosamente.", "success")
        except IntegrityError:
            conn.rollback()
            flash("Error: El correo electrónico ya existe para otra empresa.", "danger")
            company_data_for_form = dict(request.form)
            company_data_for_form["id"] = company_id
            cur.close()
            conn.close()
            return (
                render_template(
                    "company_form.html",
                    company=company_data_for_form,
                    form_action_url=url_for("edit_company", company_id=company_id),
                    error="El correo electrónico ya existe.",
                ),
                400,
            )
        finally:
            cur.close()
            conn.close()

        return redirect(url_for("home"))

    cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
    company = cur.fetchone()
    cur.close()
    conn.close()

    if company is None:
        flash("Empresa no encontrada.", "warning")
        return redirect(url_for("home"))

    return render_template(
        "company_form.html",
        company=company,
        form_action_url=url_for("edit_company", company_id=company_id),
    )


@app.route("/delete_company/<int:company_id>", methods=["POST"])
@login_required
def delete_company(company_id):
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT id FROM patients WHERE company_id = %s", (company_id,))
        patient_rows = cur.fetchall()
        patient_ids = [r["id"] for r in patient_rows]

        if patient_ids:
            placeholders = ",".join(["%s"] * len(patient_ids))
            cur.execute(f"DELETE FROM medical_records WHERE patient_id IN ({placeholders})", patient_ids)

        cur.execute("DELETE FROM patients WHERE company_id = %s", (company_id,))
        cur.execute("DELETE FROM companies WHERE id = %s", (company_id,))
        conn.commit()

        flash("Empresa y todos sus pacientes y registros médicos asociados eliminados exitosamente.", "success")

    except Exception as e:
        conn.rollback()
        print(f"Error al eliminar empresa: {e}")
        flash("Error al eliminar la empresa y sus datos asociados.", "danger")

    finally:
        cur.close()
        conn.close()

    return redirect(url_for("home"))


# ----------------------------
# Patients
# ----------------------------
@app.route("/add_patient", methods=["GET", "POST"])
@login_required
def add_patient():
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form["name"]
        surname = request.form["surname"]
        document_number = request.form["document_number"]
        phone = request.form.get("phone")
        email = request.form.get("email")
        age_str = request.form.get("age")
        age = int(age_str) if age_str and age_str.isdigit() else None
        company_id_str = request.form.get("company_id")
        company_id = int(company_id_str) if company_id_str and company_id_str.isdigit() else None

        if not company_id:
            flash("Error: La empresa debe ser seleccionada.", "danger")
            cur.execute("SELECT id, name FROM companies ORDER BY name")
            companies_for_form = cur.fetchall()
            cur.close()
            conn.close()
            return render_template(
                "patient_form.html",
                form_action_url=url_for("add_patient"),
                companies=companies_for_form,
                error="La empresa debe ser seleccionada.",
                patient_data=request.form,
            )

        try:
            cur.execute(
                """
                INSERT INTO patients (name, surname, document_number, phone, email, age, company_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (name, surname, document_number, phone, email, age, company_id),
            )
            conn.commit()
            flash("Paciente agregado exitosamente.", "success")

        except IntegrityError as e:
            conn.rollback()
            error_message = "Error de integridad. Verifique sus datos."
            if "document_number" in str(e):
                error_message = "El número de documento ya existe para otro paciente."
            flash(f"Error: {error_message}", "danger")

            cur.execute("SELECT id, name FROM companies ORDER BY name")
            companies_for_form = cur.fetchall()
            cur.close()
            conn.close()
            return render_template(
                "patient_form.html",
                form_action_url=url_for("add_patient"),
                companies=companies_for_form,
                error=error_message,
                patient_data=request.form,
            )

        finally:
            cur.close()
            conn.close()

        return redirect(url_for("home"))

    cur.execute("SELECT id, name FROM companies ORDER BY name")
    companies_data = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("patient_form.html", companies=companies_data, form_action_url=url_for("add_patient"))


@app.route("/edit_patient/<int:patient_id>", methods=["GET", "POST"])
@login_required
def edit_patient(patient_id):
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        name = request.form["name"]
        surname = request.form["surname"]
        document_number = request.form["document_number"]
        phone = request.form.get("phone")
        email = request.form.get("email")
        age_str = request.form.get("age")
        age = int(age_str) if age_str and age_str.isdigit() else None
        company_id_str = request.form.get("company_id")
        company_id = int(company_id_str) if company_id_str and company_id_str.isdigit() else None

        if not company_id:
            flash("Error: La empresa debe ser seleccionada.", "danger")
            cur.execute("SELECT id, name FROM companies ORDER BY name")
            companies_for_form = cur.fetchall()
            current_form_data = dict(request.form)
            current_form_data["id"] = patient_id
            cur.close()
            conn.close()
            return render_template(
                "patient_form.html",
                patient_data=current_form_data,
                companies=companies_for_form,
                form_action_url=url_for("edit_patient", patient_id=patient_id),
                error="La empresa debe ser seleccionada.",
            )

        try:
            cur.execute(
                """
                UPDATE patients
                SET name=%s, surname=%s, document_number=%s, phone=%s, email=%s, age=%s, company_id=%s
                WHERE id=%s
                """,
                (name, surname, document_number, phone, email, age, company_id, patient_id),
            )
            conn.commit()
            flash("Paciente actualizado exitosamente.", "success")

        except IntegrityError as e:
            conn.rollback()
            error_message = "Error de integridad. Verifique sus datos."
            if "document_number" in str(e):
                error_message = "El número de documento ya existe para otro paciente."
            flash(f"Error: {error_message}", "danger")

            cur.execute("SELECT id, name FROM companies ORDER BY name")
            companies_for_form = cur.fetchall()
            current_form_data = dict(request.form)
            current_form_data["id"] = patient_id
            cur.close()
            conn.close()
            return render_template(
                "patient_form.html",
                patient_data=current_form_data,
                companies=companies_for_form,
                form_action_url=url_for("edit_patient", patient_id=patient_id),
                error=error_message,
            )

        finally:
            cur.close()
            conn.close()

        return redirect(url_for("home"))

    cur.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
    patient_data = cur.fetchone()
    if patient_data is None:
        flash("Paciente no encontrado.", "warning")
        cur.close()
        conn.close()
        return redirect(url_for("home"))

    cur.execute("SELECT id, name FROM companies ORDER BY name")
    companies_data = cur.fetchall()
    cur.close()
    conn.close()

    return render_template(
        "patient_form.html",
        patient=patient_data,
        companies=companies_data,
        form_action_url=url_for("edit_patient", patient_id=patient_id),
    )


@app.route("/delete_patient/<int:patient_id>", methods=["POST"])
@login_required
def delete_patient(patient_id):
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("DELETE FROM medical_records WHERE patient_id = %s", (patient_id,))
        cur.execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        conn.commit()
        flash("Paciente y sus registros médicos asociados eliminados exitosamente.", "success")
    except Exception as e:
        conn.rollback()
        print(f"Error al eliminar paciente: {e}")
        flash("Error al eliminar el paciente y sus registros asociados.", "danger")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for("home"))


def get_patient_company_id(patient_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT company_id FROM patients WHERE id = %s", (patient_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result["company_id"] if result else None


# ----------------------------
# Medical Records
# ----------------------------
@app.route("/add_medical_record", methods=["GET", "POST"])
@login_required
def add_medical_record():
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        action = request.form.get("action")  # guardar o guardar y enviar

        patient_id_form = request.form.get("patient_id", type=int)
        diagnosis = request.form["diagnosis"]
        visit_date = parse_date(request.form["date"])  # DATE
        license_type_list = request.form.getlist("license_type")
        license_type = ", ".join(license_type_list) if license_type_list else None

        justified_days = request.form.get("justified_days", type=int)
        license_start = parse_date(request.form.get("license_start"))
        license_end = parse_date(request.form.get("license_end"))
        return_date = parse_date(request.form.get("return_date"))
        observations = request.form.get("observations")

        if not patient_id_form:
            flash("Error: El paciente debe ser seleccionado.", "danger")
            cur.execute("SELECT id, name, surname, document_number FROM patients ORDER BY surname, name")
            patients_for_form = cur.fetchall()
            cur.close()
            conn.close()
            return render_template(
                "medical_record_form.html",
                patients=patients_for_form,
                form_action_url=url_for("add_medical_record"),
                error="El paciente debe ser seleccionado.",
                record_data=request.form,
            )

        company_id = get_patient_company_id(patient_id_form)
        if company_id is None:
            flash("Error: El paciente seleccionado no tiene una empresa asociada.", "danger")
            cur.execute("SELECT id, name, surname, document_number FROM patients ORDER BY surname, name")
            patients_for_form = cur.fetchall()
            cur.close()
            conn.close()
            return render_template(
                "medical_record_form.html",
                patients=patients_for_form,
                form_action_url=url_for("add_medical_record"),
                error="El paciente seleccionado no tiene una empresa asociada.",
                record_data=request.form,
            )

        try:
            cur.execute(
                """
                INSERT INTO medical_records
                  (patient_id, diagnosis, date, company_id, license_type, justified_days, license_start, license_end, return_date, observations)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    patient_id_form,
                    diagnosis,
                    visit_date,
                    company_id,
                    license_type,
                    justified_days,
                    license_start,
                    license_end,
                    return_date,
                    observations,
                ),
            )
            new_record_id = cur.fetchone()["id"]
            conn.commit()
            flash("Registro médico agregado exitosamente.", "success")
        except Exception as e:
            conn.rollback()
            print(f"Error al agregar registro médico: {e}")
            flash(f"Error al agregar el registro médico: {e}", "danger")
            cur.execute("SELECT id, name, surname, document_number FROM patients ORDER BY surname, name")
            patients_for_form = cur.fetchall()
            cur.close()
            conn.close()
            return render_template(
                "medical_record_form.html",
                patients=patients_for_form,
                form_action_url=url_for("add_medical_record"),
                error=f"Ocurrió un error: {e}",
                record_data=request.form,
            )
        finally:
            cur.close()
            conn.close()

        if action == "save_and_send":
            success = send_medical_record_email(new_record_id)
            flash("Correo enviado exitosamente." if success else "Error al enviar el correo.", "success" if success else "danger")

        patient_doc_conn = get_db_connection()
        doc_cur = patient_doc_conn.cursor()
        doc_cur.execute("SELECT document_number FROM patients WHERE id = %s", (patient_id_form,))
        patient_doc_num_row = doc_cur.fetchone()
        doc_cur.close()
        patient_doc_conn.close()

        if patient_doc_num_row:
            return redirect(url_for("home", search_patient_document=patient_doc_num_row["document_number"]))
        return redirect(url_for("home"))

    cur.execute("SELECT id, name, surname, document_number FROM patients ORDER BY surname, name")
    patients_data = cur.fetchall()
    cur.close()
    conn.close()

    return render_template(
        "medical_record_form.html",
        patients=patients_data,
        form_action_url=url_for("add_medical_record"),
        current_date=get_today_iso(),
    )


@app.route("/add_medical_record/<int:patient_id>", methods=["GET", "POST"])
@login_required
def add_medical_record_for_patient(patient_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT p.*, c.name as company_name
        FROM patients p
        LEFT JOIN companies c ON p.company_id = c.id
        WHERE p.id = %s
        """,
        (patient_id,),
    )
    patient = cur.fetchone()

    if not patient:
        flash("Paciente no encontrado.", "warning")
        cur.close()
        conn.close()
        return redirect(url_for("home"))

    if request.method == "POST":
        diagnosis = request.form["diagnosis"]
        visit_date = parse_date(request.form["date"])
        company_id = patient["company_id"]

        license_type_list = request.form.getlist("license_type")
        license_type = ", ".join(license_type_list) if license_type_list else None

        justified_days = request.form.get("justified_days", type=int)
        license_start = parse_date(request.form.get("license_start"))
        license_end = parse_date(request.form.get("license_end"))
        return_date = parse_date(request.form.get("return_date"))
        observations = request.form.get("observations")

        if not company_id:
            flash("Error: La información de la empresa del paciente está incompleta.", "danger")
            cur.close()
            conn.close()
            return render_template(
                "medical_record_form.html",
                patient=patient,
                company_name=patient.get("company_name"),
                form_action_url=url_for("add_medical_record_for_patient", patient_id=patient_id),
                error="La información de la empresa del paciente está incompleta.",
                record_data=request.form,
            )

        try:
            cur.execute(
                """
                INSERT INTO medical_records
                  (patient_id, diagnosis, date, company_id, license_type, justified_days, license_start, license_end, return_date, observations)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    patient_id,
                    diagnosis,
                    visit_date,
                    company_id,
                    license_type,
                    justified_days,
                    license_start,
                    license_end,
                    return_date,
                    observations,
                ),
            )
            record_id = cur.fetchone()["id"]
            conn.commit()

            if request.form.get("action") == "save_and_send":
                send_medical_record_email(record_id)
                flash("Registro médico agregado y enviado por correo.", "success")
            else:
                flash("Registro médico agregado exitosamente para el paciente.", "success")

        except Exception as e:
            conn.rollback()
            print(f"Error al agregar registro médico para paciente {patient_id}: {e}")
            flash(f"Error al agregar el registro médico: {e}", "danger")
            cur.close()
            conn.close()
            return render_template(
                "medical_record_form.html",
                patient=patient,
                company_name=patient.get("company_name"),
                form_action_url=url_for("add_medical_record_for_patient", patient_id=patient_id),
                error=f"Ocurrió un error: {e}",
                record_data=request.form,
            )
        finally:
            cur.close()
            conn.close()

        return redirect(url_for("home", search_patient_document=patient["document_number"]))

    cur.close()
    conn.close()
    return render_template(
        "medical_record_form.html",
        patient=patient,
        company_name=patient.get("company_name"),
        form_action_url=url_for("add_medical_record_for_patient", patient_id=patient_id),
        current_date=get_today_iso(),
    )


@app.route("/edit_medical_record/<int:record_id>", methods=["GET", "POST"])
@login_required
def edit_medical_record(record_id):
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        diagnosis = request.form["diagnosis"]
        visit_date = parse_date(request.form["date"])
        license_type_list = request.form.getlist("license_type")
        license_type = ", ".join(license_type_list) if license_type_list else None

        justified_days = request.form.get("justified_days", type=int)
        license_start = parse_date(request.form.get("license_start"))
        license_end = parse_date(request.form.get("license_end"))
        return_date = parse_date(request.form.get("return_date"))
        observations = request.form.get("observations")

        cur.execute("SELECT patient_id FROM medical_records WHERE id = %s", (record_id,))
        record_for_redirect = cur.fetchone()

        try:
            cur.execute(
                """
                UPDATE medical_records
                SET diagnosis=%s, date=%s, license_type=%s, justified_days=%s,
                    license_start=%s, license_end=%s, return_date=%s, observations=%s
                WHERE id=%s
                """,
                (
                    diagnosis,
                    visit_date,
                    license_type,
                    justified_days,
                    license_start,
                    license_end,
                    return_date,
                    observations,
                    record_id,
                ),
            )
            conn.commit()
            flash("Registro médico actualizado exitosamente.", "success")
        except Exception as e:
            conn.rollback()
            print(f"Error al actualizar registro médico: {e}")
            flash(f"Error al actualizar el registro médico: {e}", "danger")
        finally:
            cur.close()
            conn.close()

        if record_for_redirect:
            patient_conn_redirect = get_db_connection()
            pc = patient_conn_redirect.cursor()
            pc.execute("SELECT document_number FROM patients WHERE id = %s", (record_for_redirect["patient_id"],))
            patient_info_redirect = pc.fetchone()
            pc.close()
            patient_conn_redirect.close()
            if patient_info_redirect:
                return redirect(url_for("home", search_patient_document=patient_info_redirect["document_number"]))

        return redirect(url_for("home"))

    cur.execute(
        """
        SELECT mr.*,
               p.name as patient_name,
               p.surname as patient_surname,
               p.document_number as patient_document_number,
               c.name as company_name
        FROM medical_records mr
        JOIN patients p ON mr.patient_id = p.id
        LEFT JOIN companies c ON mr.company_id = c.id
        WHERE mr.id = %s
        """,
        (record_id,),
    )
    record = cur.fetchone()
    if record is None:
        flash("Registro médico no encontrado.", "warning")
        cur.close()
        conn.close()
        return redirect(url_for("home"))

    patient_details = {
        "name": record["patient_name"],
        "surname": record["patient_surname"],
        "document_number": record["patient_document_number"],
    }
    company_details = {"name": record.get("company_name")}

    cur.close()
    conn.close()

    return render_template(
        "medical_record_form.html",
        record=record,
        patient_details=patient_details,
        company_details=company_details,
        form_action_url=url_for("edit_medical_record", record_id=record_id),
    )


@app.route("/delete_medical_record/<int:record_id>", methods=["POST"])
@login_required
def delete_medical_record(record_id):
    conn = get_db_connection()
    cur = conn.cursor()
    patient_doc_for_redirect = None

    try:
        cur.execute(
            """
            SELECT p.document_number
            FROM medical_records mr
            JOIN patients p ON mr.patient_id = p.id
            WHERE mr.id = %s
            """,
            (record_id,),
        )
        patient_info = cur.fetchone()
        if patient_info:
            patient_doc_for_redirect = patient_info["document_number"]

        cur.execute("DELETE FROM medical_records WHERE id = %s", (record_id,))
        conn.commit()
        flash("Registro médico eliminado exitosamente.", "success")
    except Exception as e:
        conn.rollback()
        print(f"Error al eliminar registro médico: {e}")
        flash("Error al eliminar el registro médico.", "danger")
    finally:
        cur.close()
        conn.close()

    if patient_doc_for_redirect:
        return redirect(url_for("home", search_patient_document=patient_doc_for_redirect))
    return redirect(url_for("home"))

def build_pdf_from_record(record):
    # --- Header / Footer sizes ---
    HEADER_H = 42
    FOOTER_H = 32

    pdf = FPDF(format="A4", unit="mm")

    # Reservar espacio para que el texto no pise el footer
    FOOTER_OFFSET = 8
    pdf.set_auto_page_break(auto=True, margin=FOOTER_H + FOOTER_OFFSET + 6)

    pdf.add_page()

    # --- Medidas hoja A4 ---
    PAGE_W = pdf.w  # 210
    PAGE_H = pdf.h  # 297

    # --- Colores ---
    title_color = (33, 37, 104)
    field_color = (0, 0, 0)
    line_color = (86, 189, 181)

    # =========================
    # HEADER (full width)
    # =========================
    header_path = static_path("img", "header.jpg")
    if os.path.exists(header_path):
        pdf.image(header_path, x=0, y=0, w=PAGE_W)

    # Arranca contenido más abajo
    CONTENT_TOP = HEADER_H + 20
    pdf.set_y(CONTENT_TOP)

    # Márgenes del contenido
    LEFT = 18
    RIGHT = 18
    pdf.set_left_margin(LEFT)
    pdf.set_right_margin(RIGHT)

    # Helpers
    def hline(y):
        pdf.set_draw_color(*line_color)
        pdf.set_line_width(0.8)
        pdf.line(LEFT, y, PAGE_W - RIGHT, y)

    def label_value(label, value, y=None, label_w=45, gap=2, font_size=11, line_h=5):
        if y is not None:
            pdf.set_y(y)

        x0 = LEFT
        y0 = pdf.get_y()

        # Label
        pdf.set_xy(x0, y0)
        pdf.set_font("Arial", "B", font_size)
        pdf.set_text_color(*title_color)
        pdf.cell(label_w, line_h, label, border=0)

        # Value
        pdf.set_xy(x0 + label_w + gap, y0)
        pdf.set_font("Arial", "", font_size)
        pdf.set_text_color(*field_color)

        value = value or ""
        value_w = (PAGE_W - RIGHT) - (x0 + label_w + gap)

        # Si entra en una línea -> cell (más compacto)
        if pdf.get_string_width(value) <= value_w:
            pdf.cell(value_w, line_h, value, border=0, ln=1)
            return y0 + line_h
        else:
            pdf.multi_cell(value_w, line_h, value, border=0)
            return pdf.get_y()

    def section_title(text, y=None):
        if y is not None:
            pdf.set_y(y)
        pdf.set_font("Arial", "B", 12)
        pdf.set_text_color(*title_color)
        pdf.cell(0, 7, text, ln=1)

    def fmt_iso_to_ddmmyyyy(s):
        if not s:
            return ""
        try:
            if isinstance(s, (datetime.date, datetime.datetime)):
                d = s.date() if isinstance(s, datetime.datetime) else s
                return d.strftime("%d/%m/%Y")
            return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
        except Exception:
            return str(s)

    # =========================
    # CONTENIDO
    # =========================
    y = pdf.get_y()

    hline(y); y += 2
    y = label_value("EMPRESA:", record.get("company_name", ""), y=y, label_w=28)
    y += 1

    hline(y); y += 2
    full_name = f"{record.get('patient_name','')} {record.get('patient_surname','')}".strip()
    y = label_value("Nombre y Apellido:", full_name, y=y, label_w=45)
    y = label_value("DNI:", record.get("document_number", ""), y=y, label_w=12)
    y += 1

    hline(y); y += 2
    section_title("EXAMEN", y=y)
    y = pdf.get_y()
    y = label_value("Fecha:", fmt_iso_to_ddmmyyyy(record.get("date")), y=y, label_w=15)
    y += 1

    license_str = record.get("license_type") or ""
    lic_value = "Enfermedad Inculpable" if "Enfermedad Inculpable" in license_str else ("ART" if "ART" in license_str else "")
    y = label_value("Tipo de licencia:", lic_value, y=y, label_w=35)
    y += 1

    section_title("Descripción:", y=y)
    pdf.set_font("Arial", "", 11)
    pdf.set_text_color(*field_color)
    desc_w = PAGE_W - LEFT - RIGHT
    pdf.multi_cell(desc_w, 5, record.get("diagnosis", "") or "", border=0)
    y = pdf.get_y() + 1

    hline(y); y += 2
    section_title("LICENCIA", y=y)
    y = pdf.get_y()

    y = label_value("Días justificados:", str(record.get("justified_days") or ""), y=y, label_w=40)

    desde = fmt_iso_to_ddmmyyyy(record.get("license_start"))
    hasta = fmt_iso_to_ddmmyyyy(record.get("license_end"))

    pdf.set_y(y)
    pdf.set_font("Arial", "B", 11); pdf.set_text_color(*title_color); pdf.cell(14, 5, "Desde:", 0, 0)
    pdf.set_font("Arial", "", 11);  pdf.set_text_color(*field_color); pdf.cell(50, 5, desde, 0, 0)

    pdf.set_font("Arial", "B", 11); pdf.set_text_color(*title_color); pdf.cell(14, 5, "Hasta:", 0, 0)
    pdf.set_font("Arial", "", 11);  pdf.set_text_color(*field_color); pdf.cell(0, 5, hasta, 0, 1)

    y = pdf.get_y()
    y = label_value("Fecha reincorporación:", fmt_iso_to_ddmmyyyy(record.get("return_date")), y=y, label_w=45)
    y += 1

    section_title("Observaciones:", y=y)
    pdf.set_font("Arial", "", 11)
    pdf.set_text_color(*field_color)
    pdf.multi_cell(desc_w, 5, record.get("observations", "") or "", border=0)

    # =========================
    # FOOTER (fijo abajo)
    # =========================
    footer_path = static_path("img", "footer.jpg")
    if os.path.exists(footer_path):
        footer_y = PAGE_H - FOOTER_H - FOOTER_OFFSET
        pdf.image(footer_path, x=0, y=footer_y, w=PAGE_W, h=FOOTER_H)

    return pdf.output(dest="S").encode("latin-1")




@app.route("/medical_record/<int:record_id>/pdf")
@login_required
def download_medical_record_pdf(record_id):
    pdf_content = generate_medical_record_pdf(record_id)
    if not pdf_content:
        flash("No se pudo generar el PDF: Registro médico no encontrado.", "warning")
        return redirect(url_for("home"))

    # Asegurar bytes sí o sí
    if isinstance(pdf_content, str):
        pdf_content = pdf_content.encode("latin-1")
    elif isinstance(pdf_content, bytearray):
        pdf_content = bytes(pdf_content)

    return send_file(
        BytesIO(pdf_content),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"medical_record_{record_id}.pdf",
        max_age=0,  # evita cache
    )



def generate_medical_record_pdf(record_id):
    conn = get_db_connection()
    cur = conn.cursor()
    query = """
        SELECT mr.*,
               p.name AS patient_name,
               p.surname AS patient_surname,
               p.document_number,
               c.name AS company_name,
               c.email AS company_email
        FROM medical_records mr
        JOIN patients p ON mr.patient_id = p.id
        JOIN companies c ON mr.company_id = c.id
        WHERE mr.id = %s
    """
    cur.execute(query, (record_id,))
    record = cur.fetchone()
    cur.close()
    conn.close()

    if not record:
        print(f"No se encontraron datos para el registro {record_id}")
        return None

    return build_pdf_from_record(record)


# ----------------------------
# Email single record
# ----------------------------
def send_medical_record_email(record_id):
    conn = get_db_connection()
    cur = conn.cursor()
    query = """
        SELECT mr.id as record_id,
               mr.date as record_date,
               p.name as patient_name,
               p.surname as patient_surname,
               p.document_number,
               c.name as company_name,
               c.email as company_email
        FROM medical_records mr
        JOIN patients p ON mr.patient_id = p.id
        JOIN companies c ON mr.company_id = c.id
        WHERE mr.id = %s
    """
    cur.execute(query, (record_id,))
    data = cur.fetchone()
    cur.close()
    conn.close()

    if not data or not data.get("company_email"):
        print(f"Correo no enviado: datos no encontrados o correo de empresa faltante para registro {record_id}.")
        return False

    pdf_content = generate_medical_record_pdf(record_id)
    if not pdf_content:
        print(f"Correo no enviado: falló la generación de PDF para registro {record_id}.")
        return False

    recipients = [e.strip() for e in (data["company_email"] or "").split(",") if e.strip()]

    msg = MIMEMultipart()
    msg["Subject"] = f"Informe Médico del Paciente: {data['patient_name']} {data['patient_surname']} ({data['record_date']})"
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)

    body = (
        f"Estimado/a {data['company_name']},\n\n"
        f"Adjunto encontrará el informe médico del paciente {data['patient_name']} {data['patient_surname']}, "
        f"atendido el {data['record_date']}.\n\n"
        "Saludos cordiales,\nDr. Juan Pablo Moya"
    )
    msg.attach(MIMEText(body, "plain"))

    pdf_attachment = MIMEApplication(pdf_content, _subtype="pdf")
    pdf_attachment.add_header(
        "Content-Disposition",
        "attachment",
        filename=f"informe_medico_{data['document_number']}_{data['record_date']}.pdf",
    )
    msg.attach(pdf_attachment)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"Correo para registro {record_id} enviado exitosamente a {data['company_email']}.")
        return True
    except Exception as e:
        print(f"Error al enviar correo para registro {record_id}: {e}")
        return False


@app.route("/medical_record/<int:record_id>/send_email", methods=["POST"])
@login_required
def email_medical_record(record_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.document_number, c.email as company_email, c.name as company_name
        FROM medical_records mr
        JOIN patients p ON mr.patient_id = p.id
        JOIN companies c ON mr.company_id = c.id
        WHERE mr.id = %s
        """,
        (record_id,),
    )
    info = cur.fetchone()
    cur.close()
    conn.close()

    patient_document_number = info["document_number"] if info else None
    company_email_display = info["company_email"] if info and info.get("company_email") else None
    company_name_display = info["company_name"] if info and info.get("company_name") else "la empresa (nombre no encontrado)"

    if not company_email_display:
        flash(f"No se pudo enviar el correo: Email de la empresa '{company_name_display}' no encontrado para el registro {record_id}.", "danger")
        return redirect(url_for("home", search_patient_document=patient_document_number or ""))

    success = send_medical_record_email(record_id)
    if success:
        flash(f"Correo enviado exitosamente a {company_email_display} ({company_name_display}).", "success")
    else:
        flash(f"Error al enviar el correo a {company_email_display} ({company_name_display}). Verifique la configuración y el log del servidor.", "danger")

    return redirect(url_for("home", search_patient_document=patient_document_number or ""))


# ----------------------------
# Daily send (select companies + send grouped PDFs)
# ----------------------------
@app.route("/send_daily_reports/select_companies", methods=["GET", "POST"])
@login_required
def select_companies_for_daily_send():
    conn = get_db_connection()
    cur = conn.cursor()

    today = datetime.date.today()
    cur.execute(
        """
        SELECT DISTINCT c.id, c.name, c.email
        FROM medical_records mr
        JOIN companies c ON mr.company_id = c.id
        WHERE DATE(mr.created_at) = %s
        """,
        (today,),
    )
    companies = cur.fetchall()

    cur.close()
    conn.close()

    return render_template("select_companies_send.html", companies=companies, today=today)


@app.route("/send_daily_reports", methods=["POST"])
@login_required
def send_daily_reports():
    selected_company_ids = request.form.getlist("company_ids")
    if not selected_company_ids:
        flash("No se seleccionó ninguna empresa.", "warning")
        return redirect(url_for("select_companies_for_daily_send"))

    today = datetime.date.today()

    conn = get_db_connection()
    cur = conn.cursor()

    placeholders = ",".join(["%s"] * len(selected_company_ids))
    query = f"""
    SELECT mr.id, c.email as company_email, c.name as company_name
    FROM medical_records mr
    JOIN companies c ON mr.company_id = c.id
    WHERE mr.company_id IN ({placeholders}) AND DATE(mr.created_at) = %s
    """
    params = [*selected_company_ids, today]

    cur.execute(query, params)
    records = cur.fetchall()

    cur.close()
    conn.close()

    grouped_records = defaultdict(list)
    for r in records:
        grouped_records[(r["company_email"], r["company_name"])].append(r["id"])

    for (email, name), record_ids in grouped_records.items():
        attachments = []
        for rid in record_ids:
            pdf = generate_medical_record_pdf(rid)
            if pdf:
                attachments.append((f"registro_{rid}.pdf", pdf))

        if attachments:
            success = send_multiple_pdfs_email(email, name, attachments)
            if success:
                flash(f"Correo enviado a {email} ({name}) con {len(attachments)} registros.", "success")
            else:
                flash(f"Error al enviar correo a {email}.", "danger")

    return redirect(url_for("home"))


def send_multiple_pdfs_email(to_email, company_name, attachments):
    recipients = [e.strip() for e in (to_email or "").split(",") if e.strip()]
    if not recipients:
        return False

    msg = MIMEMultipart()
    msg["Subject"] = f"Informes Médicos del Día - {company_name}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)

    body = (
        f"Estimado/a {company_name},\n\n"
        "Adjunto encontrará los informes médicos correspondientes al día de hoy.\n\n"
        "Saludos cordiales,\nDr. Juan Pablo Moya"
    )
    msg.attach(MIMEText(body, "plain"))

    for filename, content in attachments:
        part = MIMEApplication(content, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        return True
    except Exception as e:
        print(f"Error al enviar correo a {recipients}: {e}")
        return False


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
