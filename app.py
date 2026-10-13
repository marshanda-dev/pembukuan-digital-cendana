import base64
import json
import os
import re

import streamlit as st
import pandas as pd
from fpdf import FPDF
from PIL import Image
 

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
FALLBACK_GEMINI_MODEL = "gemini-3.1-flash-lite"


def get_gemini_settings():
    """Ambil API key & nama model dari environment atau .streamlit/secrets.toml."""
    api_key = os.environ.get("GEMINI_API_KEY")
    model_name = os.environ.get("GEMINI_MODEL")
    try:
        api_key = api_key or st.secrets.get("GEMINI_API_KEY")
        model_name = model_name or st.secrets.get("GEMINI_MODEL")
    except Exception:
        pass
    return (api_key or "").strip(), (model_name or DEFAULT_GEMINI_MODEL).strip()


def load_workshop_fallback():
    """Load the local complaint and workshop catalog used when Gemini is unavailable."""
    dataset_path = os.path.join(os.path.dirname(__file__), "data", "workshop_fallback.json")
    try:
        with open(dataset_path, "r", encoding="utf-8") as dataset_file:
            return json.load(dataset_file)
    except (OSError, json.JSONDecodeError):
        return {"complaints": [], "catalog_template": [], "pricing_policy": {}}


def match_fallback_complaints(text, dataset):
    normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    tokens = set(normalized.split())
    colloquial_terms = {
        "ga": "tidak", "gak": "tidak", "enggak": "tidak", "nggak": "tidak",
        "nyala": "hidup", "nyalain": "hidup", "starter": "starter", "distarter": "starter",
        "rem": "rem", "ngerem": "rem", "ngerem": "rem",
        "panas": "overheat", "mendidih": "overheat", "tekor": "aki", "soak": "aki",
        "bunyi": "bunyi", "berisik": "bunyi", "decit": "bunyi", "gredek": "bunyi",
        "dingin": "ac", "adem": "ac", "transmisi": "transmisi", "matic": "transmisi",
        "oli": "oli", "netes": "bocor", "rembes": "bocor", "kempes": "ban", "bocor": "bocor",
        "check": "engine", "indikator": "engine", "lampu": "engine"
    }
    expanded = set(tokens)
    for token in tokens:
        if token in colloquial_terms:
            expanded.add(colloquial_terms[token])
    matches = []
    for complaint in dataset.get("complaints", []):
        keywords = complaint.get("keywords", [])
        keyword_match = any(re.sub(r"[^a-z0-9]+", " ", keyword.lower()).strip() in normalized for keyword in keywords)
        # Allow common short, informal descriptions to match the same complaint family.
        family_terms = {
            "ENG-NO-START": {"starter", "hidup", "nyala", "start"},
            "BRK-NOISE": {"rem", "bunyi", "ngerem"},
            "AC-NO-COOL": {"ac", "dingin", "adem", "panas"},
            "SUS-NOISE": {"suspensi", "kaki", "bunyi", "gredek", "getar"},
            "ENG-OVERHEAT": {"overheat", "temperatur", "panas", "radiator"},
            "ENG-OIL-LEAK": {"oli", "bocor", "rembes", "netes"},
            "ELEC-BATTERY": {"aki", "tekor", "soak", "starter", "redup"},
            "TRANS-SLIP": {"transmisi", "gear", "gigi", "matic", "selip", "hentak"},
            "TIRE-PUNCTURE": {"ban", "kempes", "bocor", "kempis"},
            "ENG-CHECK-LIGHT": {"engine", "mesin", "indikator", "obd"},
        }
        family = family_terms.get(complaint.get("id"), set())
        distinctive_terms = {
            "ENG-NO-START": {"starter", "start", "mogok", "distarter"},
            "BRK-NOISE": {"ngerem", "rem"},
            "AC-NO-COOL": {"ac"},
            "SUS-NOISE": {"suspensi", "kaki", "gluduk", "gredek"},
            "ENG-OVERHEAT": {"overheat", "temperatur", "radiator"},
            "ENG-OIL-LEAK": {"oli", "rembes"},
            "ELEC-BATTERY": {"aki", "tekor", "soak"},
            "TRANS-SLIP": {"transmisi", "gear", "gigi", "matic", "selip"},
            "TIRE-PUNCTURE": {"ban", "kempes", "kempis"},
            "ENG-CHECK-LIGHT": {"obd", "engine"},
        }
        family_match = (
            len(expanded.intersection(family)) >= 2
            or bool(expanded.intersection(distinctive_terms.get(complaint.get("id"), set())))
        )
        if keyword_match or family_match:
            matches.append(complaint)
    return matches


def friendly_complaint_text(text, matches):
    """Make a readable, conversational customer-facing summary without adding a diagnosis."""
    if matches:
        labels = [item.get("name", "") .lower() for item in matches if item.get("name")]
        label_text = ", ".join(labels)
        return f"Keluhannya, {label_text}. Nanti kita cek dulu penyebabnya supaya penanganannya tepat."
    original = re.sub(r"\s+", " ", (text or "").strip())
    if not original:
        return "Gejalanya belum dijelaskan. Kita perlu tanya detailnya dulu sebelum membuat perkiraan."
    return f"Keluhannya: {original[0].lower() + original[1:]}. Nanti kita cek dulu penyebabnya, ya."


def fallback_estimate_rows(matches, dataset):
    """Baris estimasi dari dataset lokal: [nama, qty, harga perkiraan (nilai tengah), jenis]."""
    rows = []
    for complaint in matches:
        for item in complaint.get("candidate_items", []):
            price = item.get("price_idr")
            if price is None:
                continue
            if isinstance(price, dict):
                low = int(price.get("min", 0) or 0)
                high = int(price.get("max", low) or low)
                mid = (low + max(low, high)) / 2
            else:
                mid = int(price)
            mid = int(round(mid / 1000.0) * 1000) if mid >= 1000 else int(mid)
            rows.append([
                item.get("label", "Komponen/jasa"),
                1,
                mid,
                "Jasa" if item.get("type") == "service" else "Suku cadang",
            ])
    if not rows:
        rows.append(["Pemeriksaan awal dan diagnosis keluhan", 1, 200000, "Jasa"])
    return rows


