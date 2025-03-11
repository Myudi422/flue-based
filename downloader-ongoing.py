from fastapi import FastAPI, HTTPException, Query
import os
import re
import httpx
from time import sleep
import boto3
from botocore.exceptions import ClientError
import magic
import mimetypes
import mysql.connector
from fastapi.middleware.cors import CORSMiddleware
import threading
from functools import lru_cache

# Global variable untuk menyimpan log episode yang gagal diproses
failed_logs = []

def add_failed_log(anime_id, series_slug, ep_id, episode_number, resolusi, error_message):
    """
    Tambahkan atau perbarui log kegagalan proses suatu episode.
    Jika log untuk kombinasi (anime_id, series_slug, ep_id, episode_number, resolusi)
    sudah ada, maka data tersebut akan di-replace dengan yang terbaru.
    """
    key = (anime_id, series_slug, ep_id, episode_number, resolusi)
    for i, log in enumerate(failed_logs):
        existing_key = (
            log.get("anime_id"),
            log.get("series_slug"),
            log.get("ep_id"),
            log.get("episode_number"),
            log.get("resolusi")
        )
        if existing_key == key:
            failed_logs[i] = {
                "anime_id": anime_id,
                "series_slug": series_slug,
                "ep_id": ep_id,
                "episode_number": episode_number,
                "resolusi": resolusi,
                "error": error_message
            }
            break
    else:
        failed_logs.append({
            "anime_id": anime_id,
            "series_slug": series_slug,
            "ep_id": ep_id,
            "episode_number": episode_number,
            "resolusi": resolusi,
            "error": error_message
        })

# Dictionary untuk menyimpan lock per slug
locks = {}

app = FastAPI()

# Konfigurasi CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- KONFIGURASI ---

# Konfigurasi MySQL
MYSQL_CONFIG = {
    "host": "localhost",
    "user": "ccgnimex",      # sesuaikan dengan username MySQL Anda
    "password": "aaaaaaac",  # sesuaikan dengan password MySQL Anda
    "database": "ccgnimex"
}

# Konfigurasi Backblaze B2 (akses S3)
B2_ENDPOINT_URL = 'https://s3.us-east-005.backblazeb2.com'
B2_ACCESS_KEY = '0057ba6d7a5725c0000000002'
B2_SECRET_KEY = 'K005XvUqydtIZQvuNBYCM/UDhXfrWLQ'
BUCKET_NAME = 'ccgnimex'

# Resolusi yang akan diproses
RESOLUTION_LIST = ["480p"]

# API URL dasar
API_INFO_URL = "http://api.flue.my.id:5000/episode/?data={}"
API_VIEW_URL = "http://api.flue.my.id:5000/api/otakudesu/view/?data={}"

# --- UTILITAS LOCK ---

def get_lock(slug):
    """Ambil atau buat lock berdasarkan slug."""
    if slug not in locks:
        locks[slug] = threading.Lock()
    return locks[slug]

# --- FUNGSI DATABASE ---

def get_db_connection():
    """Buat koneksi ke database MySQL."""
    return mysql.connector.connect(**MYSQL_CONFIG)

