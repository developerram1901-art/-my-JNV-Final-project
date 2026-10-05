# JNV Attendance Management System — Deployment Pack

Ye package Flask based JNV Attendance app ke liye hai.

## 1. Sabse important: Gmail

App me **ek hi Gmail sender** use hota hai:

- Principal registration OTP
- Principal password reset OTP
- Principal credentials email
- Existing MOD ID/password email

Environment variables:

```text
JNV_GMAIL_ADDRESS=yourgmail@gmail.com
JNV_GMAIL_APP_PASSWORD=abcdefghijklmnop
```

Normal Gmail password use mat karo. Google App Password use karo.

### Gmail App Password banane ka short process

1. Apne Gmail/Google account me 2-Step Verification enable karo.
2. Google Account → Security → App passwords.
3. App Password create karo, naam `JNV Attendance` rakh sakte ho.
4. 16-character App Password ko `JNV_GMAIL_APP_PASSWORD` me rakho.
5. Spaces hata sakte ho; app khud bhi spaces remove karta hai.

---

# 2. Admin Login

Default first-run Admin:

```text
ID: Roy@1901
Password: pass@1901
```

Deployment ke liye better hai Render/Railway ke Environment Variables me ye set karna:

```text
JNV_ADMIN_USERNAME=Roy@1901
JNV_ADMIN_PASSWORD=pass@1901
JNV_ADMIN_EMAIL=yourgmail@gmail.com
```

**Important:** `admins.json` ek baar banne ke baad usme saved Admin credentials continue rahenge. Admin Panel se ID/password change kar sakte ho.

---

# 3. Principal account ka flow

Main Login page par Admin button hai.

```text
Main Login
   ↓
Admin Login
   ↓
Admin Panel
   ↓
Create Principal
   ↓
Principal email par OTP
   ↓
Admin OTP verify karta hai
   ↓
Principal account create
   ↓
Principal ke email par Login ID + Password
```

Multiple Principals create kiye ja sakte hain.

---

# 4. Google Drive ko apne personal Google Drive se connect karna

App local `data/*.json` files me data rakhta hai aur Google Drive configured hone par har JSON ko tumhare selected Drive folder me mirror karta hai.

## A. Google Cloud project

1. Google Cloud Console kholo.
2. New project banao, example: `JNV Attendance`.
3. APIs & Services → Library.
4. **Google Drive API** enable karo.
5. OAuth consent screen configure karo.
6. Agar app Testing mode me hai, apna Gmail address Test User me add karo.
7. Credentials → Create Credentials → OAuth client ID.
8. Application type: **Desktop app**.
9. JSON download karo.
10. File ko project folder me `client_secret.json` naam se rakho.

`client_secret.json` ko GitHub par upload mat karna.

## B. Local token create karo

PowerShell:

```powershell
cd JNV_Attendance_DEPLOYMENT
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python setup_google_drive.py
```

Browser open hoga. Apne **same Google account** se login karo jiske Drive me data rakhna hai.

Successful login ke baad:

```text
google_token.json
```

banega.

Is file ko GitHub par upload mat karna.

## C. Drive folder banao

Apne Google Drive me ek folder banao, example:

```text
JNV-Attendance-Data
```

Folder open karke browser URL me `/folders/` ke baad jo ID hai, wo copy karo.

Example:

```text
https://drive.google.com/drive/folders/ABC123...
```

Folder ID:

```text
ABC123...
```

---

# 5. Local `.env`

`.env.example` ko copy karke `.env` banao.

Example:

```text
JNV_SECRET_KEY=put-a-long-random-secret-here
JNV_GMAIL_ADDRESS=yourgmail@gmail.com
JNV_GMAIL_APP_PASSWORD=abcdefghijklmnop
JNV_ADMIN_USERNAME=Roy@1901
JNV_ADMIN_PASSWORD=pass@1901
JNV_ADMIN_EMAIL=yourgmail@gmail.com
JNV_GOOGLE_DRIVE_FOLDER_ID=ABC123...
JNV_GOOGLE_TOKEN_JSON={"token":"...","refresh_token":"...","token_uri":"https://oauth2.googleapis.com/token","client_id":"...","client_secret":"...","scopes":["https://www.googleapis.com/auth/drive"],"universe_domain":"googleapis.com","account":""}
JNV_DEBUG=0
```

`JNV_GOOGLE_TOKEN_JSON` me `google_token.json` ka complete JSON ek hi line me paste karna hai.

---

# 6. Local run

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Browser:

```text
http://127.0.0.1:8080
```

Health check:

```text
http://127.0.0.1:8080/health
```

---

# 7. GitHub par upload

GitHub par **real `.env`, `google_token.json`, `client_secret.json`, JSON data files** upload mat karo.

PowerShell:

```powershell
git init
git add .
git commit -m "JNV Attendance deployment"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

Agar GitHub repo pehle se bana hua hai to uska URL use karo.

---

# 8. Render par deploy — easiest

1. Render account kholo.
2. New → Web Service.
3. GitHub repository connect karo.
4. Repository select karo.
5. Runtime: Python.
6. Build Command:

```text
pip install -r requirements.txt
```

7. Start Command:

```text
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

8. Environment Variables me ye values add karo:

```text
JNV_SECRET_KEY
JNV_GMAIL_ADDRESS
JNV_GMAIL_APP_PASSWORD
JNV_ADMIN_USERNAME
JNV_ADMIN_PASSWORD
JNV_ADMIN_EMAIL
JNV_GOOGLE_DRIVE_FOLDER_ID
JNV_GOOGLE_TOKEN_JSON
```

9. Deploy karo.
10. Deploy hone ke baad:

```text
https://YOUR-SERVICE.onrender.com/health
```

par `status: ok` aana chahiye.

11. Main URL kholo aur Admin Login test karo.

### Render par ek important baat

Local disk ko permanent database mat samjho. Is project me Google Drive JSON mirror isliye enabled hai. Production me `JNV_GOOGLE_TOKEN_JSON` aur `JNV_GOOGLE_DRIVE_FOLDER_ID` correctly set hone chahiye.

---

# 9. Railway / doosre Python hosts

Jahan Python Flask/Gunicorn Web Service support ho, same project use kar sakte ho.

Build:

```text
pip install -r requirements.txt
```

Start:

```text
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

Aur same Environment Variables add karo.

---

# 10. Deployment ke baad testing checklist

### Gmail

- Admin → Create Principal
- Principal email par OTP aaya?
- OTP verify hua?
- Principal ko ID/password email aaya?

### Principal

- Principal ID/password se login hua?
- Dashboard open hua?
- Teacher submission monitoring chal rahi hai?

### MOD

- Principal/Admin se MOD create/assign karo.
- MOD email par ID/password aaya?
- MOD login open hua?

### Drive

Apne Drive folder me JSON files check karo:

```text
users.json
mods.json
attendance.json
teacher_submissions.json
admin_credential_logs.json
...
```

---

# 11. 404 se bachne ke liye

Deployment ke baad old server/process use mat karo. New commit deploy hone ke baad browser me hard refresh karo.

Main routes:

```text
/
/admin-login
/admin
/admin/principal/create
/admin/principal/verify
/admin/change-password
/health
/mod-login
```

Flask `url_for()` se links generate karta hai; manually `/{{ ... }}` URL nahi banaye gaye hain.

---

# 12. Security warning

Admin Panel me requested feature ke hisaab se Principal credentials ka record rakha gaya hai. Isliye `admin_credential_logs.json` sensitive hai.

Production me GitHub public repo me koi password, Gmail App Password, OAuth token ya real student data commit mat karo.

Long-term security ke liye passwords ko plaintext ke bajay hashed storage me migrate karna recommended hai.
