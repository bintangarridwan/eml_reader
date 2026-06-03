# EML Reader (Streamlit)

Aplikasi lokal untuk membaca file email format **`.eml`**:
- Menampilkan header (From/To/Subject/Date, dll.)
- Menampilkan body **text/plain** dan **HTML**
- Menampilkan & mengunduh attachment

## Cara menjalankan

Di Windows (PowerShell):

```powershell
cd d:\Project\eml
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Lalu buka URL yang muncul di terminal (biasanya `http://localhost:8501`).