def fetch_series(latest_only=False):
    """Ambil data series dari tabel 'otakudesu'."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    if latest_only:
        cursor.execute("SELECT anime_id, slug FROM otakudesu ORDER BY anime_id DESC LIMIT 1")
    else:
        cursor.execute("SELECT anime_id, slug FROM otakudesu")
    series = cursor.fetchall()
    cursor.close()
    conn.close()
    return series

def episode_exists(anime_id, episode_number, resolusi):
    conn = get_db_connection()
    cursor = conn.cursor()
    if resolusi == "480p":
        query = ("SELECT COUNT(*) FROM nonton WHERE anime_id = %s AND episode_number = %s AND resolusi IN (%s, %s)")
        cursor.execute(query, (anime_id, episode_number, "480p", "en"))
    else:
        query = ("SELECT COUNT(*) FROM nonton WHERE anime_id = %s AND episode_number = %s AND resolusi = %s")
        cursor.execute(query, (anime_id, episode_number, resolusi))
    (count,) = cursor.fetchone()
    cursor.close()
    conn.close()
    return count > 0


def insert_episode(anime_id, episode_number, title, video_url, resolusi):
    # Mapping langsung: jika resolusi adalah "480p", ubah menjadi "en"
    if resolusi == "480p":
        resolusi = "en"
    conn = get_db_connection()
    cursor = conn.cursor()
    query = ("INSERT INTO nonton (anime_id, episode_number, title, video_url, resolusi) "
             "VALUES (%s, %s, %s, %s, %s)")
    cursor.execute(query, (anime_id, episode_number, title, video_url, resolusi))
    conn.commit()
    cursor.close()
    conn.close()

# --- FUNGSI B2, DOWNLOAD & UPLOAD ---

def initialize_b2_client():
    """Inisialisasi boto3 client untuk Backblaze B2 (S3 API)."""
    client = boto3.client(
        's3',
        endpoint_url=B2_ENDPOINT_URL,
        aws_access_key_id=B2_ACCESS_KEY,
        aws_secret_access_key=B2_SECRET_KEY
    )
    return client

def download_file(url, local_filename, max_retries=3):
    attempt = 0
    while attempt < max_retries:
        try:
            print(f"Mulai mendownload: {url} (attempt {attempt+1})")
            timeout = httpx.Timeout(60.0)
            with httpx.stream("GET", url, timeout=timeout) as response:
                response.raise_for_status()
                with open(local_filename, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            print(f"Download selesai: {local_filename}")
            return  # Sukses, keluar dari fungsi
        except Exception as e:
            print(f"Error saat mendownload {url}: {e}")
            attempt += 1
            sleep_time = 2 ** attempt  # Exponential backoff
            print(f"Mencoba lagi dalam {sleep_time} detik...")
            sleep(sleep_time)
    # Jika sudah mencapai batas retry
    raise Exception(f"Gagal mendownload {url} setelah {max_retries} percobaan")


def upload_to_b2(s3_client, local_filename, remote_filename):
    """
    Upload file ke Backblaze B2 dan kembalikan URL publik file tersebut.
    URL hasil upload: https://file.ccgnimex.my.id/file/ccgnimex/{remote_filename}
    """
    print(f"Mulai mengupload {local_filename} ke B2 sebagai {remote_filename}")
    try:
        content_type, _ = mimetypes.guess_type(local_filename)
        if not content_type:
            content_type = magic.from_file(local_filename, mime=True)
        extra_args = {'ContentType': content_type}
        s3_client.upload_file(local_filename, BUCKET_NAME, remote_filename, ExtraArgs=extra_args)
    except ClientError as e:
        print(f"Error saat upload {local_filename} ke B2: {e}")
        raise

    b2_url = f"https://file.{BUCKET_NAME}.my.id/file/{BUCKET_NAME}/{remote_filename}"
    print(f"Upload selesai, file tersedia di: {b2_url}")
    return b2_url

# --- FUNGSI UTILS ---

def extract_episode_number(title):
    """
    Ekstrak nomor episode dari judul.
    Contoh: "Episode 5" akan menghasilkan 5 (integer).
    Jika tidak ditemukan, kembalikan None.
    """
    match = re.search(r"Episode\s+(\d+)", title, re.IGNORECASE)
    if match:
        return int(match.group(1))
    else:
        return None

# --- PROSES SERIES & EPISODE ---

def process_series(anime_id, series_slug, s3_client):
    """
    1. Ambil info series dari API,
    2. Cek seluruh episode dan identifikasi mana yang belum ada di DB,
    3. Untuk tiap episode yang “kurang”, ambil link download, download & upload,
    4. Dan simpan ke DB.
    """
    info_url = API_INFO_URL.format(series_slug)
    try:
        response = httpx.get(info_url)
        response.raise_for_status()
        info_data = response.json()
    except Exception as e:
        err_msg = f"Error saat mendapatkan info series: {e}"
        print(err_msg)
        add_failed_log(anime_id, series_slug, None, None, "all", err_msg)
        return

    if "data" not in info_data or "data_episode" not in info_data["data"]:
        err_msg = "Format data tidak sesuai untuk series"
        print(f"{err_msg} {series_slug}")
        add_failed_log(anime_id, series_slug, None, None, "all", err_msg)
        return

    episodes = info_data["data"]["data_episode"]

    # --- Cek seluruh episode untuk melihat resolusi yang belum ada di DB ---
    tasks_by_ep = {}
    for ep in episodes:
        ep_id = ep.get("data")
        ep_title = ep.get("judul_episode")
        if not ep_id or not ep_title:
            add_failed_log(anime_id, series_slug, ep_id, None, "N/A", "Episode data atau title kosong")
            continue

        episode_number = extract_episode_number(ep_title)
        if episode_number is None:
            err_msg = f"Tidak dapat mengekstrak nomor episode dari judul: {ep_title}"
            print(err_msg)
            add_failed_log(anime_id, series_slug, ep_id, None, "N/A", err_msg)
            continue

        new_title = f"Episode {episode_number}"
        missing_res = []
        for res in RESOLUTION_LIST:
            if episode_exists(anime_id, episode_number, res):
                print(f"Episode {episode_number} (resolusi {res}) untuk anime_id {anime_id} sudah ada di DB.")
            else:
                missing_res.append(res)
        if missing_res:
            tasks_by_ep[ep_id] = {
                "episode_number": episode_number,
                "new_title": new_title,
                "missing_res": missing_res
            }

    # --- Proses tiap episode yang belum ada ---
    for ep_id, task in tasks_by_ep.items():
        episode_number = task["episode_number"]
        new_title = task["new_title"]
        missing_res_list = task["missing_res"]

        # Ambil link download via API view
        view_url = API_VIEW_URL.format(ep_id)
        try:
            view_resp = httpx.get(view_url)
            view_resp.raise_for_status()
            view_data = view_resp.json()
        except Exception as e:
            err_msg = f"Error mendapatkan view untuk episode {ep_id}: {e}"
            print(err_msg)
            add_failed_log(anime_id, series_slug, ep_id, episode_number, "all", err_msg)
            continue

        try:
            download_links = view_data["data"]["data"]["download_links"]
        except Exception as e:
            err_msg = f"Error parsing download links untuk episode {ep_id}: {e}"
            print(err_msg)
            add_failed_log(anime_id, series_slug, ep_id, episode_number, "all", err_msg)
            continue

        for res in missing_res_list:
            if res not in download_links:
                err_msg = f"Tidak ada link untuk resolusi {res} pada episode {ep_id}."
                print(err_msg)
                add_failed_log(anime_id, series_slug, ep_id, episode_number, res, err_msg)
                continue

            url = download_links[res]
            local_filename = f"{ep_id}_{res}.mp4"
            try:
                download_file(url, local_filename)
            except Exception as e:
                err_msg = f"Gagal mendownload episode {ep_id} resolusi {res}: {e}"
                print(err_msg)
                add_failed_log(anime_id, series_slug, ep_id, episode_number, res, err_msg)
                continue

            remote_filename = f"{series_slug}/{ep_id}_{res}.mp4"
            try:
                video_url = upload_to_b2(s3_client, local_filename, remote_filename)
            except Exception as e:
                err_msg = f"Gagal mengupload episode {ep_id} resolusi {res} ke B2: {e}"
                print(err_msg)
                add_failed_log(anime_id, series_slug, ep_id, episode_number, res, err_msg)
                if os.path.exists(local_filename):
                    os.remove(local_filename)
                continue
            finally:
                if os.path.exists(local_filename):
                    os.remove(local_filename)

            try:
                insert_episode(anime_id, episode_number, new_title, video_url, res)
                print(f"Data episode {episode_number} (resolusi {res}) untuk anime_id {anime_id} telah disimpan.")
            except Exception as e:
                err_msg = f"Error memasukkan data ke DB untuk episode {ep_id} resolusi {res}: {e}"
                print(err_msg)
                add_failed_log(anime_id, series_slug, ep_id, episode_number, res, err_msg)
            sleep(1)
        sleep(2)

# --- PENGECEKAN OTOMATIS (BACKGROUND) ---

def background_checker():
    """
    Fungsi yang berjalan terus-menerus untuk:
      - Mengambil seluruh series,
      - Membersihkan log error di awal siklus pengecekan,
      - Memproses tiap series untuk mendownload hanya episode/resolusi yang belum ada.
    """
    s3_client = initialize_b2_client()
    global failed_logs  # pastikan kita menggunakan variable global
    while True:
        print("\n[Background Checker] Mulai pengecekan semua series...")
        # Bersihkan log error sebelum mulai pengecekan baru
        failed_logs.clear()
        series_list = fetch_series()
        for series in series_list:
            anime_id = series["anime_id"]
            series_slug = series["slug"]
            print(f"\nMemproses series: anime_id {anime_id}, slug {series_slug}...")
            process_series(anime_id, series_slug, s3_client)
        print("[Background Checker] Selesai satu siklus pengecekan. Menunggu 10 menit...\n")
        sleep(6000)  # 600 detik = 10 menit


@app.on_event("startup")
async def start_background_loop():
    # Mulai background thread secara otomatis saat aplikasi dinyalakan
    thread = threading.Thread(target=background_checker, daemon=True)
    thread.start()

# --- ENDPOINTS MANUAL/REFRESH ---

@app.get("/refresh")
async def refresh_all():
    s3_client = initialize_b2_client()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT anime_id, slug FROM otakudesu")
    series_list = cursor.fetchall()
    cursor.close()
    conn.close()
    
    for series in series_list:
        process_series(series["anime_id"], series["slug"], s3_client)
    
    return {"message": "Semua data episode berhasil diperbarui."}

@app.get("/manual")
async def manual_refresh(slug: str = Query(..., description="Slug anime yang ingin diperbarui")):
    s3_client = initialize_b2_client()
    lock = get_lock(slug)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=400, detail="Proses untuk slug ini sedang berjalan. Coba lagi nanti.")
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT anime_id, slug FROM otakudesu WHERE slug = %s", (slug,))
        series = cursor.fetchone()
        cursor.close()
        conn.close()
        if not series:
            raise HTTPException(status_code=404, detail="Anime dengan slug ini tidak ditemukan")
        process_series(series["anime_id"], series["slug"], s3_client)
        return {"message": f"Data untuk slug '{slug}' berhasil diperbarui."}
    finally:
        lock.release()

@app.get("/belum")
async def get_failed_logs():
    """
    Endpoint untuk mendapatkan log anime/episode yang gagal diproses.
    Hasilnya berupa daftar log error terbaru (tanpa duplikasi).
    """
    return {"failed_logs": failed_logs}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=500)
