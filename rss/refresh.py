import time
import requests

url = "https://ccgnimex.my.id/v2/android/berita/rss.php"

def visit_website():
    try:
        response = requests.get(url)
        # Tambahkan penanganan kesalahan atau verifikasi bahwa permintaan berhasil
        if response.status_code == 200:
            print("Website berhasil diakses:", time.ctime())
        else:
            print("Gagal mengakses website. Kode status:", response.status_code)
    except Exception as e:
        print("Terjadi kesalahan:", str(e))

def main():
    while True:
        visit_website()
        time.sleep(1800)  # 30 menit

if __name__ == "__main__":
    main()