def build_estimate_pdf(estimate, rows_df, discount=0):
    """Buat PDF Form Penawaran Service Kendaraan (kop surat + logo + ttd manajer). Mengembalikan bytes."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    L, W = 15, 180

    def safe(value):
        return str(value if value is not None else "").encode("latin-1", errors="replace").decode("latin-1")

    def rupiah(value):
        return f"{int(round(float(value))):,}".replace(",", ".")

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(L, 10, L)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def wrap(text, max_width):
        lines, current = [], ""
        for word in text.split():
            while pdf.get_string_width(word) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and pdf.get_string_width(word[:cut]) > max_width:
                    cut -= 1
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:cut])
                word = word[cut:]
            candidate = (current + " " + word).strip()
            if pdf.get_string_width(candidate) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    # ---------- KOP SURAT ----------
    logo_path = os.path.join(base_dir, "logo.png")
    if os.path.exists(logo_path):
        pdf.image(logo_path, 15, 10, 24)
    pdf.set_xy(42, 11)
    pdf.set_font("Arial", "B", 13)
    pdf.cell(153, 6, "BENGKEL CENDANA MOTOR GUNUNG SINDUR", 0, 1, "C")
    pdf.set_font("Arial", "", 8)
    for line in [
        "Perumahan Al Falah 4 Blok H No. 13",
        "Kel. Rawakalong, Kec. Gunung Sindur Kab. Bogor 16340",
        "Telp: 082125074809 | NIB: 3110250100276 | NPWP: 1000000006562499",
        "Email: cendanamotovision@gmail.com",
    ]:
        pdf.set_x(42)
        pdf.cell(153, 4.5, line, 0, 1, "C")
    pdf.set_line_width(0.7)
    pdf.line(L, 37, L + W, 37)
    pdf.set_line_width(0.2)
    pdf.line(L, 38.2, L + W, 38.2)
    pdf.set_y(42)

    pdf.set_x(L)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(W, 7, "FORM ESTIMASI SERVICE KENDARAAN", 0, 1, "C")
    pdf.ln(2)

    # ---------- DATA PELANGGAN ----------
    odometer = estimate.get("odometer") or 0
    info = [
        ("Nama Pengguna", estimate.get("customer_name", "-"), "Tanggal", estimate.get("estimate_date", "-")),
        ("Jenis Kendaraan", estimate.get("vehicle", "-"), "No Polisi", estimate.get("plate_number", "-")),
        ("Kilometer", f"{int(odometer):,} km".replace(",", ".") if odometer else "-", "", ""),
    ]
    for label1, value1, label2, value2 in info:
        pdf.set_x(L)
        pdf.set_font("Arial", "B", 9)
        pdf.cell(32, 6, label1 + " :", 0, 0)
        pdf.set_font("Arial", "", 9)
        pdf.cell(66, 6, safe(value1), 0, 0)
        pdf.set_font("Arial", "B", 9)
        pdf.cell(20, 6, (label2 + " :") if label2 else "", 0, 0)
        pdf.set_font("Arial", "", 9)
        pdf.cell(62, 6, safe(value2), 0, 1)
    complaint_text = safe(estimate.get("complaint", "")).strip()
    if complaint_text:
        pdf.set_x(L)
        pdf.set_font("Arial", "B", 9)
        pdf.cell(32, 6, "Keluhan :", 0, 0)
        pdf.set_font("Arial", "", 9)
        pdf.multi_cell(148, 5.5, complaint_text[:300])
        pdf.set_x(L)
    pdf.ln(3)

    # ---------- TABEL ESTIMASI ----------
    def table_header():
        pdf.set_x(L)
        pdf.set_font("Arial", "B", 9)
        pdf.set_fill_color(235, 235, 235)
        pdf.cell(12, 7, "NO", 1, 0, "C", True)
        pdf.cell(118, 7, "JENIS SERVICE", 1, 0, "C", True)
        pdf.cell(50, 7, "ESTIMASI BIAYA", 1, 1, "C", True)

    table_header()
    pdf.set_font("Arial", "", 9)
    total = 0.0
    for number, (_, row) in enumerate(rows_df.iterrows(), start=1):
        qty = float(row["Jumlah"])
        price = float(row["Harga perkiraan pasar (Rp)"])
        subtotal = qty * price
        total += subtotal
        name = str(row["Komponen / jasa"]).strip()
        label = safe(name + (f" (x{qty:g})" if qty != 1 else ""))
        lines = wrap(label, 118 - 6)
        row_h = max(7, 5 * len(lines) + 2)
        if pdf.get_y() + row_h > 255:
            pdf.add_page()
            table_header()
            pdf.set_font("Arial", "", 9)
        y = pdf.get_y()
        pdf.rect(L, y, 12, row_h)
        pdf.rect(L + 12, y, 118, row_h)
        pdf.rect(L + 130, y, 50, row_h)
        pdf.set_xy(L, y)
        pdf.cell(12, row_h, str(number), 0, 0, "C")
        for k, text_line in enumerate(lines):
            pdf.set_xy(L + 15, y + 1 + k * 5)
            pdf.cell(112, 5, text_line, 0, 0, "L")
        pdf.set_xy(L + 130, y)
        pdf.cell(50, row_h, "Rp  " + rupiah(subtotal) + "  ", 0, 0, "R")
        pdf.set_y(y + row_h)
        pdf.set_x(L)

    discount = max(0.0, min(float(discount or 0), total))
    for label, value in [
        ("TOTAL KESELURUHAN BIAYA", total),
        ("DISKON SPESIAL / SUBSIDI BIAYA", discount),
        ("TOTAL BIAYA YANG DIKELUARKAN", total - discount),
    ]:
        if pdf.get_y() + 7 > 270:
            pdf.add_page()
        pdf.set_x(L)
        pdf.set_font("Arial", "B", 9)
        pdf.cell(130, 7, label + "  :  ", 1, 0, "R")
        pdf.cell(50, 7, "Rp  " + rupiah(value) + "  ", 1, 1, "R")

    # ---------- KETENTUAN (singkat) ----------
    if pdf.get_y() > 215:
        pdf.add_page()
    pdf.ln(5)
    pdf.set_x(L)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(W, 6, "Keterangan Pengerjaan dan Garansi", 0, 1)
    pdf.set_font("Arial", "", 8.5)
    terms = [
        "Harga di atas hanya estimasi perkiraan dan dapat berubah sesuai harga pada bon pembayaran.",
        "Barang/sparepart dari bengkel mendapat garansi selama 1 (satu) bulan sejak pekerjaan selesai.",
        f"Perkiraan waktu pengerjaan: {estimate.get('work_duration') or 'dikonfirmasi setelah pemeriksaan'}. Pekerjaan dimulai setelah estimasi disetujui.",
    ]
    for number, term in enumerate(terms, start=1):
        pdf.set_x(L + 2)
        pdf.multi_cell(W - 2, 5, safe(f"{number}. {term}"))
        pdf.set_x(L)

    # ---------- TANDA TANGAN MANAJER ----------
    if pdf.get_y() > 232:
        pdf.add_page()
    pdf.ln(6)
    y0 = pdf.get_y()
    pdf.set_xy(25, y0)
    pdf.set_font("Arial", "B", 9)
    pdf.cell(60, 5, "Hormat kami,", 0, 0, "C")
    sign_path = os.path.join(base_dir, "ttd_manajer.png")
    if os.path.exists(sign_path):
        pdf.image(sign_path, 38, y0 + 6, 34)
    pdf.line(25, y0 + 36, 85, y0 + 36)
    pdf.set_xy(25, y0 + 37)
    pdf.cell(60, 5, "ADM CENDANA MOTOR", 0, 1, "C")

    output = pdf.output()
    if isinstance(output, str):
        output = output.encode("latin-1", errors="replace")
    return bytes(output)


# --- PENYIMPANAN PERMANEN ---
# Jika DATABASE_URL diisi di .streamlit/secrets.toml -> memakai Postgres online (Supabase), data permanen.
# Jika tidak diisi -> memakai file lokal data/cendana.db (SQLite), cocok untuk komputer sendiri.
import sqlite3

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "cendana.db")
BULAN_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]


def _get_database_url():
    try:
        url = st.secrets.get("DATABASE_URL", "")
    except Exception:
        url = ""
    return (url or os.environ.get("DATABASE_URL", "")).strip()


DATABASE_URL = _get_database_url()
USE_PG = bool(DATABASE_URL)
DB_LABEL = "Supabase (Postgres, permanen)" if USE_PG else "SQLite lokal (data/cendana.db)"


@st.cache_resource
def _pg_connect(url):
    import psycopg2
    conn = psycopg2.connect(url, connect_timeout=15)
    conn.autocommit = True
    return conn


class _Conn:
    """Pembungkus tipis agar kode yang sama bisa jalan di SQLite maupun Postgres."""

    def __init__(self):
        if USE_PG:
            self.raw = _pg_connect(DATABASE_URL)
        else:
            os.makedirs(DB_DIR, exist_ok=True)
            self.raw = sqlite3.connect(DB_PATH)
            self.raw.row_factory = sqlite3.Row

    def _run_pg(self, sql, params):
        import psycopg2
        import psycopg2.extras
        cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), [psycopg2.Binary(p) if isinstance(p, (bytes, bytearray)) else p for p in params])
        return cur

    def execute(self, sql, params=()):
        if not USE_PG:
            return self.raw.execute(sql, params)
        import psycopg2
        try:
            return self._run_pg(sql, params)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # koneksi putus (idle terlalu lama) -> sambung ulang sekali
            _pg_connect.clear()
            self.raw = _pg_connect(DATABASE_URL)
            return self._run_pg(sql, params)

    def commit(self):
        if not USE_PG:
            self.raw.commit()

    def close(self):
        if not USE_PG:
            self.raw.close()


def db_conn():
    return _Conn()


def _b(value):
    return bytes(value) if value is not None else None


def init_db():
    pk = "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    blob = "BYTEA" if USE_PG else "BLOB"
    big = "BIGINT" if USE_PG else "INTEGER"
    statements = [
        f"CREATE TABLE IF NOT EXISTS deposits (id {pk}, tanggal TEXT, nominal {big}, keterangan TEXT, bank_asal TEXT, metode TEXT, bukti_nama TEXT, bukti_blob {blob}, status TEXT)",
        f"CREATE TABLE IF NOT EXISTS expenses (id {pk}, tanggal TEXT, keterangan TEXT, kategori TEXT, nominal {big}, bukti_nama TEXT, bukti_blob {blob})",
        f"CREATE TABLE IF NOT EXISTS payslips (id {pk}, dibuat TEXT, karyawan TEXT, status_pekerja TEXT, periode TEXT, total {big}, nama_file TEXT, pdf_blob {blob}, UNIQUE(karyawan, periode))",
        f"CREATE TABLE IF NOT EXISTS estimates (id {pk}, dibuat TEXT, pelanggan TEXT, kendaraan TEXT, plat TEXT, tgl_estimasi TEXT, total {big}, nama_file TEXT, pdf_blob {blob}, UNIQUE(pelanggan, kendaraan, plat, tgl_estimasi, total))",
    ]
    conn = db_conn()
    try:
        for statement in statements:
            conn.execute(statement)
        if USE_PG:
            # Tutup akses lewat API publik Supabase; aplikasi ini terhubung langsung sebagai pemilik tabel.
            for table in ("deposits", "expenses", "payslips", "estimates"):
                conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        conn.commit()
    finally:
        conn.close()


@st.cache_resource
def init_db_once(backend_key):
    init_db()
    return True


def _now():
    return pd.Timestamp.today().strftime("%Y-%m-%d %H:%M")


def load_deposits():
    conn = db_conn()
    try:
        rows = conn.execute("SELECT * FROM deposits ORDER BY id").fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "Tanggal": r["tanggal"], "Nominal": r["nominal"], "Keterangan": r["keterangan"],
             "Bank Asal": r["bank_asal"], "Metode": r["metode"], "Bukti": r["bukti_nama"],
             "FileObj": _b(r["bukti_blob"]), "Status": r["status"]} for r in rows]


def add_deposit(draft, proof_name, proof_bytes):
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO deposits (tanggal, nominal, keterangan, bank_asal, metode, bukti_nama, bukti_blob, status) VALUES (?,?,?,?,?,?,?,?)",
            (_now(), int(draft["Nominal"]), draft["Keterangan"], draft["Bank Asal"], draft["Metode"], proof_name, proof_bytes, "Sukses (dilaporkan)"),
        )
        conn.commit()
    finally:
        conn.close()


def load_expenses():
    conn = db_conn()
    try:
        rows = conn.execute("SELECT * FROM expenses ORDER BY id").fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "Tanggal": r["tanggal"], "Keterangan": r["keterangan"], "Kategori": r["kategori"],
             "Nominal": r["nominal"], "Bukti": r["bukti_nama"], "FileObj": _b(r["bukti_blob"])} for r in rows]


def add_expense(desc, category, amount, proof_name, proof_bytes):
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO expenses (tanggal, keterangan, kategori, nominal, bukti_nama, bukti_blob) VALUES (?,?,?,?,?,?)",
            (pd.Timestamp.today().strftime("%Y-%m-%d"), desc, category, int(amount), proof_name, proof_bytes),
        )
        conn.commit()
    finally:
        conn.close()


def update_expense(expense_id, tanggal, desc, category, amount):
    conn = db_conn()
    try:
        conn.execute("UPDATE expenses SET tanggal=?, keterangan=?, kategori=?, nominal=? WHERE id=?",
                     (tanggal, desc, category, int(amount), expense_id))
        conn.commit()
    finally:
        conn.close()


def delete_expense(expense_id):
    conn = db_conn()
    try:
        conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        conn.commit()
    finally:
        conn.close()


def get_totals():
    """Saldo = total setoran - total pengeluaran (selalu dihitung dari database)."""
    conn = db_conn()
    try:
        dep = int(conn.execute("SELECT COALESCE(SUM(nominal), 0) AS s FROM deposits").fetchone()["s"])
        exp = int(conn.execute("SELECT COALESCE(SUM(nominal), 0) AS s FROM expenses").fetchone()["s"])
    finally:
        conn.close()
    return {"total_deposit": dep, "total_expense": exp, "balance": dep - exp}


def save_payslip(worker, periode, total, filename, pdf_bytes):
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO payslips (dibuat, karyawan, status_pekerja, periode, total, nama_file, pdf_blob) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (karyawan, periode) DO UPDATE SET dibuat=excluded.dibuat, status_pekerja=excluded.status_pekerja, "
            "total=excluded.total, nama_file=excluded.nama_file, pdf_blob=excluded.pdf_blob",
            (_now(), worker["name"], worker["type"], periode, int(total), filename, pdf_bytes),
        )
        conn.commit()
    finally:
        conn.close()


def save_estimate_history(estimate, total, filename, pdf_bytes):
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO estimates (dibuat, pelanggan, kendaraan, plat, tgl_estimasi, total, nama_file, pdf_blob) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (pelanggan, kendaraan, plat, tgl_estimasi, total) DO UPDATE SET dibuat=excluded.dibuat, "
            "nama_file=excluded.nama_file, pdf_blob=excluded.pdf_blob",
            (_now(), estimate.get("customer_name", "-"), estimate.get("vehicle", "-"), estimate.get("plate_number", "-"),
             estimate.get("estimate_date", ""), int(total), filename, pdf_bytes),
        )
        conn.commit()
    finally:
        conn.close()


def search_history(table, columns, query):
    like_op = "ILIKE" if USE_PG else "LIKE"
    conn = db_conn()
    try:
        if query and query.strip():
            like = f"%{query.strip()}%"
            where = " OR ".join(f"{c} {like_op} ?" for c in columns)
            rows = conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC", [like] * len(columns)).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return rows


def get_pdf(table, row_id):
    conn = db_conn()
    try:
        row = conn.execute(f"SELECT nama_file, pdf_blob FROM {table} WHERE id=?", (row_id,)).fetchone()
    finally:
        conn.close()
    return (row["nama_file"], _b(row["pdf_blob"])) if row else (None, None)


def render_pdf_history(table, columns, label_fn, display_cols, prefix, title, search_hint):
    st.subheader(title)
    query = st.text_input(search_hint, key=f"{prefix}_search")
    rows = search_history(table, columns, query)
    if not rows:
        st.caption("Belum ada riwayat dokumen." if not query else "Tidak ada dokumen yang cocok dengan pencarian.")
        return
    st.dataframe(pd.DataFrame([{label: r[col] for label, col in display_cols.items()} for r in rows]), use_container_width=True)
    chosen = st.selectbox("Pilih dokumen untuk diunduh:", options=[r["id"] for r in rows],
                          format_func=lambda i: label_fn(next(r for r in rows if r["id"] == i)), key=f"{prefix}_select")
    file_name, pdf_bytes = get_pdf(table, chosen)
    if pdf_bytes:
        st.download_button("📥 Unduh PDF dari Riwayat", data=pdf_bytes, file_name=file_name, mime="application/pdf", key=f"{prefix}_dl_{chosen}")
        with st.expander("Pratinjau PDF"):
            b64 = base64.b64encode(pdf_bytes).decode("utf-8")
            st.markdown(f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="600px" type="application/pdf"></iframe>', unsafe_allow_html=True)


def render_payslip_history(prefix):
    render_pdf_history(
        "payslips", ["karyawan", "periode"],
        lambda r: f"{r['karyawan']} - {r['periode']} (Rp {r['total']:,.0f})",
        {"Disimpan": "dibuat", "Karyawan": "karyawan", "Status": "status_pekerja", "Periode": "periode", "Total (Rp)": "total"},
        prefix, "🗂️ Riwayat Slip Gaji", "Cari nama karyawan / periode",
    )


def render_estimate_history(prefix):
    render_pdf_history(
        "estimates", ["pelanggan", "kendaraan", "plat"],
        lambda r: f"{r['tgl_estimasi']} - {r['pelanggan']} - {r['kendaraan']} (Rp {r['total']:,.0f})",
        {"Disimpan": "dibuat", "Pelanggan": "pelanggan", "Kendaraan": "kendaraan", "Plat": "plat", "Total (Rp)": "total"},
        prefix, "🗂️ Riwayat PDF Estimasi Harga", "Cari nama pelanggan / kendaraan / plat",
    )


try:
    init_db_once(DB_LABEL)
except Exception as db_error:
    st.error(f"Tidak bisa terhubung ke database ({DB_LABEL}). Periksa DATABASE_URL di secrets. Detail: {db_error}")
    st.stop()

# Konfigurasi Halaman
st.set_page_config(
    page_title="CV Cendana Motovision - Sistem Manajemen & Gaji",
    page_icon="\u2699\ufe0f",
    layout="wide"
)

# Inisialisasi Session State
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "deposit_draft" not in st.session_state:
    st.session_state.deposit_draft = None

# --- SIDEBAR: LOGIN & NAVIGASI ROLE ---
st.sidebar.title("Cendana Motovision")
st.sidebar.caption(f"Database: {DB_LABEL}")
st.sidebar.markdown("---")

if not st.session_state.logged_in:
    st.sidebar.subheader("Silakan Login")
    role_input = st.sidebar.selectbox("Pilih Peran", ["Manajer Operasional", "Owner"])
    pass_input = st.sidebar.text_input("Password", type="password")
    
    btn_login = st.sidebar.button("Masuk ke Sistem")
    
    if btn_login:
        if role_input == "Manajer Operasional" and pass_input == "manajer123":
            st.session_state.logged_in = True
            st.session_state.role = "Manajer"
            st.rerun()
        elif role_input == "Owner" and pass_input == "owner123":
            st.session_state.logged_in = True
            st.session_state.role = "Owner"
            st.rerun()
        else:
            st.sidebar.error("Password salah! (manajer123 / owner123)")
    
    st.title("Selamat Datang di Sistem Manajemen CV Cendana Motovision")
    st.info("👈 Silakan login melalui panel di sidebar kiri untuk mengakses aplikasi.")

else:
    if st.sidebar.button("Keluar / Logout"):
        st.session_state.logged_in = False
        st.rerun()
        
    st.sidebar.markdown(f"**Login sebagai:** {st.session_state.role}")

    # --- JIKA OWNER MASUK ---
    if st.session_state.role == "Owner":
        st.title("👑 Dashboard Pemilik (Owner)")
        st.markdown("Ringkasan keuangan dan aset bengkel secara real-time yang terhubung langsung dengan laporan Manajer.")
        
        totals = get_totals()
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Saldo SeaBank Bengkel", f"Rp {totals['balance']:,.0f}")
        with col2:
            st.metric("Total Pengeluaran", f"Rp {totals['total_expense']:,.0f}")
        with col3:
            st.metric("Total Akumulasi Setoran Owner", f"Rp {totals['total_deposit']:,.0f}")
        
        st.markdown("---")
        st.subheader("💰 Setor Tabungan / Modal Pemilik (Integrasi Payment SeaBank)")
        
        @st.dialog("Setor Tabungan / Modal")
        def deposit_dialog():
            if st.session_state.deposit_draft is None:
                with st.form("deposit_details_form"):
                    amount = st.number_input("Nominal setoran (Rp)", min_value=1, step=100000, value=1000000)
                    description = st.text_input("Keterangan setoran", value="Tabungan Modal Bulanan")
                    source_bank = st.selectbox("Bank asal", ["SeaBank", "Bank lain"])
                    method = st.selectbox("Metode pembayaran", ["Transfer rekening SeaBank"])
                    if st.form_submit_button("Lanjut ke Pembayaran"):
                        st.session_state.deposit_draft = {"Nominal": amount, "Keterangan": description.strip(), "Bank Asal": source_bank, "Metode": method}
            else:
                draft = st.session_state.deposit_draft
                st.write(f"Transfer **Rp {draft['Nominal']:,.0f}** ke rekening SeaBank berikut:")
                st.info("**901068895526**\n\nA.N. **Marshanda Putri Ekamawarni**")
                st.caption("Lakukan transfer melalui aplikasi bank Anda, lalu unggah screenshot bukti transfer.")
                with st.form("deposit_proof_form"):
                    proof = st.file_uploader("Upload screenshot bukti transfer", type=["jpg", "jpeg", "png"])
                    if st.form_submit_button("Konfirmasi & Bayar"):
                        if proof is None:
                            st.error("Upload screenshot bukti transfer terlebih dahulu.")
                        else:
                            add_deposit(draft, proof.name, proof.getvalue())
                            st.session_state.deposit_draft = None
                            st.success("Setoran berhasil dicatat dan saldo diperbarui.")
                            st.rerun()
                if st.button("Kembali"):
                    st.session_state.deposit_draft = None

        if st.button("Setoran Tabungan", use_container_width=True):
            deposit_dialog()
        st.subheader("Riwayat Setoran")
        deposits = load_deposits()
        if deposits:
            st.dataframe(pd.DataFrame([{k: v for k, v in row.items() if k not in ("FileObj", "id")} for row in deposits]), use_container_width=True)
            for idx, row in enumerate(deposits):
                if row.get("FileObj"):
                    st.download_button(f"Unduh bukti setoran {idx + 1}", bytes(row["FileObj"]), file_name=row["Bukti"], key=f"deposit_proof_{row['id']}")
        else:
            st.caption("Belum ada riwayat setoran.")

        st.markdown("---")
        st.subheader("Riwayat Pengeluaran")
        owner_expenses = load_expenses()
        if owner_expenses:
            table_data = [{k: v for k, v in item.items() if k not in ("FileObj", "id")} for item in owner_expenses]
            df_exp = pd.DataFrame(table_data)
            st.dataframe(df_exp, use_container_width=True)
            
            st.markdown("Detail & Pratinjau Bukti Nota Pengeluaran")
            selected_expense_idx = st.selectbox(
                "Pilih Pengeluaran untuk Melihat/Mengunduh Bukti Nota:",
                options=range(len(owner_expenses)),
                format_func=lambda i: f"{owner_expenses[i]['Tanggal']} - {owner_expenses[i]['Keterangan']} (Rp {owner_expenses[i]['Nominal']:,.0f})"
            )
            
            chosen_exp = owner_expenses[selected_expense_idx]
            st.write(f"**Keterangan:** {chosen_exp['Keterangan']}")
            st.write(f"**Nama File Bukti:** {chosen_exp['Bukti']}")
            
            if chosen_exp.get("FileObj") is not None:
                file_obj = bytes(chosen_exp["FileObj"])
                file_ext = chosen_exp["Bukti"].split(".")[-1].lower()

                @st.dialog("Bukti Pengeluaran")
                def show_expense_proof():
                    if file_ext in ["jpg", "jpeg", "png"]:
                        st.image(file_obj, caption=chosen_exp["Bukti"], use_container_width=True)
                    st.download_button("Unduh bukti", data=file_obj, file_name=chosen_exp["Bukti"], mime=f"image/{file_ext}")

                if st.button("Lihat Bukti", key=f"view_expense_{selected_expense_idx}"):
                    show_expense_proof()
            else:
                st.info("Tidak ada file digital yang diunggah untuk pengeluaran ini.")

        else:
            st.write("Belum ada data pengeluaran.")

        st.markdown("---")
        render_payslip_history("owner_slip")

        st.markdown("---")
        st.subheader("Asisten Estimasi Biaya Bengkel")
        st.caption("Keluhan diringkas dengan bahasa yang lebih santai. Saat Gemini tidak tersedia atau kuotanya habis, sistem memakai dataset lokal sebagai titik awal; harga tetap perlu dikonfirmasi owner.")
        workshop_dataset = load_workshop_fallback()

        def save_local_fallback_estimate():
            matched = match_fallback_complaints(complaint, workshop_dataset)
            friendly_text = friendly_complaint_text(complaint, matched)
            candidate_summary = "Kemungkinan penyebab: " + "; ".join(
                dict.fromkeys(cause for item in matched for cause in item.get("possible_causes", []))
            ) if matched else "Keluhan belum cocok dengan pola dataset. Perlu ditanyakan lebih lanjut dan diperiksa mekanik."
            candidate_checks = "; ".join(
                dict.fromkeys(check for item in matched for check in item.get("recommended_checks", []))
            ) if matched else "Konfirmasi gejala dan lakukan pemeriksaan awal di bengkel."
            source_ids = {
                (item.get("price_idr") or {}).get("source_id")
                for complaint_item in matched
                for item in complaint_item.get("candidate_items", [])
                if isinstance(item.get("price_idr"), dict) and item["price_idr"].get("source_id")
            }
            market_sources = [source for source in workshop_dataset.get("price_sources", []) if source.get("id") in source_ids]
            st.session_state.repair_estimate = {
                "vehicle": f"{vehicle_brand} {vehicle_model} ({int(vehicle_year)})",
                "customer_name": customer_name.strip() or "-",
                "plate_number": plate_number.strip() or "-",
                "odometer": int(odometer),
                "work_duration": work_duration.strip() or "Dikonfirmasi setelah pemeriksaan",
                "warranty": "1 (satu) bulan" or "Sesuai kesepakatan setelah pemeriksaan",
                "estimate_date": pd.Timestamp.today().strftime("%d/%m/%Y"),
                "complaint": complaint or "Keluhan belum diisi",
                "friendly_complaint": friendly_text,
                "summary": candidate_summary,
                "inspection": candidate_checks,
                "treatments": list(dict.fromkeys(treatment for item in matched for treatment in item.get("recommended_treatments", []))) or ["Periksa gejala lebih lanjut sebelum menentukan tindakan dan sparepart."],
                "rows": fallback_estimate_rows(matched, workshop_dataset),
                "sources": market_sources,
                "data_mode": "Dataset lokal (fallback)",
                "matched_complaints": [item.get("name", "") for item in matched],
            }

        with st.form("repair_estimate_form"):
            customer_name = st.text_input("Nama pelanggan / perusahaan", placeholder="Contoh: PT. Cometal")
            vehicle_brand = st.text_input("Merek kendaraan", value="Isuzu")
            vehicle_model = st.text_input("Tipe kendaraan", value="ELF NHR 55")
            vehicle_year = st.number_input("Tahun kendaraan", min_value=1950, max_value=2100, value=2020, step=1)
            plate_number = st.text_input("Nomor polisi", placeholder="Contoh: B 1234 XYZ")
            odometer = st.number_input("Kilometer kendaraan", min_value=0, max_value=3000000, value=0, step=1000)
            complaint = st.text_area("Keluhan pelanggan", placeholder="Jelaskan gejala, kapan terjadi, bunyi/asap, dan kondisi saat muncul")
            work_duration = st.text_input("Perkiraan waktu pengerjaan", value="Dikonfirmasi setelah pemeriksaan")
            support_video = st.file_uploader("Video pendukung keluhan (opsional, maksimal 19 MB)", type=["mp4", "mov", "webm"])
            estimate_submit = st.form_submit_button("Buat Estimasi")
        if estimate_submit:
            api_key, gemini_model = get_gemini_settings()
            if support_video and len(support_video.getvalue()) >= 20 * 1024 * 1024:
                st.error("Video harus berukuran di bawah 20 MB untuk analisis inline.")
            elif not api_key:
                save_local_fallback_estimate()
                st.warning("Gemini API belum diatur. Estimasi dibuat dari kisaran pasar fallback. Owner dapat mengubah harga sebelum PDF diunduh.")
            else:
                try:
                    from google import genai
                    from google.genai import types

                    client = genai.Client(api_key=api_key)
                    prompt = f"""Analisis awal kendaraan dan keluhan berikut di Indonesia. Kendaraan: {vehicle_brand} {vehicle_model} tahun {int(vehicle_year)}. Keluhan pelanggan: {complaint or 'Lihat video.'}. Tulis summary dengan bahasa Indonesia sehari-hari yang santai, sopan, dan mudah dipahami, bukan bahasa teknis kaku. Jangan mengubah gejala atau menyatakan dugaan sebagai diagnosis pasti. Sertakan treatments berupa daftar langkah perbaikan yang mungkin diperlukan dan jelaskan bahwa tindakan final menunggu inspeksi. Buat items terpisah untuk sparepart dan biaya jasa, dengan qty dan SATU harga perkiraan pasar per unit (nilai tengah dari harga pasar yang wajar, bukan rentang). Gunakan Google Search untuk menelusuri harga part yang benar-benar cocok dan jasa bengkel saat ini. Jangan mengarang kecocokan atau harga; bila tidak ditemukan isi 0 dan jelaskan. Video hanya bukti gejala, bukan diagnosis final. Kembalikan JSON saja: {{\"summary\":\"penjelasan santai dan tingkat keyakinan\",\"inspection\":\"pemeriksaan yang disarankan\",\"treatments\":[\"langkah perbaikan\"],\"items\":[{{\"name\":\"sparepart atau jasa\",\"qty\":1,\"price\":0,\"is_labor\":false}}]}}. Harga dalam rupiah per unit, berupa satu angka bulat."""
                    contents = [prompt]
                    if support_video:
                        contents.append(types.Part(inline_data=types.Blob(data=support_video.getvalue(), mime_type=support_video.type)))
                    # Urutan percobaan: model utama + Google Search -> model utama tanpa search -> model lite
                    attempts = [(gemini_model, True), (gemini_model, False)]
                    if gemini_model != FALLBACK_GEMINI_MODEL:
                        attempts.append((FALLBACK_GEMINI_MODEL, False))
                    response, used_search, attempt_errors = None, False, []
                    with st.spinner("Gemini menganalisis keluhan dan menelusuri harga pasar..."):
                        for attempt_model, use_search in attempts:
                            try:
                                config = types.GenerateContentConfig(
                                    tools=[types.Tool(google_search=types.GoogleSearch())] if use_search else None
                                )
                                response = client.models.generate_content(model=attempt_model, contents=contents, config=config)
                                used_search = use_search
                                break
                            except Exception as attempt_exc:
                                attempt_errors.append(f"{attempt_model} (search={'ya' if use_search else 'tidak'}): {str(attempt_exc)[:300]}")
                    if response is None:
                        raise RuntimeError(" | ".join(attempt_errors))
                    response_text = (response.text or "").strip()
                    start, end = response_text.find("{"), response_text.rfind("}")
                    if start == -1 or end == -1:
                        raise ValueError("Respons Gemini bukan JSON yang valid.")
                    result = json.loads(response_text[start:end + 1])
                    estimate_rows = []
                    for item in result.get("items", []):
                        if item.get("price") is not None:
                            price = int(float(item.get("price") or 0))
                        else:
                            low = int(float(item.get("price_min", 0) or 0))
                            high = int(float(item.get("price_max", low) or low))
                            price = (low + high) // 2
                        estimate_rows.append([str(item.get("name", "Komponen/jasa")), max(1, int(float(item.get("qty", 1) or 1))), max(0, price), "Jasa" if item.get("is_labor") else "Suku cadang"])
                    if not estimate_rows:
                        raise ValueError("Gemini tidak mengembalikan daftar estimasi.")
                    sources = []
                    for candidate in getattr(response, "candidates", []) or []:
                        metadata = getattr(candidate, "grounding_metadata", None)
                        for chunk in getattr(metadata, "grounding_chunks", []) or []:
                            web = getattr(chunk, "web", None)
                            if web and getattr(web, "uri", None):
                                sources.append({"title": getattr(web, "title", web.uri), "url": web.uri})
                    matched = match_fallback_complaints(complaint, workshop_dataset)
                    st.session_state.repair_estimate = {"vehicle": f"{vehicle_brand} {vehicle_model} ({int(vehicle_year)})", "customer_name": customer_name.strip() or "-", "plate_number": plate_number.strip() or "-", "odometer": int(odometer), "work_duration": work_duration.strip(), "warranty": "1 (satu) bulan", "estimate_date": pd.Timestamp.today().strftime("%d/%m/%Y"), "complaint": complaint or "Analisis video pendukung", "friendly_complaint": friendly_complaint_text(complaint, matched), "summary": result.get("summary", ""), "inspection": result.get("inspection", ""), "treatments": result.get("treatments", ["Konfirmasi tindakan setelah inspeksi langsung."]), "rows": estimate_rows, "sources": sources, "data_mode": "Gemini + pencarian web" if used_search else "Gemini (tanpa pencarian web, harga perkiraan model)"}
                except ImportError:
                    st.error("Pustaka google-genai belum terpasang. Pasang dependensi dari requirements.txt.")
                except Exception as exc:
                    error_text = str(exc)
                    if "RESOURCE_EXHAUSTED" in error_text or "429" in error_text:
                        save_local_fallback_estimate()
                        st.warning("Kuota Gemini sedang habis. Sistem beralih ke kisaran harga pasar fallback. Owner dapat mengubah rincian sebelum mengunduh PDF.")
                        st.caption(f"Detail error asli dari Google: {error_text[:600]}")
                    else:
                        save_local_fallback_estimate()
                        st.warning(f"Gemini tidak dapat digunakan ({error_text}). Sistem memakai kisaran harga fallback. Owner dapat mengubah rincian sebelum mengunduh PDF.")

        if st.session_state.get("repair_estimate"):
            estimate = st.session_state.repair_estimate
            st.write(f"**Kendaraan:** {estimate['vehicle']}  |  **Keluhan asli:** {estimate['complaint']}")
            if estimate.get("friendly_complaint"):
                st.info(f"**Ringkasan untuk pelanggan:** {estimate['friendly_complaint']}")
            st.caption(f"Sumber analisis: {estimate.get('data_mode', 'Tidak diketahui')}")
            if estimate.get("data_mode", "").startswith("Dataset lokal"):
                st.warning("Harga fallback adalah kisaran pasar umum, belum tentu cocok untuk tipe/nomor part kendaraan ini. Owner dapat mengubahnya setelah mengecek kecocokan part.")
            if estimate.get("summary"):
                st.info(f"**Analisis awal:** {estimate['summary']}\n\n**Pemeriksaan lanjutan:** {estimate.get('inspection', 'Periksa langsung di bengkel.')}")
            st.markdown("**Treatment / langkah perbaikan yang disarankan:**")
            for treatment in estimate.get("treatments", []):
                st.markdown(f"- {treatment}")
            st.caption("Sparepart dan jasa ditampilkan sebagai baris terpisah di tabel. Pastikan kecocokan part dan tindakan setelah pemeriksaan mekanik.")
            estimate_df = pd.DataFrame(estimate["rows"], columns=["Komponen / jasa", "Jumlah", "Harga perkiraan pasar (Rp)", "Jenis"])
            editor_key = f"repair_estimate_editor_{abs(hash((estimate['vehicle'], estimate['complaint'])))}"
            st.caption("Harga adalah perkiraan pasar per unit. Owner bisa mengubah nama, jumlah, dan harga langsung di tabel.")
            edited_estimate = st.data_editor(
                estimate_df,
                num_rows="dynamic",
                use_container_width=True,
                key=editor_key,
                column_config={
                    "Harga perkiraan pasar (Rp)": st.column_config.NumberColumn("Harga perkiraan pasar (Rp)", min_value=0, step=1000, format="%d"),
                    "Jumlah": st.column_config.NumberColumn("Jumlah", min_value=0, step=1, format="%d"),
                    "Jenis": st.column_config.SelectboxColumn("Jenis", options=["Suku cadang", "Jasa"]),
                },
            )
            edited_estimate = edited_estimate.dropna(subset=["Komponen / jasa"]).copy()
            edited_estimate["Komponen / jasa"] = edited_estimate["Komponen / jasa"].astype(str).str.strip()
            edited_estimate = edited_estimate[edited_estimate["Komponen / jasa"] != ""]
            for numeric_column in ["Jumlah", "Harga perkiraan pasar (Rp)"]:
                edited_estimate[numeric_column] = pd.to_numeric(edited_estimate[numeric_column], errors="coerce").fillna(0)
            edited_estimate = edited_estimate[edited_estimate["Jumlah"] > 0]
            total_estimate = int((edited_estimate["Jumlah"] * edited_estimate["Harga perkiraan pasar (Rp)"]).sum())
            discount_value = st.number_input("Diskon spesial / subsidi biaya (Rp)", min_value=0, step=10000, value=0, key=editor_key + "_diskon")
            discount_value = min(int(discount_value), total_estimate)
            metric_a, metric_b = st.columns(2)
            metric_a.metric("Total keseluruhan biaya", f"Rp {total_estimate:,.0f}".replace(",", "."))
            metric_b.metric("Total biaya yang dikeluarkan", f"Rp {total_estimate - discount_value:,.0f}".replace(",", "."))
            if estimate.get("sources"):
                source_heading = "Referensi kisaran harga pasar:" if estimate.get("data_mode", "").startswith("Dataset lokal") else "Referensi harga dari Google Search:"
                st.markdown(f"**{source_heading}**")
                for source in estimate["sources"]:
                    st.markdown(f"- [{source['title']}]({source['url']})")

            st.markdown("---")
            if edited_estimate.empty:
                st.info("Tambahkan minimal satu baris pekerjaan/sparepart di tabel untuk membuat PDF.")
            else:
                try:
                    estimate_pdf = build_estimate_pdf(estimate, edited_estimate, discount_value)
                    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{estimate.get('customer_name', 'Pelanggan')}_{estimate.get('estimate_date', '')}").strip("_")
                    estimate_filename = f"Penawaran_{safe_name or 'Service'}.pdf"
                    st.download_button(
                        "📥 Unduh Form Penawaran (PDF)",
                        data=estimate_pdf,
                        file_name=estimate_filename,
                        mime="application/pdf",
                        on_click=save_estimate_history,
                        args=(estimate, total_estimate - discount_value, estimate_filename, estimate_pdf),
                    )
                    st.caption("PDF otomatis tersimpan di riwayat saat diunduh, jadi bisa dicari lagi nanti.")
                    with st.expander("Pratinjau PDF"):
                        pdf_b64 = base64.b64encode(estimate_pdf).decode("utf-8")
                        st.markdown(f'<iframe src="data:application/pdf;base64,{pdf_b64}" width="100%" height="650px" type="application/pdf"></iframe>', unsafe_allow_html=True)
                except Exception as pdf_error:
                    st.error(f"PDF gagal dibuat: {pdf_error}")

        st.markdown("---")
        render_estimate_history("owner_est")

    # --- JIKA MANAJER MASUK ---
    else:
        st.title("Panel Manajer Operasional Bengkel")
        
        tab1, tab2, tab3, tab4 = st.tabs([
            "Input Absensi & Rekening", 
            "Kalkulator & Unduh Slip Gaji", 
            "💸 Catat Pengeluaran", 
            "📊 Rekapitulasi Laporan"
        ])

        with tab1:
            st.subheader("Form Kehadiran, Kinerja, & Informasi Rekening Karyawan")
            
            with st.form("attendance_form"):
                col_a, col_b = st.columns(2)
                with col_a:
                    worker_name = st.text_input("Nama Karyawan / Anak PKL", value="Budi Santoso")
                    worker_type = st.selectbox("Status Pekerja", ["Karyawan Tetap", "Anak PKL"])
                    work_hours = st.number_input("Total Jam Kerja Sebulan", value=234)
                    overtime_hours = st.number_input("Jam Lembur Malam (Lewat Jam 9)", value=10)
                
                with col_b:
                    bank_name = st.text_input("Nama Bank (Contoh: SeaBank / BCA / Mandiri)", value="SeaBank")
                    bank_account = st.text_input("Nomor Rekening Bank", value="901234567890")
                    late_days = st.number_input("Jumlah Hari Terlambat (Denda Rp 20k/hari)", value=1)
                    permit_days = st.number_input("Jumlah Izin / Libur (Maks 3x gratis)", value=2)
                    kasbon = st.number_input("Kasbon karyawan (Rp; isi 0 jika tidak ada)", min_value=0, step=50000, value=0)

                craft_bonus = st.number_input("Bonus Kerajinan & Pengerjaan (Isi Manual)", value=150000, step=25000)
                
                submitted_att = st.form_submit_button("Simpan Data Absensi & Rekening")
                if submitted_att:
                    st.session_state.current_worker = {
                        "name": worker_name,
                        "type": worker_type,
                        "hours": work_hours,
                        "overtime": overtime_hours,
                        "bank_name": bank_name,
                        "bank_account": bank_account,
                        "late": late_days,
                        "permit": permit_days,
                        "bonus": craft_bonus,
                        "kasbon": kasbon
                    }
                    st.success("Data absensi & informasi rekening berhasil disimpan! Silakan buka tab 'Kalkulator & Unduh Slip Gaji'.")

        with tab2:
            st.subheader("Kalkulator Gaji & Cetak Slip Gaji PDF")
            
            # Cek apakah data absensi sudah pernah disimpan
            if "current_worker" not in st.session_state:
                st.warning("?? Silakan isi dan simpan data absensi pada tab pertama (Input Absensi & Rekening) terlebih dahulu.")
            else:
                # Variabel 'd' diambil dengan aman di sini
                d = st.session_state.current_worker
                st.write(f"Karyawan: **{d['name']}** ({d['type']}) | Bank: **{d['bank_name']} - {d['bank_account']}**")
                
                total_pay = 0
                slip_items = []
                _today = pd.Timestamp.today()
                periode_slip = f"{BULAN_ID[_today.month - 1]} {_today.year}"

                if d['type'] == "Karyawan Tetap":
                    base_salary = 2000000
                    ot_pay = d['overtime'] * 15000
                    late_penalty = d['late'] * 20000
                    
                    permit_penalty = 0
                    if d['permit'] > 3:
                        excess = d['permit'] - 3
                        permit_penalty = excess * 20000
                        
                    total_pay = base_salary + ot_pay + d['bonus'] - late_penalty - permit_penalty - d.get('kasbon', 0)
                    slip_items = [
                        ("Gaji Pokok (234 Jam)", 2000000),
                        (f"Lembur Malam ({d['overtime']} Jam)", ot_pay),
                        ("Bonus Kerajinan (Manual)", d['bonus']),
                        (f"Potongan Terlambat ({d['late']} Hari)", -late_penalty),
                        (f"Potongan Kelebihan Izin ({d['permit']} Izin)", -permit_penalty),
                        ("Potongan Kasbon", -d.get("kasbon", 0))
                    ]
                else:
                    rate_per_hour = 15000
                    total_pay = (d['hours'] * rate_per_hour) + d['bonus'] - d.get('kasbon', 0)

                    slip_items = [
                        (f"Upah Jam Kerja ({d['hours']} Jam)", d['hours'] * rate_per_hour),
                        ("Bonus Kerajinan / Tambahan (Manual)", d['bonus']),
                        ("Potongan Kasbon", -d.get("kasbon", 0))
                    ]

                st.markdown("---")
        
                # --- FUNGSI PEMBUATAN PDF YANG LEBIH PROFESIONAL & RAPI ---
                def create_pdf():
                    def angka_ke_kata(angka):
                        angka = int(round(angka))
                        kata = [
                            "nol", "satu", "dua", "tiga", "empat", "lima",
                            "enam", "tujuh", "delapan", "sembilan", "sepuluh", "sebelas"
                        ]

                        if angka < 0:
                            return "minus " + angka_ke_kata(-angka)
                        if angka < 12:
                            return kata[angka]
                        if angka < 20:
                            return angka_ke_kata(angka - 10) + " belas"
                        if angka < 100:
                            puluhan, satuan = divmod(angka, 10)
                            hasil = kata[puluhan] + " puluh"
                            return hasil if satuan == 0 else hasil + " " + angka_ke_kata(satuan)
                        if angka < 200:
                            sisa = angka - 100
                            return "seratus" if sisa == 0 else "seratus " + angka_ke_kata(sisa)
                        if angka < 1000:
                            ratusan, sisa = divmod(angka, 100)
                            hasil = kata[ratusan] + " ratus"
                            return hasil if sisa == 0 else hasil + " " + angka_ke_kata(sisa)
                        if angka < 2000:
                            sisa = angka - 1000
                            return "seribu" if sisa == 0 else "seribu " + angka_ke_kata(sisa)
                        if angka < 1_000_000:
                            ribuan, sisa = divmod(angka, 1000)
                            hasil = angka_ke_kata(ribuan) + " ribu"
                            return hasil if sisa == 0 else hasil + " " + angka_ke_kata(sisa)
                        if angka < 1_000_000_000:
                            jutaan, sisa = divmod(angka, 1_000_000)
                            hasil = angka_ke_kata(jutaan) + " juta"
                            return hasil if sisa == 0 else hasil + " " + angka_ke_kata(sisa)
                        miliar, sisa = divmod(angka, 1_000_000_000)
                        hasil = angka_ke_kata(miliar) + " miliar"
                        return hasil if sisa == 0 else hasil + " " + angka_ke_kata(sisa)

                    # Mengatur margin halaman agar rapi (Margin: 15 mm)
                    pdf = FPDF(orientation='P', unit='mm', format='A4')
                    pdf.set_auto_page_break(auto=True, margin=15)
                    pdf.add_page()
                    
                    # --- KOP SURAT ---
                    # Logo Perusahaan (Kiri atas)
                    try:
                        pdf.image("logo.png", 15, 12, 24)
                    except Exception:
                        pass 
                    
                    pdf.set_font("Arial", "B", 13)
                    pdf.cell(0, 6, "CV. CENDANA MOTO VISION", ln=True, align="center")
                    
                    pdf.set_font("Arial", "I", 9)
                    pdf.cell(0, 5, "Pusat layanan, perawatan, dan solusi otomotif.", ln=True, align="center")
                    
                    pdf.set_font("Arial", "", 8)
                    pdf.cell(0, 4, "Perumahan Al Falah 4 Blok H No. 13, Kel. Rawakalong, Kec. Gunung Sindur Kab. Bogor 16340", ln=True, align="center")
                    pdf.cell(0, 4, "Telp: 082125074809 | NIB: 3110250100276 | NPWP: 1000000006562499", ln=True, align="center")
                    pdf.cell(0, 4, "Email: cendanamotovision@gmail.com", ln=True, align="center")
                    
                    # Garis Pemisah Kop Surat (Double line / Professional border)
                    pdf.ln(3)
                    pdf.set_line_width(0.8)
                    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
                    pdf.set_line_width(0.2)
                    pdf.line(15, pdf.get_y() + 1, 195, pdf.get_y() + 1)
                    pdf.ln(8)
                    
                    # Judul Dokumen Slip Gaji
                    pdf.set_font("Arial", "B", 12)
                    pdf.cell(0, 6, "SLIP GAJI / UPAH KARYAWAN", ln=True, align="center")
                    pdf.ln(4)
                    
                    # Informasi Karyawan (Box Ringkas)
                    pdf.set_font("Arial", "", 10)
                    pdf.cell(28, 6, "Nama Karyawan", ln=0)
                    pdf.cell(4, 6, ":", ln=0)
                    pdf.cell(68, 6, d['name'], ln=0)
                    pdf.cell(27, 6, "Status Pekerja", ln=0)
                    pdf.cell(4, 6, ":", ln=0)
                    pdf.cell(49, 6, d['type'], ln=1)
                    pdf.cell(28, 6, "Bank Tujuan", ln=0)
                    pdf.cell(4, 6, ":", ln=0)
                    pdf.cell(68, 6, f"{d['bank_name']} - {d['bank_account']}", ln=0)
                    pdf.cell(27, 6, "Periode", ln=0)
                    pdf.cell(4, 6, ":", ln=0)
                    pdf.cell(49, 6, periode_slip, ln=1)
                    pdf.ln(6)
                    
                    # --- TABEL KOMPONEN GAJI ---
                    pdf.set_font("Arial", "B", 10)
                    pdf.set_fill_color(240, 240, 240)
                    pdf.cell(120, 8, " Komponen Pembayaran", 1, 0, "L", fill=True)
                    pdf.cell(60, 8, "Jumlah (Rp) ", 1, 1, "R", fill=True)
                    
                    pdf.set_font("Arial", "", 10)
                    for desc, amt in slip_items:
                        pdf.cell(120, 8, f" {desc}", 1, 0, "L")
                        pdf.cell(60, 8, f"Rp {amt:,.0f} ", 1, 1, "R")
                        
                    # Baris Total
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(120, 9, " Total Take Home Pay", 1, 0, "L", fill=True)
                    pdf.cell(60, 9, f"Rp {total_pay:,.0f} ", 1, 1, "R", fill=True)
                    
                    pdf.ln(5)
                    pdf.set_font("Arial", "", 8)
                    pdf.multi_cell(
                        0,
                        4,
                        f"Terbilang: {angka_ke_kata(total_pay).capitalize()} rupiah",
                    )
                    pdf.ln(2)
                    pdf.set_font("Arial", "B", 8)
                    pdf.cell(0, 4, "Catatan:", ln=True)
                    pdf.set_font("Arial", "", 8)
                    pdf.multi_cell(
                        0,
                        4,
                        "Slip gaji ini memuat rincian pembayaran untuk periode yang tercantum. "
                        "Mohon periksa nominal dan komponennya. Jika ada ketidaksesuaian, "
                        "silakan sampaikan kepada manajemen. Dokumen ini bersifat pribadi.",
                    )
                    bulan_indonesia = [
                        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                        "Juli", "Agustus", "September", "Oktober", "November", "Desember"
                    ]
                    tanggal_slip = pd.Timestamp.today()
                    teks_tanggal = (
                        f"Bogor, {tanggal_slip.day} "
                        f"{bulan_indonesia[tanggal_slip.month - 1]} {tanggal_slip.year}"
                    )
                    pdf.ln(8)
                    pdf.set_font("Arial", "", 9)
                    pdf.cell(0, 5, teks_tanggal, ln=True, align="R")
                    pdf.ln(8)
                    
                    # --- BAGIAN TANDA TANGAN (TIDAK NABRAK) ---
                    y_start = pdf.get_y()
                    
                    # Kolom Kiri: Dibuat oleh Management Specialist (Marshanda Putri)
                    pdf.set_xy(20, y_start)
                    pdf.set_font("Arial", "", 9)
                    pdf.cell(75, 5, "Dibuat oleh,", 0, 1, "center")
                    
                    # Gambar Tanda Tangan Manajer (diposisikan agar pas di tengah kolom)
                    try:
                        pdf.image("ttd_manajer.png", x=37, y=y_start + 6, w=38)
                    except Exception:
                        pass
                    
                    # Jarak aman teks nama agar tidak tertutup gambar tanda tangan
                    pdf.set_xy(20, y_start + 27)
                    pdf.set_font("Arial", "B", 9)
                    pdf.cell(75, 5, "[ Marshanda Putri ]", 0, 1, "center")
                    pdf.set_font("Arial", "", 9)
                    pdf.set_x(20)
                    pdf.cell(75, 5, "Management Specialist", 0, 1, "center")
                    
                    # Kolom Kanan: Diketahui & Disetujui oleh Owner (Waryo)
                    pdf.set_xy(115, y_start)
                    pdf.set_font("Arial", "", 9)
                    pdf.cell(75, 5, "Diketahui & Disetujui oleh,", 0, 1, "center")
                    
                    # Gambar Tanda Tangan Owner
                    try:
                        pdf.image("ttd_owner.png", x=132, y=y_start + 6, w=38)
                    except Exception:
                        pass
                        
                    pdf.set_xy(115, y_start + 27)
                    pdf.set_font("Arial", "B", 9)
                    pdf.cell(75, 5, "[ Waryo ]", 0, 1, "center")
                    pdf.set_font("Arial", "", 9)
                    pdf.set_x(115)
                    pdf.cell(75, 5, "Owner / Pimpinan Bengkel", 0, 1, "center")
                    
                    return bytes(pdf.output())
        
                # Generate PDF data
                pdf_data = create_pdf()
        
                # --- FITUR PRATINJAU (PREVIEW) DOKUMEN SEBELUM DI-DOWNLOAD ---
                st.markdown("### Pratinjau Slip Gaji (PDF Preview)")
                
                # Membuat embed PDF viewer interaktif di dalam Streamlit
                base64_pdf = base64.b64encode(pdf_data).decode('utf-8')
                pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="550px" type="application/pdf"></iframe>'
                st.markdown(pdf_display, unsafe_allow_html=True)
                
                st.markdown("---")
                
                # Tombol Unduh Dokumen Resmi
                slip_filename = f"Slip_Gaji_{d['name'].replace(' ', '_')}_{periode_slip.replace(' ', '_')}.pdf"
                st.download_button(
                    label="📥 Download Dokumen Slip Gaji Resmi (PDF)",
                    data=pdf_data,
                    file_name=slip_filename,
                    mime="application/pdf",
                    on_click=save_payslip,
                    args=(d, periode_slip, total_pay, slip_filename, pdf_data),
                )
                st.caption("Slip otomatis tersimpan di riwayat saat diunduh.")

            st.markdown("---")
            render_payslip_history("mgr_slip")
        with tab3:
            st.subheader("Catat Pengeluaran & Unggah Bukti Nota Bengkel")
            with st.form("expense_form"):
                exp_desc = st.text_input("Keterangan Pengeluaran (Contoh: Beli Oli / Kunci Pas)")
                exp_amount = st.number_input("Nominal Pengeluaran (Rp)", min_value=0, step=50000, value=150000)
                exp_category = st.selectbox("Kategori", ["Inventaris / Part", "Operasional Bengkel", "Konsumsi"])
                
                uploaded_file = st.file_uploader("Upload Bukti Nota (Format: JPG, PNG, JPEG)", type=["jpg", "png", "jpeg"])
                
                submitted_exp = st.form_submit_button("Simpan Pengeluaran & Potong Saldo SeaBank")
                if submitted_exp:
                    if exp_amount > 0 and exp_desc:
                        file_name = uploaded_file.name if uploaded_file is not None else "Tidak ada file"
                        file_bytes = uploaded_file.getvalue() if uploaded_file is not None else None
                        add_expense(exp_desc, exp_category, exp_amount, file_name, file_bytes)
                        
                        st.success(f"✅ Pengeluaran '{exp_desc}' senilai Rp {exp_amount:,.0f} berhasil dicatat! Data telah terkirim ke Owner dan saldo SeaBank telah dipotong.")
                    else:
                        st.warning("?? Mohon isi keterangan dan nominal pengeluaran dengan benar.")

        with tab4:
            st.subheader("Rekapitulasi Pengeluaran Bengkel")
            mgr_expenses = load_expenses()
            if mgr_expenses:
                table_data = [{k: v for k, v in item.items() if k not in ("FileObj", "id")} for item in mgr_expenses]
                df_all = pd.DataFrame(table_data)
                st.dataframe(df_all, use_container_width=True)
                
                total_keluar = df_all["Nominal"].sum()
                st.info(f"Total Pengeluaran Tercatat: **Rp {total_keluar:,.0f}** | Sisa Saldo SeaBank: **Rp {get_totals()["balance"]:,.0f}**")

                st.markdown("### Aksi Rekapitulasi")
                selected_record_idx = st.selectbox(
                    "Pilih rekap yang ingin diedit atau dihapus:",
                    options=range(len(mgr_expenses)),
                    format_func=lambda i: (
                        f"{mgr_expenses[i]['Tanggal']} - "
                        f"{mgr_expenses[i]['Keterangan']} "
                        f"(Rp {mgr_expenses[i]['Nominal']:,.0f})"
                    ),
                    key=f"selected_expense_action_{len(mgr_expenses)}",
                )
                selected_record = mgr_expenses[selected_record_idx]
                category_options = ["Inventaris / Part", "Operasional Bengkel", "Konsumsi"]
                if selected_record["Kategori"] not in category_options:
                    category_options.append(selected_record["Kategori"])

                with st.form(f"edit_expense_form_{selected_record_idx}"):
                    edit_col1, edit_col2 = st.columns(2)
                    with edit_col1:
                        edited_date = st.text_input("Tanggal", value=str(selected_record["Tanggal"]))
                        edited_desc = st.text_input("Keterangan", value=selected_record["Keterangan"])
                    with edit_col2:
                        edited_category = st.selectbox(
                            "Kategori",
                            category_options,
                            index=category_options.index(selected_record["Kategori"]),
                        )
                        edited_amount = st.number_input(
                            "Nominal Pengeluaran (Rp)",
                            min_value=0,
                            step=50000,
                            value=int(selected_record["Nominal"]),
                        )
                    save_expense_edit = st.form_submit_button("Simpan Perubahan Rekap")

                if save_expense_edit:
                    if edited_date.strip() and edited_desc.strip() and edited_amount > 0:
                        update_expense(selected_record["id"], edited_date.strip(), edited_desc.strip(), edited_category, edited_amount)
                        st.success("Rekap pengeluaran berhasil diperbarui.")
                        st.rerun()
                    else:
                        st.warning("Tanggal, keterangan, dan nominal wajib diisi dengan benar.")

                if st.button("Hapus Rekap yang Dipilih", key=f"delete_expense_{selected_record_idx}"):
                    delete_expense(selected_record["id"])
                    st.rerun()
            else:
                st.write("Belum ada riwayat pengeluaran.")
